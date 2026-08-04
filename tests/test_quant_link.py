"""Tests for app/tools/quant_link.py and its ToolGateway registration.

All tests use a fake QT_API_TOKEN via env and monkeypatched httpx client —
they never touch the real /home/q/quant_trader/data/api_token.txt and never
hit the network.
"""
import asyncio
import json

import httpx
import pytest

from app.tools import quant_link
from app.tools.quant_link import QuantLinkError
from app.harness import tools as harness_tools
from app.harness.tools import ALL_TOOL_SCHEMAS, TOOL_DEFS, TOOL_ENABLED_AGENTS, ToolGateway

FAKE_TOKEN = "qt_test_token_123"


class _FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def json(self):
        return self._payload


class _FakeClient:
    """Mimics the subset of httpx.AsyncClient used by quant_link."""

    def __init__(self, payload=None, status=200, exc=None):
        self._payload = payload
        self._status = status
        self._exc = exc
        self.calls = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._payload, self._status)


class _FakeCache:
    """In-memory stand-in for core.data_cache.DataCache."""

    def __init__(self):
        self.store = {}

    def get(self, category, key, max_age_seconds=3600):
        return self.store.get((category, key))

    def set(self, category, key, value):
        self.store[(category, key)] = value


SIGNAL_PAYLOAD = {
    "code": 0,
    "data": {
        "summary": {"buy": 1, "sell": 0, "hold": 0, "total": 1},
        "alerts": [{"code": "600519", "alert": "动量强势(12.6%)"}],
        "results": [{
            "code": "600519",
            "action": "buy",
            "confidence": 0.278,
            "score": 0.278,
            "reasons": ["动量向上(roc=0.126)", "均线多头排列(ma_cross=0.059)"],
            "alerts": ["动量强势(12.6%)"],
            "factors": {"roc_20": 0.125999, "rsi_14": 77.168175},
            "factor_trend": {"roc_20": "flat", "rsi_14": "up"},
            "factor_acceleration": {"roc_20": -0.003},
        }],
    },
}


@pytest.fixture
def qt_env(monkeypatch, tmp_path):
    """Fake token via env; token file pointed at a nonexistent tmp path."""
    monkeypatch.setenv("QT_API_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("QT_TOKEN_FILE", str(tmp_path / "no_such_token.txt"))
    monkeypatch.setenv("QT_BASE_URL", "http://127.0.0.1:8000")
    quant_link.reset_token_cache()
    yield
    quant_link.reset_token_cache()


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(quant_link, "get_client", lambda: _awaitable(client))


async def _return(client):
    return client


def _awaitable(client):
    return _return(client)


# ---------------------------------------------------------------- module tests

def test_factor_signal_success(qt_env, monkeypatch):
    client = _FakeClient(payload=SIGNAL_PAYLOAD)
    _patch_client(monkeypatch, client)

    result = asyncio.run(quant_link.get_factor_signal("600519"))

    assert result["_source"] == "quant_trader"
    assert result["summary"]["buy"] == 1
    row = result["results"][0]
    assert row["code"] == "600519"
    assert row["action"] == "buy"
    assert row["score"] == 0.278
    assert row["reasons"][0].startswith("动量向上")
    # factors rounded, acceleration trimmed for compactness
    assert row["factors"] == {"roc_20": 0.126, "rsi_14": 77.1682}
    assert "factor_acceleration" not in row
    # request went to the right endpoint with Bearer auth
    call = client.calls[0]
    assert call["url"].endswith("/api/factors/signal")
    assert call["params"] == {"codes": "600519"}
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_factor_signal_requires_codes(qt_env):
    with pytest.raises(QuantLinkError, match="股票代码"):
        asyncio.run(quant_link.get_factor_signal("  "))


def test_qt_down_connect_error(qt_env, monkeypatch):
    client = _FakeClient(exc=httpx.ConnectError("Connection refused"))
    _patch_client(monkeypatch, client)

    with pytest.raises(QuantLinkError, match="不可达"):
        asyncio.run(quant_link.get_market_scores())


def test_401_token_error(qt_env, monkeypatch):
    client = _FakeClient(payload={"code": 401, "message": "未授权"}, status=401)
    _patch_client(monkeypatch, client)

    with pytest.raises(QuantLinkError, match="401"):
        asyncio.run(quant_link.get_daily_review())
    # token cache must be invalidated so the next call re-resolves
    assert quant_link._TOKEN is None


def test_missing_token(monkeypatch, tmp_path):
    monkeypatch.delenv("QT_API_TOKEN", raising=False)
    monkeypatch.setenv("QT_TOKEN_FILE", str(tmp_path / "no_such_token.txt"))
    quant_link.reset_token_cache()
    # /api/auth/token fallback also fails (QT down)
    _patch_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("down")))

    with pytest.raises(QuantLinkError, match="Token"):
        asyncio.run(quant_link.get_bottom_thermometer())
    quant_link.reset_token_cache()


def test_qt_error_code_raises(qt_env, monkeypatch):
    payload = {"code": 1, "message": "底部温度计数据获取超时或失败: boom"}
    _patch_client(monkeypatch, _FakeClient(payload=payload))

    with pytest.raises(QuantLinkError, match="boom"):
        asyncio.run(quant_link.get_bottom_thermometer())


# ---------------------------------------------------------------- gateway tests

def test_schemas_and_tooldefs_registered():
    names = {s["function"]["name"] for s in ALL_TOOL_SCHEMAS}
    for tool in ("quant_factor_signal", "quant_market_scores",
                 "quant_bottom_thermometer", "quant_daily_review"):
        assert tool in names, f"{tool} schema missing"
        assert tool in TOOL_DEFS, f"{tool} ToolDef missing"
    assert TOOL_DEFS["quant_factor_signal"].cache_ttl >= 300
    assert TOOL_DEFS["quant_daily_review"].cache_ttl >= 3600
    # research agents incl. the 7 macro roles are tool-enabled
    for role in ("macro-institutional-economist", "macro-forecaster", "macro-strategist",
                 "cn-policy-framework", "cn-data-cycle", "cn-fx-external", "cn-structure-trend"):
        assert role in TOOL_ENABLED_AGENTS


def test_gateway_dispatch(qt_env, monkeypatch):
    client = _FakeClient(payload=SIGNAL_PAYLOAD)
    _patch_client(monkeypatch, client)

    gw = ToolGateway()
    gw._cache = _FakeCache()
    out = asyncio.run(gw.execute("quant_factor_signal", {"codes": "600519"}))

    parsed = json.loads(out)
    assert parsed["results"][0]["code"] == "600519"
    assert "error" not in parsed


def test_gateway_dispatch_qt_down_returns_error(qt_env, monkeypatch):
    _patch_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("down")))

    gw = ToolGateway()
    gw._cache = _FakeCache()
    result = asyncio.run(gw.call("quant_daily_review", {}))

    assert not result.ok
    assert "不可达" in json.loads(result.content)["error"]


def test_gateway_cache_ttl_factor_signal(qt_env, monkeypatch):
    client = _FakeClient(payload=SIGNAL_PAYLOAD)
    _patch_client(monkeypatch, client)

    gw = ToolGateway()
    gw._cache = _FakeCache()
    args = {"codes": "600519"}
    r1 = asyncio.run(gw.call("quant_factor_signal", args))
    r2 = asyncio.run(gw.call("quant_factor_signal", args))

    assert r1.ok and r2.ok
    assert not r1.cache_hit
    assert r2.cache_hit
    # underlying HTTP only hit once — 300s gateway cache short-circuited the 2nd call
    assert len(client.calls) == 1
