#!/usr/bin/env python3
"""Patch: Add industry chain search tools to orchestrator + API."""
from pathlib import Path

BASE = Path("/home/q/ai_berkshire_web/app")
ORCH = BASE / "agents" / "orchestrator.py"
MAIN_PY = BASE / "main.py"

# ═══════════════════════════════════════════════════════
#  1. Patch orchestrator.py
# ═══════════════════════════════════════════════════════

orch = ORCH.read_text(encoding="utf-8")

# 1a. Add industry chain tool schemas
INDUSTRY_SCHEMAS = '''
# ==================== Industry Chain Tool Schemas ====================

INDUSTRY_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_industry_chain",
            "description": "从专业产业研究网站搜索产业链信息。自动匹配最相关的数据源（艾瑞咨询/头豹研究院/前瞻产业研究院等），执行站内搜索。返回搜索结果列表（标题、URL、摘要、来源）。适用于分析行业上下游、竞争格局、产业链环节时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "行业或产业链关键词，如'新能源汽车产业链'或'半导体上游材料'"},
                    "max_per_source": {"type": "integer", "description": "每个数据源最大结果数，默认3", "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_industry_sources",
            "description": "列出所有可用的产业研究数据源（艾瑞咨询/头豹/前瞻等），可按类别筛选。返回数据源列表（名称、域名、覆盖领域、描述）。适用于了解有哪些专业数据源可用于产业链研究。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "按类别筛选，如'新能源'/'半导体'/'消费电子'。为空返回全部。", "default": ""},
                },
            },
        },
    },
]

'''

ALL_TOOLS_MARKER = "ALL_TOOL_SCHEMAS = FINANCIAL_TOOL_SCHEMAS + DATA_TOOL_SCHEMAS + WEB_TOOL_SCHEMAS"
ALL_TOOLS_NEW = "ALL_TOOL_SCHEMAS = FINANCIAL_TOOL_SCHEMAS + DATA_TOOL_SCHEMAS + WEB_TOOL_SCHEMAS + INDUSTRY_TOOL_SCHEMAS"

if "INDUSTRY_TOOL_SCHEMAS" not in orch:
    # Insert schemas before ALL_TOOL_SCHEMAS
    orch = orch.replace(
        f"# All tools available to agents\n{ALL_TOOLS_MARKER}",
        f"{INDUSTRY_SCHEMAS}# All tools available to agents\n{ALL_TOOLS_NEW}",
    )
    print("  [orchestrator.py] Added INDUSTRY_TOOL_SCHEMAS")
else:
    print("  [orchestrator.py] INDUSTRY_TOOL_SCHEMAS already present")

# 1b. Add industry tools to executor
EXECUTOR_OLD = '''    # --- Web search tools (async) ---
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

EXECUTOR_NEW = '''    # --- Web search tools (async) ---
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

if "search_industry_chain" not in orch:
    if EXECUTOR_OLD in orch:
        orch = orch.replace(EXECUTOR_OLD, EXECUTOR_NEW, 1)
        print("  [orchestrator.py] Added search_industry_chain + list_industry_sources to executor")
    else:
        print("  [orchestrator.py] WARNING: Could not find executor block!")
        # Try to find it
        import re
        m = re.search(r'# --- Web search tools.*?async_tools = \{[^}]+\}', orch, re.DOTALL)
        if m:
            print(f"  Found:\n{m.group()[:300]}")
else:
    print("  [orchestrator.py] search_industry_chain already in executor")

ORCH.write_text(orch, encoding="utf-8")
print(f"  [orchestrator.py] Saved ({len(orch)} bytes)")

# ═══════════════════════════════════════════════════════
#  2. Patch main.py — add industry chain API endpoints
# ═══════════════════════════════════════════════════════

mp = MAIN_PY.read_text(encoding="utf-8")

INDUSTRY_ENDPOINTS = '''
@app.get("/api/data/industry-search", dependencies=[Depends(_verify_token)])
async def api_industry_search(q: str, max_per_source: int = 3):
    """Search industry chain information from curated sources."""
    from .tools.industry_sources import search_industry_chain
    result = await search_industry_chain(q, max_per_source=max_per_source)
    return result


@app.get("/api/data/industry-sources", dependencies=[Depends(_verify_token)])
async def api_industry_sources(category: str = ""):
    """List available industry research data sources."""
    from .tools.industry_sources import list_industry_sources
    return {"sources": list_industry_sources(category)}


@app.get("/api/data/price/{symbol}"'''

MARKER = '@app.get("/api/data/price/{symbol}"'

if "industry-search" not in mp:
    mp = mp.replace(MARKER, INDUSTRY_ENDPOINTS, 1)
    print("  [main.py] Added /api/data/industry-search and /api/data/industry-sources endpoints")
else:
    print("  [main.py] Industry endpoints already present")

MAIN_PY.write_text(mp, encoding="utf-8")
print(f"  [main.py] Saved ({len(mp)} bytes)")

print("\n✅ Industry chain tools patch complete!")
