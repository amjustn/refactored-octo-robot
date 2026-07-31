#!/usr/bin/env python3
"""Fix: Add web_search + read_webpage to _execute_tool in orchestrator.py"""
from pathlib import Path

ORCH = Path("/home/q/ai_berkshire_web/app/agents/orchestrator.py")
code = ORCH.read_text(encoding="utf-8")

OLD = '''    # --- Market data tools (async) ---
    from ..tools.market_data import (
        get_stock_price, get_financial_data,
        fetch_company_news, fetch_market_indices,
        search_company,
    )
    async_tools = {
        "get_stock_price": get_stock_price,
        "get_financial_data": get_financial_data,
        "fetch_company_news": fetch_company_news,
        "fetch_market_indices": fetch_market_indices,
        "search_company": search_company,
    }'''

NEW = '''    # --- Market data tools (async) ---
    from ..tools.market_data import (
        get_stock_price, get_financial_data,
        fetch_company_news, fetch_market_indices,
        search_company,
    )
    # --- Web search tools (async) ---
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

if "from ..tools.web_search import" in code:
    print("Already patched!")
elif OLD in code:
    code = code.replace(OLD, NEW, 1)
    ORCH.write_text(code, encoding="utf-8")
    print(f"✅ Patched! Saved ({len(code)} bytes)")
else:
    print("ERROR: Could not find target block!")
    # Show what's there
    import re
    m = re.search(r'# --- Market data tools.*?async_tools = \{[^}]+\}', code, re.DOTALL)
    if m:
        print(f"Found block:\n{m.group()[:300]}")
