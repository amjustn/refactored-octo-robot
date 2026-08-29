#!/usr/bin/env python3
"""WS 断线重连补发/状态对齐测试。

覆盖：
- watch_task_id 附加到活跃任务：收到后续事件与 complete 帧，且不注册新任务
  （active task count 不变）
- watch_task_id 指向未知任务：干净的 error 帧 + 关闭，无崩溃
- watch_task_id 指向已完成任务：合成 complete 帧，带回存档报告
- /api/task/{id}：已完成任务带回 report（截断至 200k）、arguments、duration

Run: cd . && venv/bin/python -m pytest tests/test_ws_reconnect.py -v
"""
import os
import sys
import json

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, ".")

import pytest
from fastapi.testclient import TestClient

import app.web_common as main_module
from app.main import app
from app.core import config as _config

client = TestClient(app)


def _auth_headers():
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        return {"Authorization": f"Bearer {create_token(_config.BERKSHIRE_API_TOKEN)}"}
    return {}


def _ws_payload(**kw):
    """WS 首帧；环境配置了 BERKSHIRE_API_TOKEN 时附带 JWT。"""
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        kw["token"] = create_token(_config.BERKSHIRE_API_TOKEN)
    return json.dumps(kw)


@pytest.fixture
def tmp_repo(monkeypatch, tmp_path):
    """把持久化重定向到临时目录（tasks.db + reports/），与 test_harness_opt 同法。"""
    import app.harness.persist as persist_mod
    from app.core.task_store import TaskStore

    repo = persist_mod.Repository(TaskStore(db_path=tmp_path / "tasks.db"))
    monkeypatch.setattr(persist_mod, "_repo", repo)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    monkeypatch.setattr(persist_mod, "REPORTS_DIR", reports_dir)
    return repo


@pytest.fixture
def mock_llm(monkeypatch):
    """拦截 runner 的 LLM 入口 + ContextBuilder，与 test_harness_opt 同法。"""
    from app.harness import runner as runner_mod
    from app.harness.context import ContextBuilder
    from app.harness.prompts import AGENT_ROLE_PROMPTS

    delays = {}

    def _agent_of(system: str):
        for name, prompt in AGENT_ROLE_PROMPTS.items():
            if prompt and system.startswith(prompt[:30]):
                return name
        return None

    async def fake_chat_with_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
        import asyncio
        name = _agent_of(system)
        sleep_s, result = delays.get(name, (0, f"{name} 分析结论"))
        if sleep_s:
            await asyncio.sleep(sleep_s)
        return result

    async def fake_chat_stream(system, user, llm_config=None, **kw):
        yield "综合报告内容"

    async def fake_chat_complete(system, user, llm_config=None, **kw):
        return "完整输出"

    monkeypatch.setattr(runner_mod, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(runner_mod, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(runner_mod, "chat_complete_with_tools", fake_chat_with_tools)
    # run() 的报告摘要润色走的是 harness 模块自己 import 的 chat_complete，
    # 不打掉会重试真实网络（LLM_BASE_URL 指向 localhost:9999）拖慢测试。
    import app.harness as harness_mod
    monkeypatch.setattr(harness_mod, "chat_complete", fake_chat_complete)

    class _FakeBundle:
        context = ""

    async def _fake_build(self, arguments, skill_name=None, llm_config=None):
        return _FakeBundle()

    monkeypatch.setattr(ContextBuilder, "build", _fake_build)
    return delays


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """测试进程内多次 WS 连接触发的是同一 IP 的限流，这里放宽。"""
    monkeypatch.setattr(main_module, "_RATE_LIMIT_MAX", 10000)


def _read_until(ws, wanted, max_frames=200):
    """读到指定 type 的帧并返回它；期间丢弃其它帧。"""
    for _ in range(max_frames):
        msg = ws.receive_json()
        if msg["type"] == wanted:
            return msg
    raise AssertionError(f"未等到 {wanted} 帧")


# ==================== watch_task_id：活跃任务 ====================

class TestWatchActiveTask:
    def test_watch_receives_events_without_new_run(self, mock_llm, tmp_repo):
        """活跃任务 + watch_task_id → 收到后续事件与 complete；活跃任务数不变。"""
        from app.harness import active_count

        # 让每个 Agent 睡一会儿，保证 watch  attach 时任务仍在运行
        for name in ("financial-analyst", "industry-researcher",
                     "business-analyst", "risk-assessor"):
            mock_llm[name] = (0.6, f"{name} 结论")

        # 关键：TestClient 作为上下文管理器时所有连接共享同一个 portal
        # （同一事件循环），与生产环境 uvicorn 单循环一致；否则每条 WS
        # 各起一个 loop，asyncio.Queue 跨循环投递永远无法唤醒接收方。
        with client:
            with client.websocket_connect("/ws/research/investment-team") as ws1:
                ws1.send_text(_ws_payload(arguments="断线重连测试"))
                started = _read_until(ws1, "started")
                task_id = started["task_id"]
                count_before = active_count()

                with client.websocket_connect("/ws/research/investment-team") as ws2:
                    ws2.send_text(_ws_payload(watch_task_id=task_id))
                    # 监听 attach 后不得注册新任务
                    assert active_count() == count_before

                    seen = []
                    while True:
                        msg = ws2.receive_json()
                        assert msg["task_id"] == task_id
                        # watch 连接不得收到新的 started（没有新 run）
                        assert msg["type"] != "started"
                        seen.append(msg["type"])
                        if msg["type"] in ("complete", "error", "cancelled"):
                            terminal = msg
                            break

                    assert terminal["type"] == "complete"
                    assert "综合报告内容" in terminal["report"]
                    assert active_count() == count_before  # 全程无新任务注册

                # 原始连接也正常收到 complete
                done = _read_until(ws1, "complete")
                assert done["task_id"] == task_id


# ==================== watch_task_id：未知 / 已结束任务 ====================

class TestWatchInactiveTask:
    def test_watch_unknown_task_clean_error(self, tmp_repo):
        """未知 task_id → 干净 error 帧 + 服务端主动关闭，无崩溃。"""
        from starlette.websockets import WebSocketDisconnect

        with client.websocket_connect("/ws/research/investment-team") as ws:
            ws.send_text(_ws_payload(watch_task_id="no-such-task"))
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg.get("watch") is True
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()

    def test_watch_terminal_failed_task_clean_error(self, tmp_repo):
        """已失败任务 → error 帧 + 关闭（前端据此走重启路径）。"""
        from starlette.websockets import WebSocketDisconnect
        from app.models.schemas import ResearchStatus

        tmp_repo.save_task(ResearchStatus(
            task_id="t-failed", skill_name="investment-team",
            status="failed", error="boom",
        ), arguments="x")
        with client.websocket_connect("/ws/research/investment-team") as ws:
            ws.send_text(_ws_payload(watch_task_id="t-failed"))
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg.get("watch") is True
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()

    def test_watch_completed_task_synthesizes_complete(self, tmp_repo):
        """已完成任务 → 合成 complete 帧，带回存档报告（断线期间刚好跑完）。"""
        from app.models.schemas import ResearchStatus

        tmp_repo.save_task(ResearchStatus(
            task_id="t-done", skill_name="investment-team",
            status="completed", report="# 存档报告\n\n结论",
        ), arguments="贵州茅台")
        with client.websocket_connect("/ws/research/investment-team") as ws:
            ws.send_text(_ws_payload(watch_task_id="t-done"))
            msg = ws.receive_json()
            assert msg["type"] == "complete"
            assert msg["task_id"] == "t-done"
            assert msg["report"] == "# 存档报告\n\n结论"
            assert msg.get("resumed") is True


# ==================== /api/task/{id} 渲染数据 ====================

class TestApiTaskPayload:
    def test_completed_task_includes_render_payload(self, tmp_repo):
        """已完成任务：report（截断 200k）+ arguments + duration_seconds。"""
        from app.models.schemas import ResearchStatus

        big_report = "报" * 250_000
        tmp_repo.save_task(ResearchStatus(
            task_id="t-big", skill_name="investment-team",
            status="completed", report=big_report,
        ), arguments="贵州茅台")
        tmp_repo.save_billing(
            "t-big", {"prompt_tokens": 10, "completion_tokens": 20}, 12.3,
        )

        r = client.get("/api/task/t-big", headers=_auth_headers())
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert len(data["report"]) == 200_000  # 截断上限
        assert data["arguments"] == "贵州茅台"
        assert data["duration_seconds"] == 12.3

    def test_unknown_task_not_found(self, tmp_repo):
        r = client.get("/api/task/no-such-task", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json().get("error") == "Task not found"
