#!/usr/bin/env python3
"""Patch: Add web_search + read_webpage tools to orchestrator and API endpoints.

This gives the LLM the ability to:
1. Search the web for any topic (DuckDuckGo, no API key)
2. Read any web page and extract text content
3. Use that information in investment analysis

Usage: cd /home/q/ai_berkshire_web && python3 patch_web_search.py
"""
import shutil
from pathlib import Path

BASE = Path("/home/q/ai_berkshire_web/app")
ORCHESTRATOR = BASE / "agents" / "orchestrator.py"
MAIN_PY = BASE / "main.py"

# Backup
for f in [ORCHESTRATOR, MAIN_PY]:
    bak = f.with_suffix(f".websearch_bak{f.suffix}")
    if not bak.exists():
        shutil.copy2(f, bak)
        print(f"  Backup: {f.name} -> {bak.name}")

# ═══════════════════════════════════════════════════════
#  1. Patch orchestrator.py — add web search tool schemas + executor
# ═══════════════════════════════════════════════════════

orch = ORCHESTRATOR.read_text(encoding="utf-8")

# 1a. Add web search tool schemas after DATA_TOOL_SCHEMAS
WEB_TOOL_SCHEMAS = '''
# ==================== Web Search Tool Schemas ====================

WEB_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取最新信息。可以搜索公司新闻、行业报告、政策法规、宏观数据等任何主题。返回搜索结果列表（标题、URL、摘要）。使用场景：当你需要查找训练数据中没有的最新信息时，先搜索再阅读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如'贵州茅台 2025年业绩'或'新能源汽车行业趋势'"},
                    "max_results": {"type": "integer", "description": "最大返回结果数，默认10", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "读取指定URL的网页内容，提取干净的文本。可以读取新闻文章、研究报告、年报、公告等任何网页。返回标题和正文内容。使用场景：搜索到相关结果后，读取具体页面获取详细信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要读取的网页URL"},
                    "max_chars": {"type": "integer", "description": "最大读取字符数，默认8000", "default": 8000},
                },
                "required": ["url"],
            },
        },
    },
]

'''

# Insert after ALL_TOOL_SCHEMAS definition
ALL_TOOLS_MARKER = 'ALL_TOOL_SCHEMAS = FINANCIAL_TOOL_SCHEMAS + DATA_TOOL_SCHEMAS'
ALL_TOOLS_NEW = 'ALL_TOOL_SCHEMAS = FINANCIAL_TOOL_SCHEMAS + DATA_TOOL_SCHEMAS + WEB_TOOL_SCHEMAS'

if "WEB_TOOL_SCHEMAS" not in orch:
    # Insert the schema definition before ALL_TOOL_SCHEMAS
    orch = orch.replace(
        f"# All tools available to agents\n{ALL_TOOLS_MARKER}",
        f"{WEB_TOOL_SCHEMAS}# All tools available to agents\n{ALL_TOOLS_NEW}",
    )
    print("  [orchestrator.py] Added WEB_TOOL_SCHEMAS")
else:
    print("  [orchestrator.py] WEB_TOOL_SCHEMAS already present")

# 1b. Add web_search + read_webpage to tool executor
TOOL_EXECUTOR_OLD = '''    # --- Market data tools (async) ---
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
    }

    if tool_name in async_tools:
        try:
            result = await async_tools[tool_name](**arguments)
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {tool_name}"}, ensure_ascii=False)'''

TOOL_EXECUTOR_NEW = '''    # --- Market data tools (async) ---
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
    }

    if tool_name in async_tools:
        try:
            result = await async_tools[tool_name](**arguments)
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {tool_name}"}, ensure_ascii=False)'''

if "web_search" not in orch:
    if TOOL_EXECUTOR_OLD in orch:
        orch = orch.replace(TOOL_EXECUTOR_OLD, TOOL_EXECUTOR_NEW, 1)
        print("  [orchestrator.py] Added web_search + read_webpage to tool executor")
    else:
        print("  [orchestrator.py] WARNING: Could not find tool executor block!")
else:
    print("  [orchestrator.py] web_search already in executor")

ORCHESTRATOR.write_text(orch, encoding="utf-8")
print(f"  [orchestrator.py] Saved ({len(orch)} bytes)")

# ═══════════════════════════════════════════════════════
#  2. Patch main.py — add web search API endpoints
# ═══════════════════════════════════════════════════════

mp = MAIN_PY.read_text(encoding="utf-8")

WEB_ENDPOINTS = '''
@app.get("/api/data/web-search", dependencies=[Depends(_verify_token)])
async def api_web_search(q: str, max_results: int = 10):
    """Search the web for any topic."""
    from .tools.web_search import web_search
    results = await web_search(q, max_results=max_results)
    return {"query": q, "results": results}


@app.get("/api/data/read-url", dependencies=[Depends(_verify_token)])
async def api_read_url(url: str, max_chars: int = 8000):
    """Read any web page and extract text content."""
    from .tools.web_search import read_webpage
    result = await read_webpage(url, max_chars=max_chars)
    return result


@app.get("/api/data/price/{symbol}"'''

WEB_MARKER = '@app.get("/api/data/price/{symbol}"'

if "web-search" not in mp:
    mp = mp.replace(WEB_MARKER, WEB_ENDPOINTS, 1)
    print("  [main.py] Added /api/data/web-search and /api/data/read-url endpoints")
else:
    print("  [main.py] Web search endpoints already present")

MAIN_PY.write_text(mp, encoding="utf-8")
print(f"  [main.py] Saved ({len(mp)} bytes)")

print("\n✅ Web search patch complete!")
print("  New file: app/tools/web_search.py")
print("  The LLM can now search the web and read any web page!")
