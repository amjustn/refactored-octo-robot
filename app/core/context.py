"""Context Injector — Provides real-world context to LLM agents.

Before each agent runs, this module injects:
1. Current date and time (so the LLM knows "today")
2. Market status (open/closed for A-share/HK/US)
3. Latest market indices (if cached)
4. Relevant company data (if the target is a stock)
5. Latest investment philosophy updates from the knowledge base

This ensures the LLM (with potentially outdated training data) always
has access to current information when making investment analysis.
"""
import logging
from datetime import datetime

from .config import BASE_DIR

logger = logging.getLogger("ai_berkshire.context")


def build_time_context() -> str:
    """Build current time context string."""
    now = datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    # Determine market status
    hour = now.hour
    is_weekday = now.weekday() < 5

    market_status_parts = []
    if is_weekday:
        if 9 <= hour < 15:
            market_status_parts.append("A股: 交易中")
        else:
            market_status_parts.append("A股: 已收盘")
        if 9 <= hour < 16:
            market_status_parts.append("港股: 交易中")
        else:
            market_status_parts.append("港股: 已收盘")
        if hour >= 21 or hour < 5:
            market_status_parts.append("美股: 交易中")
        else:
            market_status_parts.append("美股: 已收盘/未开盘")
    else:
        market_status_parts.append("周末休市")

    return f"""## 当前时间上下文

**当前日期**：{now.strftime('%Y年%m月%d日')} {weekday_cn}
**当前时间**：{now.strftime('%H:%M:%S')}
**市场状态**：{' | '.join(market_status_parts)}

**重要提示**：你的训练数据可能有截止日期，请基于以上当前时间进行分析。
对于你不确定的最新数据，请标注"需要核实"并使用 get_stock_price 工具获取最新数据。
"""


def build_market_context(indices_data: dict = None) -> str:
    """Build market indices context from cached data."""
    if not indices_data:
        return ""

    parts = ["## 最新市场指数"]
    for key, val in indices_data.items():
        if key == "timestamp":
            continue
        if isinstance(val, dict):
            name = val.get("name", key)
            price = val.get("price", 0)
            change = val.get("change_pct", 0)
            if change > 0:
                parts.append(f"- {name}: {price:,.2f} (🔴+{change:.2f}%)")
            elif change < 0:
                parts.append(f"- {name}: {price:,.2f} (🟢{change:.2f}%)")
            else:
                parts.append(f"- {name}: {price:,.2f} (0.00%)")

    if len(parts) == 1:
        return ""

    ts = indices_data.get("timestamp", "")
    parts.append(f"\n*数据时间: {ts}*")
    return "\n".join(parts) + "\n"


def build_company_context(symbol: str, price_data: dict = None, news_data: list = None) -> str:
    """Build company-specific context from cached data."""
    parts = []

    if price_data and not price_data.get("error"):
        parts.append(f"## {symbol} 实时行情")
        name = price_data.get("name", symbol)
        price = price_data.get("price", 0)
        change = price_data.get("change_pct", 0)
        # Use appropriate currency symbol based on market/currency
        currency = price_data.get("currency", "")
        market = price_data.get("market", "")
        if market == "A_SHARE" or currency == "CNY":
            currency_sym = "¥"
        elif market == "HK" or currency == "HKD":
            currency_sym = "HK$"
        else:
            currency_sym = "$"
        parts.append(f"- **{name}** ({symbol}): {currency_sym}{price:,.2f}")

        if change > 0:
            parts.append(f"  涨跌幅: 🔴+{change:.2f}%")
        elif change < 0:
            parts.append(f"  涨跌幅: 🟢{change:.2f}%")
        else:
            parts.append(f"  涨跌幅: 0.00%")

        if price_data.get("pe"):
            parts.append(f"  PE: {price_data['pe']:.1f}")
        if price_data.get("pb"):
            parts.append(f"  PB: {price_data['pb']:.1f}")
        if price_data.get("total_market_cap"):
            cap = price_data["total_market_cap"]
            if cap > 1e8:
                parts.append(f"  总市值: {cap/1e8:.1f}亿")
            else:
                parts.append(f"  总市值: {cap:,.0f}")

        if price_data.get("_stale"):
            parts.append(f"\n  ⚠️ 数据可能过期（缓存时间: {price_data.get('timestamp', 'unknown')}）")

        parts.append("")

    if news_data and len(news_data) > 0:
        parts.append(f"## {symbol} 近期新闻")
        for item in news_data[:5]:  # Top 5 news
            title = item.get("title", "")
            date = item.get("date", "")
            source = item.get("source", "")
            parts.append(f"- [{date}] {title} ({source})")
        if len(news_data) > 5:
            parts.append(f"\n*共 {len(news_data)} 条新闻*")
        parts.append("")

    return "\n".join(parts) if parts else ""


def build_knowledge_context() -> str:
    """Build investment philosophy context from knowledge base."""
    from ..tools.knowledge_updater import get_knowledge_summary_for_prompt
    return get_knowledge_summary_for_prompt()


async def build_web_search_context(symbol: str = "", arguments: str = "", skill_name: str = "") -> str:
    """Automatically search the web for latest information about the target.

    This is called by build_full_context() before every report generation,
    ensuring the LLM has access to the LATEST internet information — not just
    cached API data or its training data.

    Searches:
    1. Latest news about the company (web_search)
    2. Industry chain information (search_industry_chain)
    3. Reads top 2 search results for detailed content (read_webpage)

    Returns a formatted context string with search findings.
    """
    parts = []
    search_keywords = []

    # Build search keywords from symbol and arguments
    if symbol:
        search_keywords.append(symbol)
        # Also search by company name if it's a known Chinese company
        from ..tools.market_data import _resolve_company_name, COMPANY_NAME_MAP
        # Reverse lookup: find Chinese name from code
        for name, code in COMPANY_NAME_MAP.items():
            if code.upper() == symbol.upper():
                search_keywords.append(name)
                break

    # Extract additional keywords from arguments (first 20 chars)
    if arguments:
        # Use arguments directly as search term (truncated)
        arg_short = arguments.strip()[:30]
        if arg_short and arg_short not in search_keywords:
            search_keywords.append(arg_short)

    if not search_keywords:
        return ""

    primary_keyword = search_keywords[0]
    parts.append(f"## 网络最新信息（自动搜索）")
    parts.append(f"*搜索关键词: {primary_keyword}*")
    parts.append("")

    # ── 1. Web search for latest news ──
    web_results = []
    try:
        from ..tools.web_search import web_search, read_webpage
        import asyncio

        search_query = f"{primary_keyword} 2025 最新 业绩 新闻"
        logger.info(f"Auto web search for context: {search_query}")
        web_results = await asyncio.wait_for(
            web_search(search_query, max_results=8),
            timeout=15.0,
        )

        if web_results and not web_results[0].get("_placeholder"):
            parts.append(f"### 搜索结果 ({len(web_results)} 条)")
            for i, r in enumerate(web_results[:5]):
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("snippet", "")
                parts.append(f"- [{i+1}] {title}")
                if snippet:
                    parts.append(f"  摘要: {snippet[:100]}")
                parts.append(f"  来源: {url[:80]}")
            parts.append("")

            # ── 2. Read top 2 search results for detailed content ──
            pages_read = 0
            for result in web_results[:2]:
                url = result.get("url", "")
                if not url or result.get("_placeholder"):
                    continue
                # Skip baidu wiki pages (often 403)
                if "baike.baidu.com" in url:
                    continue
                try:
                    page = await asyncio.wait_for(
                        read_webpage(url, max_chars=3000),
                        timeout=15.0,
                    )
                    if page and "content" in page and not page.get("error"):
                        title = page.get("title", "")
                        content = page.get("content", "")
                        parts.append(f"### 网页内容: {title[:50]}")
                        parts.append(f"*URL: {url[:80]}*")
                        parts.append(content[:1500])
                        if len(content) > 1500:
                            parts.append("...(内容已截断)")
                        parts.append("")
                        pages_read += 1
                except asyncio.TimeoutError:
                    logger.debug(f"Read webpage timeout: {url}")
                except Exception as e:
                    logger.debug(f"Read webpage failed: {e}")
                if pages_read >= 2:
                    break
    except ImportError:
        logger.debug("web_search module not available")
    except Exception as e:
        logger.debug(f"Web search context failed: {e}")

    # ── 3. Direct site crawling (latest articles from industry websites) ──
    try:
        from ..tools.site_crawler import build_industry_news_context
        import asyncio as _aio

        crawl_ctx = await _aio.wait_for(
            build_industry_news_context(primary_keyword),
            timeout=20.0,
        )
        if crawl_ctx:
            parts.append(crawl_ctx)
    except Exception as e:
        logger.debug(f"Site crawling context failed: {e}")

    # ── 4. Industry chain search (for industry-related skills) ──
    industry_keywords = ["产业链", "行业", "竞争", "产业", "赛道", "新能源", "半导体", "消费"]
    is_industry_query = any(kw in arguments for kw in industry_keywords) or "industry" in (skill_name or "")

    if is_industry_query and primary_keyword:
        try:
            from ..tools.industry_sources import search_industry_chain
            import asyncio

            industry_query = primary_keyword
            logger.info(f"Auto industry chain search for context: {industry_query}")
            industry_result = await asyncio.wait_for(
                search_industry_chain(industry_query, max_per_source=2),
                timeout=20.0,
            )

            if industry_result and industry_result.get("results"):
                sources = industry_result.get("sources_searched", [])
                results = industry_result.get("results", [])
                parts.append(f"### 产业链搜索结果 ({len(results)} 条)")
                parts.append(f"*数据源: {", ".join(sources)}*")
                for r in results[:5]:
                    title = r.get("title", "")
                    url = r.get("url", "")
                    source_name = r.get("source_name", "")
                    parts.append(f"- [{source_name}] {title}")
                    parts.append(f"  URL: {url[:80]}")
                parts.append("")
        except ImportError:
            logger.debug("industry_sources module not available")
        except Exception as e:
            logger.debug(f"Industry chain search context failed: {e}")

    if len(parts) <= 2:  # Only header, no results
        return ""

    parts.append("---")
    return "\n".join(parts)


async def build_full_context(arguments: str = "", skill_name: str = "") -> str:
    """Build the full context string to inject into agent prompts.

    This combines time, market, company, skill-specific, and knowledge context.
    Tries to fetch real-time data but falls back gracefully.
    """
    parts = [build_time_context()]

    # Try to get market indices
    try:
        from ..tools.market_data import (
            fetch_market_indices, get_stock_price, fetch_company_news,
            _detect_market, _resolve_company_name,
        )
        import re

        indices = await fetch_market_indices()
        if indices and not indices.get("error"):
            parts.append(build_market_context(indices))

        # Try to extract stock symbol from arguments
        # Strategy: 1) Check Chinese company name map 2) Regex for code patterns
        symbol = None

        # First, try resolving Chinese company names (腾讯, 茅台, 拼多多, etc.)
        resolved = _resolve_company_name(arguments.strip())
        if resolved:
            symbol = resolved
        else:
        # Look for patterns like: 600519, AAPL, 0700.HK, 002555.SZ
            # Also try case-insensitive US tickers
            # Exclude common false positives: Q1-Q4, AI, PE, PB, ROE, ROI, etc.
            _FALSE_POSITIVES = {
                "Q1", "Q2", "Q3", "Q4", "AI", "PE", "PB", "ROE", "ROI",
                "EPS", "CEO", "CFO", "CTO", "IPO", "SPAC", "ETF", "API",
                "GDP", "CPI", "PPI", "EBIT", "FCF", "DCA", "NAV", "AUM",
                "YTD", "MTD", "QTD", "H1", "H2", "YOY", "MOM",
                "IT", "HR", "PR", "US", "UK", "EU", "JP", "KR",
                "URL", "PDF", "CSV", "HTML", "JSON", "XML",
                # Single-letter false positives (A股, B股, H股, etc.)
                "A", "B", "H", "T", "F", "C", "S", "P", "V", "D", "E",
                "I", "K", "M", "N", "R", "U", "W", "X", "Y", "Z", "G", "L", "O", "J", "Q",
                # Common financial abbreviations that could be tickers
                "EBITDA", "CAPEX", "OPEX", "MARGIN", "CASH", "DEBT", "BOOK",
                "SALE", "ASSET", "EBIT", "REVENUE", "PROFIT", "LOSS", "COST",
                "BOOK", "DEEP", "GOOD", "REAL", "TICK", "BOND", "FUND",
            }
            symbol_match = re.search(
                r'\b([A-Za-z]{1,6}(?:\.HK)?|\d{6}(?:\.SH|\.SZ|\.HK)?)\b',
                arguments
            )
            if symbol_match:
                candidate = symbol_match.group(1).upper()
                # Skip common false positives (only for short purely-alpha codes)
                if candidate in _FALSE_POSITIVES and not re.match(r'^\d{6}', candidate):
                    pass  # Don't use this as a symbol
                else:
                    symbol = candidate

        if symbol:
            try:
                price = await get_stock_price(symbol)
                if price and not price.get("error"):
                    news = await fetch_company_news(symbol, days=14)
                    parts.append(build_company_context(symbol, price, news))
            except Exception as e:
                logger.debug(f"Could not fetch company data for {symbol}: {e}")
    except ImportError:
        logger.debug("market_data module not available")
    except Exception as e:
        logger.debug(f"Context building partial failure: {e}")

    # ── Auto web search for latest internet information ──
    # This ensures every report has the LATEST news/data from the web
    if symbol or arguments:
        try:
            web_ctx = await build_web_search_context(
                symbol=symbol or "",
                arguments=arguments,
                skill_name=skill_name,
            )
            if web_ctx:
                parts.append(web_ctx)
        except Exception as e:
            logger.debug(f"Web search context failed: {e}")

    # Add skill-specific domain knowledge (each of the 21 skills has its own)
    if skill_name:
        try:
            from ..tools.skill_knowledge import get_skill_knowledge_for_prompt
            skill_knowledge = get_skill_knowledge_for_prompt(skill_name)
            if skill_knowledge:
                parts.append(skill_knowledge)
        except Exception as e:
            logger.debug(f"Skill knowledge context failed: {e}")

    # Add investment philosophy knowledge
    try:
        knowledge = build_knowledge_context()
        if knowledge:
            parts.append(knowledge)
    except Exception as e:
        logger.debug(f"Knowledge context failed: {e}")

    return "\n".join(parts)
