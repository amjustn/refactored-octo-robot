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
