"""Harness tool registry — schemas, executor and (PR-3) the ToolGateway.

PR-2 scope: tool schemas + the dispatch executor moved verbatim from
agents/orchestrator.py. PR-3 adds ToolDef / CircuitBreaker / ToolGateway
(circuit breaking, caching, timeout/retry, fallback chains) on top of
this registry.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("ai_berkshire.harness.tools")

# ==================== Financial Tool Schemas ====================

FINANCIAL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "verify_market_cap",
            "description": "验证市值 = 股价 × 股本，返回偏差百分比和通过/失败状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "price": {"type": "number", "description": "当前股价"},
                    "shares": {"type": "number", "description": "总股本（股）"},
                    "reported": {"type": "number", "description": "报告的市值"},
                    "currency": {"type": "string", "description": "货币单位", "default": "USD"},
                },
                "required": ["price", "shares", "reported"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_valuation",
            "description": "计算关键估值比率：PE/PB/FCF收益率/股息率",
            "parameters": {
                "type": "object",
                "properties": {
                    "price": {"type": "number", "description": "当前股价"},
                    "eps": {"type": "number", "description": "每股收益"},
                    "bvps": {"type": "number", "description": "每股净资产"},
                    "fcf_per_share": {"type": "number", "description": "每股自由现金流"},
                    "dividend": {"type": "number", "description": "每股股息"},
                },
                "required": ["price"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cross_validate",
            "description": "交叉验证同一指标的多源数据，返回偏差和通过/警告/失败状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "指标名称"},
                    "values": {"type": "object", "description": "各数据源的值，如 {\"源A\": 1000, \"源B\": 1005}"},
                    "unit": {"type": "string", "description": "单位", "default": ""},
                },
                "required": ["field", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "three_scenario",
            "description": "三情景估值：乐观/中性/悲观，返回目标价和上涨空间",
            "parameters": {
                "type": "object",
                "properties": {
                    "price": {"type": "number", "description": "当前股价"},
                    "eps": {"type": "number", "description": "当前每股收益"},
                    "shares_100m": {"type": "number", "description": "总股本（亿股）"},
                    "growth_rates": {"type": "array", "items": {"type": "number"}, "description": "三个情景的增长率"},
                    "pe_multiples": {"type": "array", "items": {"type": "number"}, "description": "三个情景的PE倍数"},
                    "currency": {"type": "string", "default": "CNY"},
                },
                "required": ["price", "eps", "shares_100m", "growth_rates", "pe_multiples"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "精确财务计算，支持加减乘除、括号、幂运算。例如: '100 * (1 + 0.1)'",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "benford",
            "description": "本福特定律检测：检查一组数字是否符合本福特定律分布，用于发现财务数据异常",
            "parameters": {
                "type": "object",
                "properties": {
                    "numbers": {"type": "array", "items": {"type": "number"}, "description": "待检测的数字列表"},
                },
                "required": ["numbers"],
            },
        },
    },
]

# ==================== Market Data Tool Schemas ====================

DATA_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": "获取股票最新实时行情数据（支持A股/港股/美股）。自动检测市场类型。返回股价、涨跌幅、成交量、PE、PB、市值等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 600519 / 0700.HK / AAPL"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_financial_data",
            "description": "获取公司财务报表数据（利润表/资产负债表/现金流量表），返回最近几个季度的数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_company_news",
            "description": "获取公司近期新闻列表，返回标题、内容摘要、日期和来源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "days": {"type": "integer", "description": "获取最近多少天的新闻，默认14天", "default": 14},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_market_indices",
            "description": "获取全球主要市场指数（上证指数/深证成指/恒生指数/标普500/纳斯达克），包含最新价格和涨跌幅。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_company",
            "description": "按公司名称或代码搜索股票，返回匹配的股票列表（含代码、名称、市场）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词（公司名或股票代码）"},
                },
                "required": ["query"],
            },
        },
    },
]


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
                "required": [],
            },
        },
    },
]

# All tools available to agents
ALL_TOOL_SCHEMAS = FINANCIAL_TOOL_SCHEMAS + DATA_TOOL_SCHEMAS + WEB_TOOL_SCHEMAS + INDUSTRY_TOOL_SCHEMAS


# ==================== Tool Executor ====================

async def _execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool (financial or market data) and return result as string.

    Supports both sync financial tools and async market data tools.
    """
    # --- Financial tools (sync) ---
    from ..tools import (
        verify_market_cap, verify_valuation, cross_validate,
        three_scenario, calc, benford,
    )
    sync_tools = {
        "verify_market_cap": verify_market_cap,
        "verify_valuation": verify_valuation,
        "cross_validate": cross_validate,
        "three_scenario": three_scenario,
        "calc": calc,
        "benford": benford,
    }

    if tool_name in sync_tools:
        try:
            result = sync_tools[tool_name](**arguments)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # --- Market data tools (async) ---
    from ..tools.market_data import (
        get_stock_price, get_financial_data,
        fetch_company_news, fetch_market_indices,
        search_company,
    )
    # --- Web search tools (async) ---
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
    }

    if tool_name in async_tools:
        try:
            result = await async_tools[tool_name](**arguments)
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {tool_name}"}, ensure_ascii=False)


# Agents that have tool access (financial + market data)
# All research/analysis agents get tools; editorial agents don't need them
TOOL_ENABLED_AGENTS = {
    "business-analyst", "financial-analyst", "industry-researcher", "risk-assessor",
    "company-event-scout", "industry-peer-analyst",
    "business-interpreter", "financial-auditor", "competition-analyst", "risk-hunter",
    "researcher",
}
