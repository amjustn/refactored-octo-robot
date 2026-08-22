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

# ==================== Broker Research Report Tool Schemas ====================

BROKER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_broker_reports",
            "description": "获取券商/机构研报数据（东方财富研报中心）：个股评级分布、机构一致预期（次年EPS/PE中位数、目标价区间）、最近研报列表（机构/日期/评级/标题）。仅支持A股6位代码（如600519），港股/美股返回covered=false。分析A股个股的估值、投资评级、市场预期时可调用。**报告中凡使用本工具返回的研报数据，必须在引用处标注来源：单篇研报（来源：<机构名> <日期> 研报），一致预期/评级分布（来源：东方财富研报中心，近一年N篇研报）。**",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A股6位股票代码，如 600519 或 000001.SZ"},
                    "days": {"type": "integer", "description": "统计近多少天研报，默认365", "default": 365},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_broker_report_detail",
            "description": "读取一篇券商研报的正文内容（事件描述/事件点评）。先用 fetch_broker_reports 获取研报列表拿到 info_code，再传入读取具体研报观点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "info_code": {"type": "string", "description": "研报的 info_code（来自 fetch_broker_reports 返回的 info_code 字段）"},
                    "max_chars": {"type": "integer", "description": "最大读取字符数，默认6000", "default": 6000},
                },
                "required": ["info_code"],
            },
        },
    },
]


# All tools available to agents
ALL_TOOL_SCHEMAS = FINANCIAL_TOOL_SCHEMAS + DATA_TOOL_SCHEMAS + WEB_TOOL_SCHEMAS + INDUSTRY_TOOL_SCHEMAS + BROKER_TOOL_SCHEMAS


# ==================== Tool Executor ====================

async def _execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool (financial or market data) and return result as string.

    Supports both sync financial tools and async market data tools.
    """
    # --- Financial tools (sync) ---
    from ..tools import (
        benford,
        calc,
        cross_validate,
        three_scenario,
        verify_market_cap,
        verify_valuation,
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
    # --- Industry chain tools (async) ---
    from ..tools.industry_sources import list_industry_sources, search_industry_chain
    from ..tools.market_data import (
        fetch_company_news,
        fetch_market_indices,
        get_financial_data,
        get_stock_price,
        search_company,
    )

    # --- Broker research report tools (async) ---
    from ..tools.broker_reports import get_rating_summary, get_report_detail

    # --- Web search tools (async) ---
    from ..tools.web_search import read_webpage, web_search

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
        "fetch_broker_reports": get_rating_summary,
        "fetch_broker_report_detail": get_report_detail,
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
    "macro-institutional-economist", "macro-forecaster", "macro-strategist",
    "cn-policy-framework", "cn-data-cycle", "cn-fx-external", "cn-structure-trend",
}


# ==================== ToolGateway (PR-3) ====================

import asyncio
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Awaitable, Callable, Optional


@dataclass
class ToolDef:
    """A registered tool with execution policy.

    ``fn`` 为可选的自定义执行函数（签名 async fn(args: dict) -> str）；
    为空时走底层 ``_execute_tool`` 分发表。行情价格工具注册了源级
    fallback 链实现（akshare->腾讯->新浪 / yfinance->腾讯->雅虎）。
    """
    name: str
    fn: Optional[Callable[[dict], Awaitable[str]]] = None
    timeout: float = 20.0
    retries: int = 1
    cache_ttl: Optional[int] = None      # seconds; None = no gateway-level cache
    fallback: list = field(default_factory=list)  # alternative tool names


@dataclass
class ToolResult:
    tool: str
    ok: bool
    content: str                          # JSON string (same shape as _execute_tool)
    source: str = ""
    latency_ms: float = 0.0
    cache_hit: bool = False
    fallback_depth: int = 0
    circuit: str = "closed"
    error: str = ""


_BUSINESS_ERROR_HINTS = ("不存在", "未找到", "not found", "no data", "无法识别")


def _is_business_error(msg: str) -> bool:
    """A legitimate 'no such data' answer is not a provider failure and must
    not trip the circuit breaker (e.g. querying a delisted/unknown symbol)."""
    m = (msg or "").lower()
    return any(h.lower() in m for h in _BUSINESS_ERROR_HINTS)


class CircuitBreaker:
    """Per-tool circuit breaker.

    - closed: normal; consecutive failures increment a counter
    - open (after ``fail_threshold`` consecutive failures): calls are
      rejected immediately for ``recovery_probe_after`` seconds
    - half-open: after the cooldown exactly one probe call is allowed at
      a time (concurrent calls are rejected); success closes the breaker,
      failure re-opens it
    """

    def __init__(self, name: str, fail_threshold: int = 3, recovery_probe_after: float = 300):
        self.name = name
        self.fail_threshold = fail_threshold
        self.recovery_probe_after = recovery_probe_after
        self.state = "closed"
        self.consecutive_failures = 0
        self.opened_at: Optional[float] = None
        self.last_error: str = ""
        self._probe_in_flight = False

    def allow_call(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.opened_at is not None and (time.monotonic() - self.opened_at) >= self.recovery_probe_after:
                self.state = "half_open"   # allow one probe
                self._probe_in_flight = True
                return True
            return False
        # half_open: only the single probe may pass; concurrent calls wait
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self):
        self.state = "closed"
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = ""
        self._probe_in_flight = False

    def record_failure(self, error: str = ""):
        self.consecutive_failures += 1
        self.last_error = error[:200]
        self._probe_in_flight = False
        if self.state == "half_open" or self.consecutive_failures >= self.fail_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()

    def reset(self):
        self.record_success()

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "open_since_s": round(time.monotonic() - self.opened_at, 1) if self.opened_at else None,
        }


# Tool registry with per-tool execution policy. cache_ttl mirrors the
# DATA_CACHE_TTL_* tiers in core.config (market_data also caches
# internally; the gateway cache short-circuits repeat calls within a run).
TOOL_DEFS: dict[str, ToolDef] = {
    # financial calculators — fast, deterministic, no cache needed
    "verify_market_cap": ToolDef("verify_market_cap", timeout=5),
    "verify_valuation": ToolDef("verify_valuation", timeout=5),
    "cross_validate": ToolDef("cross_validate", timeout=5),
    "three_scenario": ToolDef("three_scenario", timeout=5),
    "calc": ToolDef("calc", timeout=5),
    "benford": ToolDef("benford", timeout=5),
    # market data — network-bound, cached, own internal fallback chains
    "get_stock_price": ToolDef("get_stock_price", timeout=25, cache_ttl=300),
    "get_financial_data": ToolDef("get_financial_data", timeout=30, cache_ttl=86400),
    "fetch_company_news": ToolDef("fetch_company_news", timeout=25, cache_ttl=3600),
    "fetch_market_indices": ToolDef("fetch_market_indices", timeout=20, cache_ttl=600),
    "search_company": ToolDef("search_company", timeout=20, cache_ttl=86400),
    # web / industry
    "web_search": ToolDef("web_search", timeout=20),
    "read_webpage": ToolDef("read_webpage", timeout=20),
    "search_industry_chain": ToolDef("search_industry_chain", timeout=25),
    "list_industry_sources": ToolDef("list_industry_sources", timeout=10, cache_ttl=86400),
    # broker research reports (东财研报中心) — 内部已有 SQLite 缓存
    "fetch_broker_reports": ToolDef("fetch_broker_reports", timeout=25, retries=1),
    "fetch_broker_report_detail": ToolDef("fetch_broker_report_detail", timeout=20, retries=1),
}


class ToolGateway:
    """Unified tool entry: circuit check → cache → timeout/retry →
    fallback chain → TOOL_CALL event.

    Playback mode (eval replay): when ``playback`` is set, matching
    cassette entries are returned instead of executing real tools.
    """

    def __init__(self, tools: Optional[dict[str, ToolDef]] = None):
        # 复制注册表，避免跨实例共享可变的 ToolDef
        self.tools = dict(tools or TOOL_DEFS)
        # 行情价格走源级 fallback 链（收编 market_data*.py 现有抓取函数）
        if "get_stock_price" in self.tools and self.tools["get_stock_price"].fn is None:
            self.tools["get_stock_price"] = replace(
                self.tools["get_stock_price"], fn=self._stock_price_chain,
            )
        # 财务报表同样走源级 fallback 链（US/HK 主源换东财数据中心，yfinance 降权）
        if "get_financial_data" in self.tools and self.tools["get_financial_data"].fn is None:
            self.tools["get_financial_data"] = replace(
                self.tools["get_financial_data"], fn=self._financial_data_chain,
            )
        # 熔断器：工具级 + 数据源级（akshare/yfinance/tencent/sina/yahoo/eastmoney）
        # 共用一个字典，名称互不冲突；源级在首次使用时惰性创建
        self.breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(name) for name in self.tools
        }
        self.playback: Optional[dict[str, str]] = None  # cassette key -> content
        self._cache = None

    # ----- cache -----

    def _get_cache(self):
        if self._cache is None:
            from ..core.data_cache import DataCache
            self._cache = DataCache()
        return self._cache

    @staticmethod
    def _cache_key(tool: str, args: dict) -> str:
        return f"{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

    # ----- circuit breakers -----

    def breaker_states(self) -> dict:
        return {name: b.snapshot() for name, b in self.breakers.items()}

    def reset_breaker(self, name: Optional[str] = None) -> dict:
        if name:
            b = self.breakers.get(name)
            if b:
                b.reset()
                return {"reset": name, "state": b.state}
            return {"reset": name, "error": "unknown tool"}
        for b in self.breakers.values():
            b.reset()
        return {"reset": "all", "count": len(self.breakers)}

    def seed_breaker(self, name: str, state: str, failures: int = 0):
        """Seed a breaker from external health info (e.g. bootstrap check)."""
        b = self.breakers.setdefault(name, CircuitBreaker(name))
        if state == "open":
            b.state = "open"
            b.opened_at = time.monotonic()
            b.consecutive_failures = max(failures, b.fail_threshold)

    # ----- 数据源级熔断（fallback 链用） -----

    def _source_breaker(self, source: str) -> CircuitBreaker:
        """取数据源级熔断器（惰性创建，与工具级共存于 self.breakers）。"""
        return self.breakers.setdefault(source, CircuitBreaker(source))

    def seed_from_health(self, health: dict) -> list:
        """把 bootstrap.health_check_data_sources() 的结果喂给熔断器初始态。

        库缺失或 HTTP 探活失败的源初始即摘除（如东财连接中断、雅虎 403），
        300 秒后由熔断器半开探活自动恢复。返回被初始摘除的源名列表。
        """
        seeded = []
        libs = (health or {}).get("libraries", {})
        for lib, source in (("akshare", "akshare"), ("yfinance", "yfinance")):
            if libs.get(lib, {}).get("status") == "missing":
                self.seed_breaker(source, "open")
                seeded.append(source)
        http = (health or {}).get("http_sources", {})
        for key, source in (
            ("tencent_http", "tencent"), ("sina_http", "sina"),
            ("yahoo_http", "yahoo"), ("eastmoney_http", "eastmoney"),
        ):
            status = http.get(key, {}).get("status")
            if status is not None and status != "ok":
                self.seed_breaker(source, "open")
                seeded.append(source)
        if seeded:
            logger.info(f"数据源熔断器初始摘除: {seeded}")
        return seeded

    async def seed_from_bootstrap(self) -> list:
        """跑一次启动健康检查并喂给熔断器（服务启动时调用一次）。"""
        from ..core.bootstrap import health_check_data_sources
        health = await health_check_data_sources()
        return self.seed_from_health(health)

    # ----- 行情价格源级 fallback 链 -----

    async def _stock_price_chain(self, args: dict) -> str:
        """行情价格 fallback 链，收编 market_data*.py 的现有抓取函数：

        A股:  akshare -> 腾讯 -> 新浪
        US/HK: yfinance -> 腾讯 -> 雅虎
        （与 get_stock_price 的现有优先级一致；每源独立熔断，超时摘除）

        返回 JSON 字符串（与 _execute_tool 契约一致），成功时含 _source
        字段标明实际命中的数据源。缓存与 market_data 共享（SQLite），
        全部失败时回退过期缓存，行为与旧链路尾部一致。
        """
        from ..tools import market_data as md
        from ..tools import market_data_http as mdh

        symbol = str((args or {}).get("symbol", ""))
        resolved = md._resolve_company_name(symbol) or symbol
        market = md._detect_market(resolved)

        # 市场无法识别：回退 legacy 全链路（含搜索兜底），不改变怪异入参行为
        if market not in ("A_SHARE", "US", "HK"):
            legacy = await md.get_stock_price(symbol)
            return json.dumps(legacy, ensure_ascii=False, default=str)

        # 与 market_data 共享缓存：build_full_context 等直接调用方也能命中
        cached = md._cache.get("price", resolved, md.CACHE_TTL_REALTIME)
        if cached:
            return json.dumps({**cached, "_source": "cache"}, ensure_ascii=False, default=str)

        if market == "A_SHARE":
            providers = [
                ("akshare", lambda: md._fetch_a_share_price(resolved), 30.0),
                ("tencent", lambda: mdh.fetch_a_share_price_tencent(resolved), 8.0),
                ("sina", lambda: mdh.fetch_a_share_price_sina(resolved), 8.0),
            ]
        else:
            # yfinance 美股/港股在本环境频繁 curl 超时（生产日志实测），降权垫底；
            # 腾讯 qt.gtimg.cn 报价实测稳定，升为主源
            providers = [
                ("tencent", lambda: mdh.fetch_us_hk_price_tencent(resolved), 8.0),
                ("yahoo", lambda: mdh.fetch_us_hk_price_yahoo(resolved), 10.0),
                ("yfinance", lambda: md._fetch_yf_price(resolved, market), 12.0),
            ]

        errors = []
        for source, fetch, timeout_s in providers:
            br = self._source_breaker(source)
            if not br.allow_call():
                errors.append(f"{source}: circuit open")
                logger.info(f"行情源 {source} 熔断中，跳过（{resolved}）")
                continue
            try:
                data = await asyncio.wait_for(fetch(), timeout=timeout_s)
            except asyncio.TimeoutError:
                if source in ("akshare", "yfinance"):
                    # 同步 SDK 超时会在 mkt-sync 线程池留下孤儿线程，计入遥测
                    md._record_sync_timeout(source)
                br.record_failure(f"timeout>{timeout_s}s")
                errors.append(f"{source}: timeout>{timeout_s}s")
                logger.warning(f"行情源 {source} 超时（{resolved}，>{timeout_s}s）")
                continue
            except Exception as e:
                br.record_failure(str(e)[:120])
                errors.append(f"{source}: {str(e)[:80]}")
                logger.warning(f"行情源 {source} 失败（{resolved}）: {e}")
                continue
            if data and not data.get("error"):
                br.record_success()
                data["_source"] = source
                try:
                    md._cache.set("price", resolved, data)
                except Exception:
                    pass
                return json.dumps(data, ensure_ascii=False, default=str)
            br.record_failure("empty result")
            errors.append(f"{source}: empty")

        # 全部源失败 -> 过期缓存兜底（与旧 get_stock_price 尾部一致）
        stale = md._cache.get_stale("price", resolved)
        if stale:
            payload = dict(stale["data"])
            payload["_stale"] = True
            payload["_source"] = "stale_cache"
            return json.dumps(payload, ensure_ascii=False, default=str)
        return json.dumps({
            "symbol": resolved,
            "error": f"未获取到 {resolved} 的行情数据（所有数据源均失败）",
            "hint": "可能是数据源不可用且未安装 akshare/yfinance",
            "errors": errors,
            "timestamp": datetime.now().isoformat(),
        }, ensure_ascii=False, default=str)

    # ----- 财务报表源级 fallback 链 -----

    async def _financial_data_chain(self, args: dict) -> str:
        """财务报表 fallback 链（与 _stock_price_chain 同模式）：

        A股:   akshare（新浪报表系）
        US:    东财数据中心(akshare datacenter) -> yfinance(12s 降权)
        HK:    东财数据中心(akshare datacenter) -> yfinance(12s 降权)
        （东财 push2 行情接口本环境被连接重置，但 datacenter 财务接口实测
        <1s 可达且数据完整；yfinance 财务接口实测 57s 超时，仅作回退）

        每源独立熔断（东财财务用独立 breaker 名 eastmoney_dc，不与
        push2 行情的 eastmoney 混用——两者可达性不同）。
        """
        from ..tools import market_data as md

        symbol = str((args or {}).get("symbol", ""))
        resolved = md._resolve_company_name(symbol) or symbol
        market = md._detect_market(resolved)

        # 市场无法识别：回退 legacy 全链路（含搜索兜底），不改变怪异入参行为
        if market not in ("A_SHARE", "US", "HK"):
            legacy = await md.get_financial_data(symbol)
            return json.dumps(legacy, ensure_ascii=False, default=str)

        # 与 market_data 共享缓存（routers/data.py 等直接调用方也能命中）
        cached = md._cache.get("financials", resolved, md.CACHE_TTL_FINANCIAL)
        if cached:
            return json.dumps({**cached, "_source": "cache"}, ensure_ascii=False, default=str)

        if market == "A_SHARE":
            providers = [
                ("akshare", lambda: md._fetch_a_share_financials(resolved), 30.0),
            ]
        elif market == "US":
            providers = [
                ("eastmoney_dc", lambda: md._fetch_em_us_financials(resolved), 15.0),
                ("yfinance", lambda: md._fetch_yf_financials(resolved, market), 12.0),
            ]
        else:  # HK
            providers = [
                ("eastmoney_dc", lambda: md._fetch_em_hk_financials(resolved), 15.0),
                ("yfinance", lambda: md._fetch_yf_financials(resolved, market), 12.0),
            ]

        errors = []
        for source, fetch, timeout_s in providers:
            br = self._source_breaker(source)
            if not br.allow_call():
                errors.append(f"{source}: circuit open")
                logger.info(f"财务源 {source} 熔断中，跳过（{resolved}）")
                continue
            try:
                data = await asyncio.wait_for(fetch(), timeout=timeout_s)
            except asyncio.TimeoutError:
                if source in ("akshare", "yfinance"):
                    # 同步 SDK 超时会在 mkt-sync 线程池留下孤儿线程，计入遥测
                    md._record_sync_timeout(source)
                br.record_failure(f"timeout>{timeout_s}s")
                errors.append(f"{source}: timeout>{timeout_s}s")
                logger.warning(f"财务源 {source} 超时（{resolved}，>{timeout_s}s）")
                continue
            except Exception as e:
                br.record_failure(str(e)[:120])
                errors.append(f"{source}: {str(e)[:80]}")
                logger.warning(f"财务源 {source} 失败（{resolved}）: {e}")
                continue
            if data and not data.get("error"):
                br.record_success()
                data["_source"] = source
                try:
                    md._cache.set("financials", resolved, data)
                except Exception:
                    pass
                return json.dumps(data, ensure_ascii=False, default=str)
            br.record_failure("empty result")
            errors.append(f"{source}: empty")

        # 全部源失败 -> 过期缓存兜底（与旧 get_financial_data 尾部一致）
        stale = md._cache.get_stale("financials", resolved)
        if stale:
            payload = dict(stale["data"])
            payload["_stale"] = True
            payload["_source"] = "stale_cache"
            return json.dumps(payload, ensure_ascii=False, default=str)
        return json.dumps({
            "symbol": resolved,
            "error": f"未获取到 {resolved} 的财务数据（所有数据源均失败）",
            "hint": "可能是数据源不可用且未安装 akshare/yfinance",
            "errors": errors,
            "timestamp": datetime.now().isoformat(),
        }, ensure_ascii=False, default=str)

    # ----- execution -----

    async def call(
        self,
        tool: str,
        args: dict,
        task_id: Optional[str] = None,
        agent: Optional[str] = None,
        bus=None,
    ) -> ToolResult:
        result = await self._call_chain(tool, args, depth=0, seen=set())
        if bus is not None and task_id:
            from .events import EventType
            payload = {
                "tool": tool,
                "args": {k: (str(v)[:80]) for k, v in (args or {}).items()},
                "agent": agent or "",
                "source": result.source,
                "latency_ms": round(result.latency_ms, 1),
                "cache_hit": result.cache_hit,
                "fallback_depth": result.fallback_depth,
                "circuit": result.circuit,
                "ok": result.ok,
            }
            bus.emit(task_id, EventType.TOOL_CALL, **payload)
        return result

    async def _call_chain(self, tool: str, args: dict, depth: int, seen: set) -> ToolResult:
        if tool in seen:
            return ToolResult(tool, False, json.dumps({"error": f"fallback loop at {tool}"}, ensure_ascii=False), error="fallback loop")
        seen.add(tool)

        tdef = self.tools.get(tool)
        if tdef is None:
            return ToolResult(tool, False, json.dumps({"error": f"Unknown tool: {tool}"}, ensure_ascii=False), error="unknown tool")

        breaker = self.breakers.setdefault(tool, CircuitBreaker(tool))

        # Playback (eval replay) short-circuits everything
        if self.playback is not None:
            key = self._cache_key(tool, args)
            if key in self.playback:
                return ToolResult(tool, True, self.playback[key], source="cassette", cache_hit=True, fallback_depth=depth)
            return ToolResult(tool, False, json.dumps({"error": f"no cassette for {key}"}, ensure_ascii=False), error="no cassette", fallback_depth=depth)

        # Circuit check → fallback or reject
        if not breaker.allow_call():
            fb = await self._try_fallbacks(tdef, args, depth, seen)
            if fb is not None:
                return fb
            return ToolResult(
                tool, False,
                json.dumps({"error": f"circuit open for {tool}", "circuit": "open"}, ensure_ascii=False),
                circuit="open", error="circuit open", fallback_depth=depth,
            )

        # Cache (sqlite I/O offloaded so the event loop is never stalled)
        cache_key = self._cache_key(tool, args)
        if tdef.cache_ttl:
            try:
                cached = await asyncio.to_thread(
                    self._get_cache().get, "gateway", cache_key, max_age_seconds=tdef.cache_ttl,
                )
                if cached is not None:
                    return ToolResult(tool, True, cached if isinstance(cached, str) else json.dumps(cached, ensure_ascii=False),
                                      source="cache", cache_hit=True, fallback_depth=depth)
            except Exception:
                pass

        # Execute with timeout/retry
        start = time.monotonic()
        last_error = ""
        for attempt in range(1 + tdef.retries):
            try:
                if tdef.fn is not None:
                    content = await asyncio.wait_for(tdef.fn(args), timeout=tdef.timeout)
                else:
                    content = await asyncio.wait_for(_execute_tool(tool, args), timeout=tdef.timeout)
                latency = (time.monotonic() - start) * 1000
                ok = True
                source = ""
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        if "error" in parsed:
                            ok = False
                            last_error = str(parsed.get("error"))[:200]
                        source = str(parsed.get("_source", ""))[:40]
                except Exception:
                    pass
                if ok:
                    breaker.record_success()
                    if tdef.cache_ttl:
                        try:
                            await asyncio.to_thread(self._get_cache().set, "gateway", cache_key, content)
                        except Exception:
                            pass
                    return ToolResult(tool, True, content, source=source, latency_ms=latency, fallback_depth=depth)
                # E5: business errors (unknown symbol / no such data) are
                # legitimate answers, not provider failures — no breaker hit.
                if not _is_business_error(last_error):
                    breaker.record_failure(last_error)
            except asyncio.TimeoutError:
                last_error = f"timeout after {tdef.timeout}s"
                breaker.record_failure(last_error)
            except Exception as e:
                last_error = str(e)[:200]
                breaker.record_failure(last_error)
            if attempt < tdef.retries:
                await asyncio.sleep(0.5)

        latency = (time.monotonic() - start) * 1000
        fb = await self._try_fallbacks(tdef, args, depth, seen)
        if fb is not None:
            return fb
        return ToolResult(
            tool, False,
            json.dumps({"error": last_error or "tool failed"}, ensure_ascii=False),
            latency_ms=latency, circuit=breaker.state, error=last_error, fallback_depth=depth,
        )

    async def _try_fallbacks(self, tdef: ToolDef, args: dict, depth: int, seen: set) -> Optional[ToolResult]:
        for fb_name in tdef.fallback:
            fb = await self._call_chain(fb_name, args, depth + 1, seen)
            if fb.ok:
                return fb
        return None

    async def execute(self, tool_name: str, arguments: dict,
                      task_id: Optional[str] = None, agent: Optional[str] = None, bus=None) -> str:
        """Drop-in replacement for _execute_tool (returns JSON string)."""
        result = await self.call(tool_name, arguments, task_id=task_id, agent=agent, bus=bus)
        return result.content


_gateway: Optional[ToolGateway] = None


def get_gateway() -> ToolGateway:
    global _gateway
    if _gateway is None:
        _gateway = ToolGateway()
    return _gateway

