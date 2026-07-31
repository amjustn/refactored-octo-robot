#!/usr/bin/env python3
"""Patch: Add automatic web search to build_full_context().

This ensures that EVERY report generation automatically:
1. Searches the web for latest company/industry news
2. Reads top search results for detailed content
3. Searches industry chain databases (艾瑞/头豹/前瞻 etc.)
4. Injects all findings into the LLM context before report generation

Usage: cd /home/q/ai_berkshire_web && python3 patch_context_websearch.py
"""
from pathlib import Path
import shutil

CONTEXT = Path("/home/q/ai_berkshire_web/app/core/context.py")
bak = CONTEXT.with_suffix(".websearch_bak.py")
if not bak.exists():
    shutil.copy2(CONTEXT, bak)
    print(f"  Backup: {bak.name}")

code = CONTEXT.read_text(encoding="utf-8")

# ═══════════════════════════════════════════════════════
#  1. Add build_web_search_context function
# ═══════════════════════════════════════════════════════

NEW_FUNC = '''async def build_web_search_context(symbol: str = "", arguments: str = "", skill_name: str = "") -> str:
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

    # ── 3. Industry chain search (for industry-related skills) ──
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
    return "\\n".join(parts)


'''

# Insert the new function before build_full_context
MARKER = "async def build_full_context("
if "build_web_search_context" not in code:
    code = code.replace(MARKER, NEW_FUNC + MARKER)
    print("  [context.py] Added build_web_search_context function")
else:
    print("  [context.py] build_web_search_context already exists")

# ═══════════════════════════════════════════════════════
#  2. Call build_web_search_context in build_full_context
# ═══════════════════════════════════════════════════════

# Find the section after company context, before skill knowledge
OLD_SECTION = '''    # Add skill-specific domain knowledge (each of the 21 skills has its own)
    if skill_name:'''

NEW_SECTION = '''    # ── Auto web search for latest internet information ──
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
    if skill_name:'''

if "Auto web search for latest internet information" not in code:
    if OLD_SECTION in code:
        code = code.replace(OLD_SECTION, NEW_SECTION, 1)
        print("  [context.py] Integrated web search into build_full_context")
    else:
        print("  [context.py] WARNING: Could not find insertion point!")
else:
    print("  [context.py] Web search integration already exists")

CONTEXT.write_text(code, encoding="utf-8")
print(f"  [context.py] Saved ({len(code)} bytes)")
print("\n✅ Context web search patch complete!")
print("  Every report will now automatically search the web for latest info.")
