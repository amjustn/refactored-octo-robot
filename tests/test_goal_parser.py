"""Tests for app.core.goal_parser.parse_goal.

pytest-asyncio is not installed in this project, so async functions are
driven with asyncio.run() inside sync tests (same pattern as
tests/test_bug_fixes_round2.py).
"""
import asyncio
import json

import pytest

from app.core import goal_parser
from app.core.data_cache import DataCache
from app.core.goal_parser import parse_goal

LONG_GOAL = "请帮我深入分析腾讯控股2025年的护城河变化，重点关注游戏和广告业务的竞争格局"


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path):
    """Point parse_goal's cache at a throwaway db so tests never touch the real one."""
    monkeypatch.setattr(goal_parser, "_cache", DataCache(tmp_path / "goal_parse_test.db"))
VALID_PAYLOAD = {
    "symbol": "0700.HK",
    "company": "腾讯控股",
    "search_query": "腾讯控股 护城河 游戏 广告 竞争格局",
    "keywords": ["腾讯", "护城河", "游戏", "广告"],
    "intent": "分析腾讯2025年护城河变化",
}


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


def test_valid_json_returns_parsed_dict(monkeypatch):
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD, ensure_ascii=False))
    result = asyncio.run(parse_goal(LONG_GOAL))
    assert calls, "LLM should be called for long input"
    assert result is not None
    assert result["symbol"] == "0700.HK"
    assert result["company"] == "腾讯控股"
    assert result["search_query"] == VALID_PAYLOAD["search_query"]
    assert result["keywords"] == VALID_PAYLOAD["keywords"]
    assert result["intent"] == VALID_PAYLOAD["intent"]


def test_markdown_fenced_json_is_parsed(monkeypatch):
    fenced = "```json\n" + json.dumps(VALID_PAYLOAD, ensure_ascii=False) + "\n```"
    _patch_llm(monkeypatch, response=fenced)
    result = asyncio.run(parse_goal(LONG_GOAL))
    assert result is not None
    assert result["symbol"] == "0700.HK"


def test_garbage_response_returns_none(monkeypatch):
    _patch_llm(monkeypatch, response="对不起，我无法理解这个问题，没有JSON可以给你")
    result = asyncio.run(parse_goal(LONG_GOAL))
    assert result is None


def test_invalid_symbol_is_dropped(monkeypatch):
    payload = dict(VALID_PAYLOAD, symbol="NOT_A_SYMBOL!!")
    _patch_llm(monkeypatch, response=json.dumps(payload, ensure_ascii=False))
    result = asyncio.run(parse_goal(LONG_GOAL))
    assert result is not None
    assert result["symbol"] is None
    # company name survives even when the symbol is junk
    assert result["company"] == "腾讯控股"


def test_short_input_skips_llm(monkeypatch):
    calls = _patch_llm(monkeypatch, response=json.dumps(VALID_PAYLOAD))
    result = asyncio.run(parse_goal("腾讯"))
    assert result is None
    assert not calls, "short input must not trigger an LLM call"


def test_llm_exception_returns_none(monkeypatch):
    _patch_llm(monkeypatch, exc=RuntimeError("connection reset"))
    result = asyncio.run(parse_goal(LONG_GOAL))
    assert result is None


def test_all_null_payload_returns_none(monkeypatch):
    payload = {"symbol": None, "company": None, "search_query": None,
               "keywords": [], "intent": None}
    _patch_llm(monkeypatch, response=json.dumps(payload))
    result = asyncio.run(parse_goal(LONG_GOAL))
    assert result is None
