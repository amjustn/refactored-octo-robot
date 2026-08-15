#!/usr/bin/env python3
"""B2: /api/research 服务端去重测试。

同 skill+arguments 且已有 running 任务 → 409 + 既有 task_id，不新起任务；
无 running 任务 → 正常开跑（harness_run 被打桩，不耗 LLM）。
"""
import os
import sys

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.core import config as _config

client = TestClient(app)


def _auth_headers():
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        return {"Authorization": "Bearer " + create_token(_config.BERKSHIRE_API_TOKEN)}
    return {}



def test_same_skill_args_running_returns_409(monkeypatch):
    import app.harness as harness
    import app.routers.research as research_mod
    from app.models.schemas import ResearchStatus

    called = []

    async def fake_run(spec, **kw):
        called.append(spec)
        raise AssertionError("不应新起任务")

    monkeypatch.setattr(research_mod, "harness_run", fake_run)
    harness._active["existing-1"] = ResearchStatus(
        task_id="existing-1", skill_name="investment-team", status="running")
    harness._active_args["existing-1"] = "贵州茅台"
    try:
        res = client.post("/api/research", json={
            "skill_name": "investment-team", "arguments": "贵州茅台"}, headers=_auth_headers())
        assert res.status_code == 409
        body = res.json()
        assert body["task_id"] == "existing-1"
        assert body["status"] == "running"
        assert called == []
    finally:
        harness._active.pop("existing-1", None)
        harness._active_args.pop("existing-1", None)


def test_no_running_task_proceeds(monkeypatch):
    import app.routers.research as research_mod

    async def fake_run(spec, **kw):
        from app.harness import RunResult
        return RunResult(task_id=spec.task_id, status="completed", report="# R\n\n正文")

    monkeypatch.setattr(research_mod, "harness_run", fake_run)
    res = client.post("/api/research", json={
        "skill_name": "investment-team", "arguments": "贵州茅台"}, headers=_auth_headers())
    assert res.status_code == 200
    assert res.json()["status"] == "completed"


def test_same_skill_different_args_proceeds(monkeypatch):
    import app.harness as harness
    import app.routers.research as research_mod
    from app.models.schemas import ResearchStatus

    async def fake_run(spec, **kw):
        from app.harness import RunResult
        return RunResult(task_id=spec.task_id, status="completed", report="# R\n\n正文")

    monkeypatch.setattr(research_mod, "harness_run", fake_run)
    harness._active["existing-2"] = ResearchStatus(
        task_id="existing-2", skill_name="investment-team", status="running")
    harness._active_args["existing-2"] = "腾讯"
    try:
        res = client.post("/api/research", json={
            "skill_name": "investment-team", "arguments": "贵州茅台"}, headers=_auth_headers())
        assert res.status_code == 200
    finally:
        harness._active.pop("existing-2", None)
        harness._active_args.pop("existing-2", None)
