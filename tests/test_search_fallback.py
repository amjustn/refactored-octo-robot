# -*- coding: utf-8 -*-
"""搜索多源回退链测试(离线,monkeypatch)"""
import asyncio
import pytest

import app.tools.web_search as ws


class _Resp:
    def __init__(self, text, code=200):
        self.text = text
        self.status_code = code


SOGOU_HTML = '''
<html><body>
<div class="vrwrap"><h3><a href="https://example.com/a">宁德时代海外扩张分析</a></h3>
<div class="fz-mid">宁德时代欧洲工厂产能爬坡…</div></div>
<div class="vrwrap"><h3><a href="https://example.com/b">第二条结果</a></h3>
<div class="fz-mid">摘要乙</div></div>
</body></html>'''

SO360_HTML = '''
<html><body>
<div class="res-list"><h3><a href="https://example.com/c">360结果甲</a></h3>
<div class="res-desc">360摘要甲</div></div>
</body></html>'''


def test_fallback_to_sogou_when_bing_empty(monkeypatch):
    """首源无结果 → 回退到下一个源并按序返回（批次守卫见 test_search_batch_guard）"""
    async def empty(q, n):
        return []
    async def fake_360(q, n):
        return [{"title": "test query 相关标题", "url": "https://example.com/a",
                 "snippet": "test query 摘要"}]
    monkeypatch.setattr(ws, "_sogou_search", empty)
    monkeypatch.setattr(ws, "_so360_search", fake_360)
    for fn in ("_baidu_search", "_bing_search", "_toutiao_search", "_ddg_search"):
        monkeypatch.setattr(ws, fn, empty)
    r = asyncio.run(ws.web_search("test query"))
    assert r and r[0]["url"] == "https://example.com/a"


def test_sogou_parser(monkeypatch):
    """搜狗 HTML 解析:标题/链接/摘要齐全"""
    class FakeClient:
        async def get(self, url, params=None, headers=None):
            return _Resp(SOGOU_HTML)
    async def fake_client():
        return FakeClient()
    monkeypatch.setattr(ws, "_get_client", fake_client)
    r = asyncio.run(ws._sogou_search("宁德时代"))
    assert len(r) == 2
    assert r[0]["title"] == "宁德时代海外扩张分析"
    assert r[0]["snippet"].startswith("宁德时代")


def test_so360_parser(monkeypatch):
    """360 HTML 解析"""
    class FakeClient:
        async def get(self, url, params=None, headers=None):
            return _Resp(SO360_HTML)
    async def fake_client():
        return FakeClient()
    monkeypatch.setattr(ws, "_get_client", fake_client)
    r = asyncio.run(ws._so360_search("test"))
    assert len(r) == 1 and r[0]["title"] == "360结果甲"


def test_all_fail_gives_placeholder(monkeypatch):
    """全链失败 → 占位结果(不抛异常)"""
    async def boom(q, n):
        raise RuntimeError("network down")
    for fn in ("_sogou_search", "_so360_search", "_baidu_search",
               "_bing_search", "_toutiao_search", "_ddg_search"):
        monkeypatch.setattr(ws, fn, boom)
    r = asyncio.run(ws.web_search("test"))
    assert len(r) == 1 and r[0].get("_placeholder") is True
