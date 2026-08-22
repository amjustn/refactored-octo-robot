"""Tests for the parse_goal result cache (app.core.goal_parser).

pytest-asyncio is not installed in this project, so async functions are
driven with asyncio.run() inside sync tests (same pattern as
tests/test_goal_parser.py).
"""
import asyncio
import json

import pytest

from app.core import goal_parser
from app.core.data_cache import DataCache
from app.core.goal_parser import parse_goal

LONG_GOAL = "请帮我深入分析腾讯控股2025年的护城河变化，重点关注游戏和广告业务的竞争格局"
OTHER_GOAL = "请帮我全面分析贵州茅台的估值水平，重点看高端白酒的需求趋势和提价空间"
VALID_PAYLOAD = {
    "symbol": "0700.HK",
    "company": "腾讯控股",
    "search_query": "腾讯控股 护城河 游戏 广告 竞争格局",
    "keywords": ["腾讯", "护城河", "游戏", "广告"],
    "intent": "分析腾讯2025年护城河变化",
}


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path):
    """Point parse_goal's cache at a throwaway db so tests never pollute the real cache db."""
    monkeypatch.setattr(goal_parser, "_cache", DataCache(tmp_path / "goal_parse_cache_test.db"))


def _patch_llm(monkeypatch, response=None, exc=None):
    """Replace goal_parser.chat_complete; returns a call-counter list."""
    calls = []

    async def fake_chat_complete(system_prompt, user_message, model=None,
                                 temperature=0.7, llm_config=None):
        calls.append(1)
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(goal_parser, "chat_complete", fake_chat_complete)
    return calls


def test_second_call_same_args_uses_cache(monkeypatch):
    """(a) second call with same args does not invoke chat_complete again."""
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD, ensure_ascii=False))
    first = asyncio.run(parse_goal(LONG_GOAL))
    second = asyncio.run(parse_goal(LONG_GOAL))
    assert len(calls) == 1, "second call must be served from cache"
    assert first is not None
    assert second == first


def test_whitespace_variants_share_cache_entry(monkeypatch):
    """Normalization: leading/trailing whitespace hits the same cache key."""
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD, ensure_ascii=False))
    asyncio.run(parse_goal(LONG_GOAL))
    result = asyncio.run(parse_goal("  \n\t " + LONG_GOAL + "  \n "))
    assert len(calls) == 1, "whitespace-normalized goal must hit the cache"
    assert result is not None


def test_different_args_trigger_new_llm_call(monkeypatch):
    """(b) a different goal misses the cache and calls the LLM again."""
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD, ensure_ascii=False))
    asyncio.run(parse_goal(LONG_GOAL))
    asyncio.run(parse_goal(OTHER_GOAL))
    assert len(calls) == 2


def test_different_model_triggers_new_llm_call(monkeypatch):
    """Model name is part of the cache key; api key differences are not."""
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD, ensure_ascii=False))
    asyncio.run(parse_goal(LONG_GOAL, llm_config={"model": "model-a", "api_key": "k1"}))
    asyncio.run(parse_goal(LONG_GOAL, llm_config={"model": "model-a", "api_key": "k2"}))
    assert len(calls) == 1, "same model with different api key must hit the cache"
    asyncio.run(parse_goal(LONG_GOAL, llm_config={"model": "model-b", "api_key": "k1"}))
    assert len(calls) == 2, "different model must miss the cache"


def test_failed_parse_is_not_cached(monkeypatch):
    """(c) garbage LLM output returns None and is never cached — retry calls the LLM again."""
    calls = _patch_llm(monkeypatch, response="对不起，我无法理解这个问题，没有JSON可以给你")
    assert asyncio.run(parse_goal(LONG_GOAL)) is None
    assert asyncio.run(parse_goal(LONG_GOAL)) is None
    assert len(calls) == 2, "failed parses must not be cached"


def test_llm_exception_is_not_cached(monkeypatch):
    calls = _patch_llm(monkeypatch, exc=RuntimeError("connection reset"))
    assert asyncio.run(parse_goal(LONG_GOAL)) is None
    assert asyncio.run(parse_goal(LONG_GOAL)) is None
    assert len(calls) == 2


def test_short_input_never_touches_cache(monkeypatch):
    """(d) short inputs return before any cache access."""
    cache_get_calls = []

    def explode_get_cache():
        cache_get_calls.append(1)
        raise AssertionError("cache must not be touched for short input")

    monkeypatch.setattr(goal_parser, "_get_cache", explode_get_cache)
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD))
    assert asyncio.run(parse_goal("腾讯")) is None
    assert not cache_get_calls
    assert not calls


def test_cache_errors_never_break_parse(monkeypatch):
    """A broken cache (db locked etc.) degrades to a plain miss, both directions."""

    class BrokenCache:
        def get(self, *args, **kwargs):
            raise RuntimeError("database is locked")

        def set(self, *args, **kwargs):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(goal_parser, "_cache", BrokenCache())
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD, ensure_ascii=False))
    result = asyncio.run(parse_goal(LONG_GOAL))
    assert result is not None
    assert result["symbol"] == "0700.HK"
    assert len(calls) == 1
