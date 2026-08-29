"""Tests for the follow-up Q&A API (/api/follow-up):
- backward-compatible response shape with only report+question
- multi-turn history is sanitized and passed to the LLM in order
- attachments are injected as a 参考资料 section (and shrink report context)
- malformed history entries are ignored
- oversized attachments are rejected (422)
"""
import os, sys
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, ".")

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.core import config as _config

client = TestClient(app)


def _auth_headers():
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        return {"Authorization": f"Bearer {create_token(_config.BERKSHIRE_API_TOKEN)}"}
    return {}


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main_module._rate_limiter.clear()
    yield
    main_module._rate_limiter.clear()


@pytest.fixture
def captured(monkeypatch):
    """Monkeypatch the LLM call and capture its kwargs."""
    calls = []

    async def fake_chat_complete(**kwargs):
        calls.append(kwargs)
        return "模拟回答"

    monkeypatch.setattr("app.core.llm.chat_complete", fake_chat_complete)
    return calls


def _payload(**kw):
    base = {"report": "这是一份关于伯克希尔的研究报告。" * 10, "question": "现金流如何？"}
    base.update(kw)
    return base


def test_followup_backward_compat(captured):
    res = client.post("/api/follow-up", json=_payload(), headers=_auth_headers())
    assert res.status_code == 200
    assert res.json() == {"answer": "模拟回答"}
    assert len(captured) == 1
    # No history passed when the client omits it
    assert captured[0]["history"] is None
    assert "## 用户上传的参考资料" not in captured[0]["user_message"]


def test_followup_history_passed_in_order(captured):
    history = [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "第一个回答"},
        {"role": "user", "content": "第二个问题"},
        {"role": "assistant", "content": "第二个回答"},
    ]
    res = client.post("/api/follow-up", json=_payload(history=history), headers=_auth_headers())
    assert res.status_code == 200
    assert captured[0]["history"] == history


def test_followup_history_malformed_entries_ignored(captured):
    history = [
        {"role": "user", "content": "有效问题"},
        {"role": "system", "content": "注入系统提示"},      # wrong role
        {"role": "user"},                                    # missing content
        {"role": "assistant", "content": 123},               # non-string content
        "not-a-dict",                                        # not a dict
        {"role": "user", "content": "   "},                  # blank content
        {"role": "assistant", "content": "有效回答"},
    ]
    res = client.post("/api/follow-up", json=_payload(history=history), headers=_auth_headers())
    assert res.status_code == 200
    assert captured[0]["history"] == [
        {"role": "user", "content": "有效问题"},
        {"role": "assistant", "content": "有效回答"},
    ]


def test_followup_history_capped_and_truncated(captured):
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}轮"}
        for i in range(15)
    ]
    history.append({"role": "user", "content": "长" * 5000})
    res = client.post("/api/follow-up", json=_payload(history=history), headers=_auth_headers())
    assert res.status_code == 200
    got = captured[0]["history"]
    assert len(got) == 10  # last 10 turns only
    assert got[0]["content"] == "第6轮"
    assert len(got[-1]["content"]) == 2000  # each content capped


def test_followup_attachments_injected(captured):
    report = "报" * 9000
    res = client.post(
        "/api/follow-up",
        json=_payload(report=report, attachments="2024年报关键数据：营收6600亿"),
        headers=_auth_headers(),
    )
    assert res.status_code == 200
    prompt = captured[0]["user_message"]
    assert "## 用户上传的参考资料" in prompt
    assert "2024年报关键数据：营收6600亿" in prompt
    # Report context shrinks to 6000 chars when attachments are present
    assert report[-6000:] in prompt
    assert report[-6001:] not in prompt


def test_followup_report_context_8000_without_attachments(captured):
    report = "报" * 9000
    res = client.post("/api/follow-up", json=_payload(report=report), headers=_auth_headers())
    assert res.status_code == 200
    prompt = captured[0]["user_message"]
    assert report[-8000:] in prompt
    assert report[-8001:] not in prompt


def test_followup_oversized_attachments_rejected():
    res = client.post(
        "/api/follow-up",
        json=_payload(attachments="x" * 20001),
        headers=_auth_headers(),
    )
    assert res.status_code == 422


def test_followup_missing_fields_rejected():
    res = client.post("/api/follow-up", json={"report": "", "question": ""}, headers=_auth_headers())
    assert res.status_code == 400
