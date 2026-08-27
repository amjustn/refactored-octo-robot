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
            parts.append("  涨跌幅: 0.00%")

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


async def build_web_search_context(symbol: str = "", arguments: str = "", skill_name: str = "", search_query: str = "") -> str:
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

    # Build search keywords — ALWAYS prioritize user's own words
    # The user's arguments (what they typed) are the ground truth.
    # Symbol-based keywords are fallbacks for when arguments are absent.
    if arguments:
        # Primary keyword = user's exact input (truncated to 60 chars)
        primary_keyword = arguments.strip()[:60]
    elif symbol:
        primary_keyword = symbol
    else:
        return ""

    # Goal-parser hint: a concise search query beats the raw (possibly long) goal
    if search_query and search_query.strip():
        primary_keyword = search_query.strip()[:60]

    parts.append("## 网络最新信息（自动搜索）")
    parts.append(f"*搜索关键词: {primary_keyword}*")
    parts.append("")

    # ── 1. Web search for latest news ──
    web_results = []
    try:
        import asyncio

        # Extract year from arguments if present
        # Match: 2026Q4, 2026年, 2026的, 2026财报, 2026年报 etc.
        import re as _re

        from ..tools.web_search import read_webpage, web_search
        _year_match = _re.search(r'20(\d{2})(?!\d)', arguments) if arguments else None
        _search_year = f"20{_year_match.group(1)}" if _year_match else datetime.now().strftime('%Y')
        # Use user's full query as the search term — this is what they asked for
        search_query = f"{primary_keyword} {_search_year} 最新"
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
        import asyncio as _aio

        from ..tools.site_crawler import build_industry_news_context

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
            import asyncio

            from ..tools.industry_sources import search_industry_chain

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
                parts.append(f"*数据源: {', '.join(sources)}*")
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


async def build_full_context(arguments: str = "", skill_name: str = "", goal_hints: dict = None) -> str:
    """Build the full context string to inject into agent prompts.

    This combines time, market, company, skill-specific, and knowledge context.
    Tries to fetch real-time data but falls back gracefully.

    goal_hints: optional output of core.goal_parser.parse_goal. When None,
    behavior is identical to the legacy path (company map + regex on
    arguments). When present, its symbol/company/search_query/intent steer
    symbol resolution, web search, and the core-instruction section.
    """
    parts = [build_time_context()]

    # ── Pre-validation: check if requested data period is in the future ──
    _data_warnings = []
    import re as _re
    _now = datetime.now()
    _current_quarter = (_now.month - 1) // 3 + 1
    _avail_year = _now.year
    _avail_quarter = _current_quarter - 1 if _current_quarter > 1 else 4
    if _avail_quarter == 4:
        _avail_year -= 1

    if arguments:
        # Check for quarter references: 2026Q4, 2026 Q4
        _quarter_match = _re.search(r'(\d{4})\s*Q([1-4])', arguments)
        if _quarter_match:
            _req_year = int(_quarter_match.group(1))
            _req_quarter = int(_quarter_match.group(2))
            _is_future = (_req_year > _avail_year) or (_req_year == _avail_year and _req_quarter > _avail_quarter)
            if _is_future:
                _qw = ("\n\n**⚠️ 数据不可用警告**：你请求分析的是 " + str(_req_year) + "Q" + str(_req_quarter)
                    + " 财报，但当前最新可用财报为 " + str(_avail_year) + "Q" + str(_avail_quarter) + "。"
                    + str(_req_year) + "Q" + str(_req_quarter) + " 财报尚未发布，请等待财报公布后重试。\n")
                _data_warnings.append(_qw)
        else:
            # Check for bare year references: 2026, 2026年, 2026的, 2026年财报 etc.
            # Exclude false positives like stock codes (600519, 000001)
            _bare_year_match = _re.search(r'(?<!\d)(20\d{2})(?!\d)', arguments)
            if _bare_year_match:
                _req_year = int(_bare_year_match.group(1))
                if _req_year > _avail_year:
                    msg = (
                        "\n\n**⚠️ 数据可用性提醒**：你请求聚焦 " + str(_req_year) + " 年的数据，"
                        "但当前最新完整财年为 " + str(_avail_year) + "。"
                        "" + str(_req_year) + " 年数据可能不完整。"
                        "**【禁止】** 不要生成观察清单、展望、预览等替代内容。"
                        "对确实存在的数据正常分析，对缺失部分直接标注\u201c数据尚未发布\u201d。\n"
                    )
                    _data_warnings.append(msg)

    # ── Market / company / web data — fetched in parallel ──
    # Symbol resolution is synchronous (company-name map + regex) and feeds
    # both the company branch and the web-search branch, so it runs first.
    # The three fetch groups (indices / price+news / web search) are
    # independent of each other and run concurrently via asyncio.gather;
    # each branch degrades to empty on failure, exactly as before.
    symbol = None
    _listing_status = None  # populated during symbol resolution block
    _md = None
    try:
        import re

        from ..tools.market_data import (
            _resolve_company_name,
            fetch_company_news,
            fetch_market_indices,
            get_stock_price,
        )

        # Try to extract stock symbol from arguments
        # Strategy: 1) Check Chinese company name map 2) Regex for code patterns

        # Goal-parser hints take priority when available:
        # validated hint symbol → company-name resolution → legacy path below
        if goal_hints:
            _hint_symbol = (goal_hints.get("symbol") or "").strip()
            if _hint_symbol and re.match(
                r'^\d{6}(\.(SH|SZ))?$|^\d{1,5}\.HK$|^[A-Za-z]{1,6}(\.HK)?$', _hint_symbol
            ):
                symbol = _hint_symbol
            else:
                _hint_company = (goal_hints.get("company") or "").strip()
                if _hint_company:
                    symbol = _resolve_company_name(_hint_company)

        if symbol is None:
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

        _md = (fetch_market_indices, get_stock_price, fetch_company_news)

        # ── Listing status check ──
        _company_name = None
        if goal_hints and goal_hints.get("company"):
            _company_name = goal_hints["company"]
        # F3: no regex fallback here — only the goal parser company
        # extraction is trusted (the old fallback matched filenames like
        # "Report.pdf" as English brand names → spurious "未上市" warnings).
        if _company_name:
            try:
                from ..tools.market_data import check_listing_status
                _listing_status = check_listing_status(_company_name)
            except Exception:
                pass
    except ImportError:
        logger.debug("market_data module not available")
    except Exception as e:
        logger.debug(f"Context building partial failure: {e}")

    # ── Parallel fetch branches ──
    # Each branch is self-contained (no shared mutable state) and returns
    # None/"" on failure so one slow or failing source cannot delay or
    # break the others. price→news stays sequential inside the company
    # branch because news is only fetched when the price lookup succeeds.
    async def _fetch_indices_ctx():
        if _md is None:
            return None
        try:
            indices = await _md[0]()
            if indices and not indices.get("error"):
                # Appended verbatim (possibly "") exactly as the legacy path
                return build_market_context(indices)
        except Exception as e:
            logger.debug(f"Context building partial failure: {e}")
        return None

    async def _fetch_company_ctx():
        if _md is None or not symbol:
            return None
        try:
            price = await _md[1](symbol)
            if price and not price.get("error"):
                news = await _md[2](symbol, days=14)
                return build_company_context(symbol, price, news)
        except Exception as e:
            logger.debug(f"Could not fetch company data for {symbol}: {e}")
        return None

    async def _fetch_web_ctx():
        # Auto web search for latest internet information — ensures every
        # report has the LATEST news/data from the web
        if not (symbol or arguments):
            return ""
        try:
            return await build_web_search_context(
                symbol=symbol or "",
                arguments=arguments,
                skill_name=skill_name,
                search_query=(goal_hints.get("search_query") or "") if goal_hints else "",
            ) or ""
        except Exception as e:
            logger.debug(f"Web search context failed: {e}")
            return ""

    async def _fetch_broker_ctx():
        # Auto broker/机构研报 context — every agent sees institutional
        # ratings & consensus estimates when analyzing an A-share stock.
        if not symbol:
            return ""
        try:
            from ..tools.broker_reports import build_broker_context
            return await build_broker_context(symbol=symbol, arguments=arguments, skill_name=skill_name) or ""
        except Exception as e:
            logger.debug(f"Broker context failed: {e}")
            return ""

    import asyncio
    idx_ctx, comp_ctx, web_ctx, broker_ctx = await asyncio.gather(
        _fetch_indices_ctx(), _fetch_company_ctx(), _fetch_web_ctx(), _fetch_broker_ctx(),
    )
    # Assemble sections in the same order as the sequential implementation
    if idx_ctx is not None:
        parts.append(idx_ctx)
    if comp_ctx is not None:
        parts.append(comp_ctx)
    if broker_ctx:
        parts.append(broker_ctx)
    if web_ctx:
        parts.append(web_ctx)

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

    # Inject any data validation warnings at the top
    if _data_warnings:
        parts.insert(1, "\n".join(_data_warnings))

    # ── Inject listing status ──
    if _listing_status:
        _ls = _listing_status
        if _ls["listed"] is False:
            # Known unlisted company — clear guidance
            parts.insert(1, (
                "\n## ⚠️ 公司上市状态：未上市\n"
                f"**确认信息**：{_ls.get('note', '该公司未上市')}\n\n"
                "**研究方法要求**：\n"
                "- 没有公开股价和财报数据，请使用侦探式研究方法\n"
                "- 从新闻报道、行业报告、融资信息、高管访谈等公开来源拼凑分析\n"
                "- 每项数据和结论必须标注置信度：[确认]/[推算]/[传闻]\n"
                "- 估值只能估算，明确标注估算方法和假设\n"
                "- 诚实标注信息不足的部分，不要假装有数据\n"
            ))
        elif _ls["listed"] is None:
            # Uncertain status — cautious hint
            parts.insert(1, (
                "\n## ⚠️ 公司上市状态：不确定\n"
                f"**{_ls.get('note', '无法确认是否为上市公司')}**\n"
                f"{_ls.get('hint', '')}\n"
            ))

    # ── Global instruction: prioritize user keywords over training data ──
    if arguments:
        _kw = arguments.strip()[:80]
        _instr = ("\n**🔑 核心指令：用户输入的关键词是本次分析的唯一基准。**\n"
            + "用户要求分析：\u300c" + _kw + "\u300d\n"
            + "\n**【严令禁止】**\n"
            + "1. 如果用户要求的财报/数据尚未发布，必须直接告知\u201c该数据尚未发布，无法进行分析\u201d，不得生成任何替代内容。\n"
            + "2. 禁止生成\u201c观察清单\u201d、\u201c发布前展望\u201d、\u201c核心关注点\u201d、\u201c预览\u201d、\u201c指南\u201d等替代性内容。\n"
            + "3. 禁止用旧季度/旧年份的数据拼凑成用户要求的分析。\n"
            + "4. 只有确认数据确实存在且可获取时，才进行实际分析。\n"
            + "5. 如果你的训练数据与用户指定的时间范围不一致，以用户为准并明确标注数据来源的时间。\n")
        if goal_hints:
            _intent = (goal_hints.get("intent") or "").strip()
            if _intent:
                _instr += "\n用户意图：" + _intent[:200] + "\n"
        parts.insert(1, _instr)

    return "\n".join(parts)
