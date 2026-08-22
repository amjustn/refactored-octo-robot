# -*- coding: utf-8 -*-
"""get_financial_data 源级 fallback 链测试（离线，mock 各数据源）"""
import asyncio
import json

import pytest

import app.tools.market_data as md
from app.harness.tools import ToolGateway


class _FakeCache:
    """禁用缓存的内存替身：get 永远未命中，get_stale 可控"""
    def __init__(self, stale=None):
        self._stale = stale
        self.writes = []

    def get(self, category, key, max_age_seconds=3600):
        return None

    def set(self, category, key, value):
        self.writes.append((category, key))

    def get_stale(self, category, key):
        return self._stale


EM_US_PAYLOAD = {
    "symbol": "PDD", "currency": "CNY",
    "annual": [{"report_date": "2025-12-31", "revenue_yi": 4318.46}],
    "quarterly": [{"report_date": "2025-12-31", "revenue_yi": 1239.12}],
}
YF_PAYLOAD = {"quarterly_income": {"Total Revenue": {"2025-12-31": 123912194000}}}


def _gw(monkeypatch, stale=None):
    gw = ToolGateway()
    monkeypatch.setattr(md, "_cache", _FakeCache(stale=stale))
    # 网关自身也有 sqlite 缓存（可能残留冒烟测试写入），一并隔离
    monkeypatch.setattr(gw, "_get_cache", lambda: _FakeCache())
    return gw


def test_us_em_success_short_circuits(monkeypatch):
    """东财美股成功 → 直接返回，不触及 yfinance"""
    async def em(symbol):
        return dict(EM_US_PAYLOAD)

    async def yf(symbol, market):
        raise AssertionError("yfinance 不应被调用")

    monkeypatch.setattr(md, "_fetch_em_us_financials", em)
    monkeypatch.setattr(md, "_fetch_yf_financials", yf)
    gw = _gw(monkeypatch)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "PDD"}))
    d = json.loads(r.content)
    assert r.ok and d["_source"] == "eastmoney_dc"
    assert d["quarterly"][0]["revenue_yi"] == 1239.12
    assert gw.breakers["eastmoney_dc"].state == "closed"


def test_hk_em_success(monkeypatch):
    """港股走东财 hk_report 链路"""
    async def em(symbol):
        return {"symbol": symbol, "annual_income": [
            {"report_date": "2025-12-31", "revenue_yi": 7517.66, "net_profit_parent_yi": 2248.42}]}

    monkeypatch.setattr(md, "_fetch_em_hk_financials", em)
    gw = _gw(monkeypatch)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "0700.HK"}))
    d = json.loads(r.content)
    assert r.ok and d["_source"] == "eastmoney_dc"
    assert d["annual_income"][0]["net_profit_parent_yi"] == 2248.42


def test_us_fallback_to_yfinance_when_em_fails(monkeypatch):
    """东财异常 → 快速回退 yfinance，东财计入熔断失败"""
    async def em(symbol):
        raise ConnectionError("connection reset")

    async def yf(symbol, market):
        return dict(YF_PAYLOAD)

    monkeypatch.setattr(md, "_fetch_em_us_financials", em)
    monkeypatch.setattr(md, "_fetch_yf_financials", yf)
    gw = _gw(monkeypatch)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "PDD"}))
    d = json.loads(r.content)
    assert r.ok and d["_source"] == "yfinance"
    assert gw.breakers["eastmoney_dc"].consecutive_failures == 1


def test_us_fallback_when_em_timeout(monkeypatch):
    """东财超时 → 回退 yfinance；超时计入熔断"""
    async def em(symbol):
        raise asyncio.TimeoutError()

    async def yf(symbol, market):
        return dict(YF_PAYLOAD)

    monkeypatch.setattr(md, "_fetch_em_us_financials", em)
    monkeypatch.setattr(md, "_fetch_yf_financials", yf)
    gw = _gw(monkeypatch)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "PDD"}))
    d = json.loads(r.content)
    assert r.ok and d["_source"] == "yfinance"


def test_all_fail_with_stale_cache(monkeypatch):
    """全源失败 + 过期缓存 → stale 兜底"""
    async def em(symbol):
        return None

    async def yf(symbol, market):
        raise asyncio.TimeoutError()

    stale = {"data": {"symbol": "PDD", "annual": [{"revenue_yi": 3938.0}]}}
    monkeypatch.setattr(md, "_fetch_em_us_financials", em)
    monkeypatch.setattr(md, "_fetch_yf_financials", yf)
    gw = _gw(monkeypatch, stale=stale)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "PDD"}))
    d = json.loads(r.content)
    assert d.get("_stale") is True and d["_source"] == "stale_cache"


def test_all_fail_no_cache_returns_error(monkeypatch):
    """全源失败且无缓存 → 结构化 error（含各源失败原因）"""
    async def em(symbol):
        return None

    async def yf(symbol, market):
        raise ConnectionError("boom")

    monkeypatch.setattr(md, "_fetch_em_us_financials", em)
    monkeypatch.setattr(md, "_fetch_yf_financials", yf)
    gw = _gw(monkeypatch)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "PDD"}))
    d = json.loads(r.content)
    # 网关契约：工具返回 {"error": ...} 会被判失败并包装为 {"error": 消息}
    assert not r.ok and "error" in d and "未获取到" in d["error"]


def test_circuit_open_skips_source(monkeypatch):
    """东财熔断开启 → 跳过直接走 yfinance"""
    async def em(symbol):
        raise AssertionError("熔断中不应调用")

    async def yf(symbol, market):
        return dict(YF_PAYLOAD)

    monkeypatch.setattr(md, "_fetch_em_us_financials", em)
    monkeypatch.setattr(md, "_fetch_yf_financials", yf)
    gw = _gw(monkeypatch)
    gw.seed_breaker("eastmoney_dc", "open", failures=3)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "PDD"}))
    d = json.loads(r.content)
    assert r.ok and d["_source"] == "yfinance"


def test_unknown_market_falls_back_to_legacy(monkeypatch):
    """无法识别的入参 → legacy get_financial_data（含搜索兜底），不崩"""
    async def legacy(symbol):
        return {"symbol": symbol, "error": f"无法识别股票代码: {symbol}"}

    monkeypatch.setattr(md, "get_financial_data", legacy)
    gw = _gw(monkeypatch)
    r = asyncio.run(gw.call("get_financial_data", {"symbol": "某某公司"}))
    d = json.loads(r.content)
    assert "无法识别" in d["error"]
