# -*- coding: utf-8 -*-
"""搜索相关性守卫批次评分制测试（离线，monkeypatch 各源）"""
import asyncio

import app.tools.web_search as ws


def _poisoned_batch():
    """模拟 Bing 反爬投毒：整批字典词条，仅一条含查询词"""
    return [
        {"title": "拼（汉语文字）_百度百科", "url": "https://x/1", "snippet": "拼，拼音pin，部首扌，笔画9"},
        {"title": "拼的意思,拼的解释", "url": "https://x/2", "snippet": "拼的拼音,拼的部首,拼的笔顺"},
        {"title": "多（汉语文字）", "url": "https://x/3", "snippet": "多，拼音duo"},
        {"title": "财报（汉语词语）", "url": "https://x/4", "snippet": "财报的解释"},
        {"title": "拼多多官网", "url": "https://x/5", "snippet": "拼多多 新电商开创者"},
    ]


def _good_batch():
    return [
        {"title": "拼多多第四季度营收1106亿元", "url": "https://x/a", "snippet": "拼多多第四季度营收1106.101亿元，净利润274.466亿元"},
        {"title": "拼多多发布全年财报：营收3938亿元", "url": "https://x/b", "snippet": "拼多多财报营收净利润数据"},
    ]


Q = "拼多多 2025年第四季度 财报 营收 净利润"
ALL_SOURCES = ("_sogou_search", "_so360_search", "_baidu_search",
               "_bing_search", "_toutiao_search", "_ddg_search")


def test_poisoned_bing_batch_rejected_and_fallback(monkeypatch):
    """Bing 投毒批次（5 条仅 1 条沾边，命中率 20%<50%）被拒，落到下一个健康源"""
    async def poisoned(q, n):
        return _poisoned_batch()
    async def good(q, n):
        return _good_batch()
    async def empty(q, n):
        return []
    monkeypatch.setattr(ws, "_sogou_search", empty)
    monkeypatch.setattr(ws, "_so360_search", empty)
    monkeypatch.setattr(ws, "_baidu_search", poisoned)   # 投毒
    monkeypatch.setattr(ws, "_bing_search", good)        # 健康
    monkeypatch.setattr(ws, "_toutiao_search", empty)
    monkeypatch.setattr(ws, "_ddg_search", empty)
    r = asyncio.run(ws.web_search(Q))
    assert r and r[0]["url"] == "https://x/a"
    assert not r[0].get("_placeholder")


def test_legit_batch_accepted_from_first_source(monkeypatch):
    """合法批次（命中率 100%）首源直接放行"""
    async def good(q, n):
        return _good_batch()
    async def boom(q, n):
        raise AssertionError("不应继续回退")
    monkeypatch.setattr(ws, "_sogou_search", good)
    for fn in ("_so360_search", "_baidu_search", "_bing_search", "_toutiao_search", "_ddg_search"):
        monkeypatch.setattr(ws, fn, boom)
    r = asyncio.run(ws.web_search(Q))
    assert r and r[0]["url"] == "https://x/a"


def test_all_sources_poisoned_gives_placeholder(monkeypatch):
    """全源投毒/为空 → placeholder（不抛异常）"""
    async def poisoned(q, n):
        return _poisoned_batch()
    async def empty(q, n):
        return []
    monkeypatch.setattr(ws, "_sogou_search", poisoned)
    monkeypatch.setattr(ws, "_so360_search", poisoned)
    monkeypatch.setattr(ws, "_baidu_search", poisoned)
    monkeypatch.setattr(ws, "_bing_search", poisoned)
    monkeypatch.setattr(ws, "_toutiao_search", poisoned)
    monkeypatch.setattr(ws, "_ddg_search", empty)
    r = asyncio.run(ws.web_search(Q))
    assert len(r) == 1 and r[0].get("_placeholder") is True


def test_query_echo_title_only_counts_snippet(monkeypatch):
    """标题整串回声查询的结果不计为命中（除非摘要独立命中）"""
    echo_batch = [
        {"title": "拼多多 2025年第四季度 财报 营收 净利润-视频", "url": "https://x/1", "snippet": "精彩视频"},
        {"title": "拼多多 2025年第四季度 财报 营收 净利润", "url": "https://x/2", "snippet": "大家都在搜"},
        {"title": "拼多多 2025年第四季度 财报 营收 净利润_直播", "url": "https://x/3", "snippet": "直播回放"},
    ]
    async def echo(q, n):
        return echo_batch
    async def good(q, n):
        return _good_batch()
    async def empty(q, n):
        return []
    monkeypatch.setattr(ws, "_sogou_search", echo)   # 全回声 → 0% 命中
    monkeypatch.setattr(ws, "_so360_search", good)
    for fn in ("_baidu_search", "_bing_search", "_toutiao_search", "_ddg_search"):
        monkeypatch.setattr(ws, fn, empty)
    r = asyncio.run(ws.web_search(Q))
    assert r and r[0]["url"] == "https://x/a"


def test_two_token_requirement(monkeypatch):
    """单 token 沾边（如仅含'腾讯'的门户页）不足以判命中"""
    junk = [
        {"title": "腾讯网", "url": "https://x/1", "snippet": "腾讯首页"},
        {"title": "腾讯视频", "url": "https://x/2", "snippet": "海量高清视频"},
        {"title": "腾讯体育", "url": "https://x/3", "snippet": "NBA 直播"},
    ]
    async def junk_fn(q, n):
        return junk
    async def good(q, n):
        return _good_batch()
    async def empty(q, n):
        return []
    monkeypatch.setattr(ws, "_sogou_search", junk_fn)
    monkeypatch.setattr(ws, "_so360_search", good)
    for fn in ("_baidu_search", "_bing_search", "_toutiao_search", "_ddg_search"):
        monkeypatch.setattr(ws, fn, empty)
    r = asyncio.run(ws.web_search("腾讯 2025年报 营收 净利润"))
    assert r and r[0]["url"] == "https://x/a"
