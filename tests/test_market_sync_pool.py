#!/usr/bin/env python3
"""mkt-sync 有界线程池与挂起调用遥测测试。

覆盖：
- _run_sync 走专用执行器（线程名前缀 mkt-sync，而非事件循环默认执行器）
- 池容量上限：提交 2N 个阻塞任务，并发运行数不超过 N
- wait_for 超时递增 _sync_timeouts_total 并输出告警日志
- fetch_market_indices 主源超时路径真实触发遥测（打桩，无网络/无真实线程）
- /api/health/data-sources 响应包含 sync_pool 字段（加性，不破坏既有键）

Run: cd . && venv/bin/python -m pytest tests/test_market_sync_pool.py -v
"""
import asyncio
import os
import sys
import threading
import time

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, ".")

import pytest
from fastapi.testclient import TestClient

import app.tools.market_data as md
from app.core import config as _config
from app.main import app

client = TestClient(app)


def _auth_headers():
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        return {"Authorization": f"Bearer {create_token(_config.BERKSHIRE_API_TOKEN)}"}
    return {}


# ==================== 专用执行器 ====================

def test_executor_bounded_and_named():
    """模块级执行器：容量来自配置，线程名前缀 mkt-sync。"""
    assert md.SYNC_EXECUTOR._max_workers == _config.MARKET_SYNC_WORKERS


def test_run_sync_uses_dedicated_executor():
    """_run_sync 的任务运行在 mkt-sync 线程上（默认执行器线程名不含此前缀）。"""
    async def main():
        return await md._run_sync(lambda: threading.current_thread().name)

    name = asyncio.run(main())
    assert name.startswith("mkt-sync")


def test_pool_cap_respected():
    """提交 2N 个阻塞任务，同时在跑的最多 N 个。"""
    n = _config.MARKET_SYNC_WORKERS
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return True

    async def main():
        return await asyncio.gather(*[md._run_sync(slow) for _ in range(2 * n)])

    results = asyncio.run(main())
    assert all(results)
    assert peak <= n


# ==================== 挂起调用遥测 ====================

def test_record_sync_timeout_increments_counter(monkeypatch, caplog):
    monkeypatch.setattr(md, "_sync_timeouts_total", 0)
    with caplog.at_level("WARNING", logger="ai_berkshire.market_data"):
        md._record_sync_timeout("akshare")
        md._record_sync_timeout("yfinance")
    assert md._sync_timeouts_total == 2
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "akshare" in warnings[0].getMessage()
    assert "yfinance" in warnings[1].getMessage()


class _NullCache:
    def get(self, *a, **k):
        return None

    def get_stale(self, *a, **k):
        return None

    def set(self, *a, **k):
        pass


def test_indices_timeout_path_records_telemetry(monkeypatch):
    """fetch_market_indices 主源 wait_for 超时 -> 计数器 +1（打桩，无网络）。"""
    monkeypatch.setattr(md, "_sync_timeouts_total", 0)
    monkeypatch.setattr(md, "_cache", _NullCache())

    async def _hang(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(md, "_run_sync", _hang)

    async def _empty():
        return {}

    # HTTP 回退源打桩为空，避免真实网络
    monkeypatch.setattr(md, "fetch_indices_tencent", _empty)
    monkeypatch.setattr(md, "fetch_indices_tencent_global", _empty)
    monkeypatch.setattr(md, "fetch_indices_yahoo", _empty)

    # 把硬编码的 8s 超时收紧到 0.2s（仅本测试内生效）
    real_wait_for = asyncio.wait_for

    async def _fast_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=min(timeout, 0.2))

    monkeypatch.setattr(asyncio, "wait_for", _fast_wait_for)

    asyncio.run(md.fetch_market_indices())
    assert md._sync_timeouts_total == 1


def test_sync_pool_stats_shape():
    stats = md.sync_pool_stats()
    assert stats["workers"] == _config.MARKET_SYNC_WORKERS
    assert isinstance(stats["queued_estimate"], int)
    assert stats["queued_estimate"] >= 0
    assert isinstance(stats["timeouts_total"], int)


# ==================== 健康检查端点 ====================

def test_health_endpoint_includes_sync_pool(monkeypatch):
    import app.core.bootstrap as bootstrap

    async def _fake_health():
        return {"libraries": {}, "http_sources": {}}

    monkeypatch.setattr(bootstrap, "health_check_data_sources", _fake_health)

    res = client.get("/api/health/data-sources", headers=_auth_headers())
    assert res.status_code == 200
    data = res.json()
    # 既有键不被破坏
    assert "libraries" in data
    assert "circuit_breakers" in data
    # 新增字段
    pool = data["sync_pool"]
    assert pool["workers"] == _config.MARKET_SYNC_WORKERS
    assert isinstance(pool["queued_estimate"], int)
    assert isinstance(pool["timeouts_total"], int)
