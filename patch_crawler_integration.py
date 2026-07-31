#!/usr/bin/env python3
"""Patch: Integrate site_crawler into build_web_search_context."""
from pathlib import Path

CONTEXT = Path("/home/q/ai_berkshire_web/app/core/context.py")
code = CONTEXT.read_text(encoding="utf-8")

# Add site crawling after web search in build_web_search_context
OLD = '''    # ── 3. Industry chain search (for industry-related skills) ──'''

NEW = '''    # ── 3. Direct site crawling (latest articles from industry websites) ──
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

    # ── 4. Industry chain search (for industry-related skills) ──'''

if "Direct site crawling" not in code:
    if OLD in code:
        code = code.replace(OLD, NEW, 1)
        CONTEXT.write_text(code, encoding="utf-8")
        print(f"✅ Patched! context.py now crawls industry sites for latest articles")
    else:
        print("ERROR: Could not find insertion point!")
        import re
        matches = re.findall(r'.{0,50}Industry chain search.{0,50}', code)
        for m in matches:
            print(f"  Found: {m}")
else:
    print("Already patched!")
