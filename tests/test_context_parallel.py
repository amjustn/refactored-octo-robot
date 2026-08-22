"""Tests for parallel data gathering in build_full_context (app/core/context.py).

Covers:
1. Wall time ≈ max(branches), not sum — mocked fetchers with artificial sleeps.
2. Parallel output is byte-identical to a serialized reference for fixed input.
3. One branch raising still yields the other branches' sections.

No pytest-asyncio — all async entry points go through asyncio.run.
"""
import asyncio
import time

import app.core.context as ctx_mod
import app.tools.market_data as md_mod
from app.core.context import build_full_context

FIXED_INDICES = {
    "sh": {"name": "上证指数", "price": 3123.45, "change_pct": 0.56},
    "timestamp": "2026-08-04 15:00:00",
}
FIXED_PRICE = {
    "name": "腾讯控股", "market": "HK", "currency": "HKD",
    "price": 380.0, "change_pct": 1.25, "pe": 25.3, "pb": 3.1,
}
FIXED_NEWS = [
    {"title": "腾讯发布最新财报", "date": "2026-08-01", "source": "测试源"},
]
FIXED_WEB = (
    "## 网络最新信息（自动搜索）\n*搜索关键词: 腾讯*\n\n"
    "### 搜索结果 (1 条)\n- [1] 示例新闻\n  来源: http://example.com\n---"
)
FIXED_TIME = (
    "## 当前时间上下文\n\n**当前日期**：2026年08月04日 周二\n"
    "**当前时间**：12:00:00\n**市场状态**：A股: 交易中\n"
)

ARGUMENTS = "分析腾讯"


def _patch_fetchers(monkeypatch, sleeps=None, lock=None, fail=()):
    """Replace all network fetch helpers with deterministic fakes.

    sleeps: per-helper artificial delay in seconds.
    lock:   optional asyncio.Lock — when given, every fake holds it while
            sleeping, forcing fully sequential execution (the reference).
    fail:   helper names that should raise RuntimeError.
    """
    sleeps = sleeps or {}

    async def _run(name):
        if lock is not None:
            async with lock:
                await asyncio.sleep(sleeps.get(name, 0.0))
        else:
            await asyncio.sleep(sleeps.get(name, 0.0))
        if name in fail:
            raise RuntimeError(f"{name} boom")

    async def fake_indices():
        await _run("indices")
        return FIXED_INDICES

    async def fake_price(symbol):
        await _run("price")
        return FIXED_PRICE

    async def fake_news(symbol, days=14):
        await _run("news")
        return FIXED_NEWS

    async def fake_web(**kwargs):
        await _run("web")
        return FIXED_WEB

    monkeypatch.setattr(md_mod, "fetch_market_indices", fake_indices)
    monkeypatch.setattr(md_mod, "get_stock_price", fake_price)
    monkeypatch.setattr(md_mod, "fetch_company_news", fake_news)
    # build_web_search_context is looked up as a module global of context.py
    monkeypatch.setattr(ctx_mod, "build_web_search_context", fake_web)
    # Deterministic time section so two runs can be compared byte-for-byte
    monkeypatch.setattr(ctx_mod, "build_time_context", lambda: FIXED_TIME)


def test_parallel_wall_time_is_max_not_sum(monkeypatch):
    """3 independent branches x 0.3s must finish in ~0.3s, not ~0.9s."""
    _patch_fetchers(monkeypatch, sleeps={"indices": 0.3, "price": 0.3, "web": 0.3})

    async def _run():
        start = time.perf_counter()
        ctx = await build_full_context(ARGUMENTS)
        return time.perf_counter() - start, ctx

    elapsed, ctx = asyncio.run(_run())

    assert elapsed < 0.6, (
        f"branches appear sequential: {elapsed:.2f}s >= 0.6s "
        f"(3 x 0.3s parallel should be ~0.3s)"
    )
    # Sanity: all three sections actually produced
    assert "最新市场指数" in ctx
    assert "实时行情" in ctx
    assert "网络最新信息" in ctx


def test_output_identical_to_sequential_reference(monkeypatch):
    """Parallel output must equal the serialized reference byte-for-byte."""
    sleeps = {"indices": 0.05, "price": 0.05, "news": 0.05, "web": 0.05}

    # Reference: every fetcher holds one global lock -> fully sequential,
    # same content the legacy serial implementation would produce.
    _patch_fetchers(monkeypatch, sleeps=sleeps, lock=asyncio.Lock())
    sequential_ctx = asyncio.run(build_full_context(ARGUMENTS))

    # Parallel: same fakes, no lock.
    _patch_fetchers(monkeypatch, sleeps=sleeps, lock=None)
    parallel_ctx = asyncio.run(build_full_context(ARGUMENTS))

    assert parallel_ctx == sequential_ctx

    # Sections appear in the legacy order: indices -> company -> web
    i_idx = parallel_ctx.index("最新市场指数")
    i_company = parallel_ctx.index("实时行情")
    i_web = parallel_ctx.index("网络最新信息")
    assert i_idx < i_company < i_web


def test_failing_branch_does_not_break_others(monkeypatch):
    """Each failing branch degrades to empty; other sections survive."""
    # Web search blows up -> indices + company still present, no web section
    _patch_fetchers(monkeypatch, fail=("web",))
    ctx = asyncio.run(build_full_context(ARGUMENTS))
    assert "最新市场指数" in ctx
    assert "实时行情" in ctx
    assert "网络最新信息" not in ctx

    # Indices blow up -> company + web still present
    _patch_fetchers(monkeypatch, fail=("indices",))
    ctx = asyncio.run(build_full_context(ARGUMENTS))
    assert "最新市场指数" not in ctx
    assert "实时行情" in ctx
    assert "网络最新信息" in ctx

    # Price blows up -> company section dropped, indices + web survive
    _patch_fetchers(monkeypatch, fail=("price",))
    ctx = asyncio.run(build_full_context(ARGUMENTS))
    assert "最新市场指数" in ctx
    assert "实时行情" not in ctx
    assert "网络最新信息" in ctx


def test_goal_hints_behavior_preserved(monkeypatch):
    """goal_hints still steer symbol resolution and the web search query."""
    captured = {}

    async def fake_web(**kwargs):
        captured.update(kwargs)
        return FIXED_WEB

    _patch_fetchers(monkeypatch)
    monkeypatch.setattr(ctx_mod, "build_web_search_context", fake_web)

    hints = {
        "symbol": "600519",
        "company": "",
        "search_query": "贵州茅台 2025 年报",
        "intent": "分析基本面",
    }
    ctx = asyncio.run(build_full_context("随便说点什么", goal_hints=hints))

    # Hint symbol drove the company section
    assert "600519" in ctx
    # Hint search_query reached the web search branch
    assert captured.get("search_query") == "贵州茅台 2025 年报"
    # Intent hint appended to the core instruction
    assert "用户意图：分析基本面" in ctx
