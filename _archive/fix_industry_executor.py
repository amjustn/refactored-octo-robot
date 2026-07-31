#!/usr/bin/env python3
"""Fix: Add industry chain tools to _execute_tool executor."""
from pathlib import Path

ORCH = Path("/home/q/ai_berkshire_web/app/agents/orchestrator.py")
code = ORCH.read_text(encoding="utf-8")

OLD = '''    # --- Web search tools (async) ---
    from ..tools.web_search import web_search, read_webpage

    async_tools = {
        "get_stock_price": get_stock_price,
        "get_financial_data": get_financial_data,
        "fetch_company_news": fetch_company_news,
        "fetch_market_indices": fetch_market_indices,
        "search_company": search_company,
        "web_search": web_search,
        "read_webpage": read_webpage,
    }'''

NEW = '''    # --- Web search tools (async) ---
    from ..tools.web_search import web_search, read_webpage
    # --- Industry chain tools (async) ---
    from ..tools.industry_sources import search_industry_chain, list_industry_sources

    async_tools = {
        "get_stock_price": get_stock_price,
        "get_financial_data": get_financial_data,
        "fetch_company_news": fetch_company_news,
        "fetch_market_indices": fetch_market_indices,
        "search_company": search_company,
        "web_search": web_search,
        "read_webpage": read_webpage,
        "search_industry_chain": search_industry_chain,
        "list_industry_sources": lambda category="": list_industry_sources(category),
    }'''

if "from ..tools.industry_sources import" in code:
    print("Already patched!")
elif OLD in code:
    code = code.replace(OLD, NEW, 1)
    ORCH.write_text(code, encoding="utf-8")
    print(f"✅ Patched! Saved ({len(code)} bytes)")
else:
    print("ERROR: Could not find target block!")
    # Show what's around the web_search import
    idx = code.find("web_search")
    if idx >= 0:
        print(f"Context around 'web_search':")
        print(code[max(0,idx-200):idx+400])
