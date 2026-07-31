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


# ==================== ToolGateway (PR-3) ====================

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


@dataclass
class ToolDef:
    """A registered tool with execution policy."""
    name: str
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


class CircuitBreaker:
    """Per-tool circuit breaker.

    - closed: normal; consecutive failures increment a counter
    - open (after ``fail_threshold`` consecutive failures): calls are
      rejected immediately for ``recovery_probe_after`` seconds
    - half-open: after the cooldown a single probe call is allowed;
      success closes the breaker, failure re-opens it
    """

    def __init__(self, name: str, fail_threshold: int = 3, recovery_probe_after: float = 300):
        self.name = name
        self.fail_threshold = fail_threshold
        self.recovery_probe_after = recovery_probe_after
        self.state = "closed"
        self.consecutive_failures = 0
        self.opened_at: Optional[float] = None
        self.last_error: str = ""

    def allow_call(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.opened_at is not None and (time.monotonic() - self.opened_at) >= self.recovery_probe_after:
                self.state = "half_open"   # allow one probe
                return True
            return False
        # half_open: a probe is already in flight conceptually; allow it
        return True

    def record_success(self):
        self.state = "closed"
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = ""

    def record_failure(self, error: str = ""):
        self.consecutive_failures += 1
        self.last_error = error[:200]
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
}


class ToolGateway:
    """Unified tool entry: circuit check → cache → timeout/retry →
    fallback chain → TOOL_CALL event.

    Playback mode (eval replay): when ``playback`` is set, matching
    cassette entries are returned instead of executing real tools.
    """

    def __init__(self, tools: Optional[dict[str, ToolDef]] = None):
        self.tools = tools or TOOL_DEFS
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

        # Cache
        cache_key = self._cache_key(tool, args)
        if tdef.cache_ttl:
            try:
                cached = self._get_cache().get("gateway", cache_key, max_age_seconds=tdef.cache_ttl)
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
                content = await asyncio.wait_for(_execute_tool(tool, args), timeout=tdef.timeout)
                latency = (time.monotonic() - start) * 1000
                ok = True
                source = ""
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        if "error" in parsed and len(parsed) <= 2:
                            ok = False
                            last_error = str(parsed.get("error"))[:200]
                        source = str(parsed.get("_source", ""))[:40]
                except Exception:
                    pass
                if ok:
                    breaker.record_success()
                    if tdef.cache_ttl:
                        try:
                            self._get_cache().set("gateway", cache_key, content)
                        except Exception:
                            pass
                    return ToolResult(tool, True, content, source=source, latency_ms=latency, fallback_depth=depth)
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

