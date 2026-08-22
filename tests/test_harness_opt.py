#!/usr/bin/env python3
"""编排层优化回归测试（P0-1 ~ P1-5）。

覆盖：
- P0-1：run_multi 信号量调度 + 单 Agent 超时不拖垮同级；附录保持名册顺序
- P0-2：全局并发闸（busy 拒绝）+ WS 限流
- P0-3：task_artifacts 中间产物持久化 + 取消/失败时生成部分报告
- P1-2：工具循环中 LLM 异常 → 无工具兜底沿用既有消息（保留工具结果）
- P1-3：结构闸门只告警不注入横幅；超长报告仅由 OutputGuard 截断一次

"""
import os
import sys
import json
import asyncio
from types import SimpleNamespace

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


# ==================== mock 工具 ====================

@pytest.fixture
def tmp_repo(monkeypatch, tmp_path):
    """把持久化重定向到临时目录（tasks.db + reports/），与 eval/replay 同法。"""
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
    """拦截 runner 的 LLM 入口 + ContextBuilder；返回可控的 per-agent 行为表。

    返回 delays: {agent_name: (sleep_seconds, result)}，未列出的 Agent 立即成功。
    """
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

    class _FakeBundle:
        context = ""

    async def _fake_build(self, arguments, skill_name=None, llm_config=None):
        return _FakeBundle()

    monkeypatch.setattr(ContextBuilder, "build", _fake_build)
    return delays


def _make_spec(skill_name="investment-team", arguments="测试目标"):
    from app.harness.spec import TaskSpec
    from app.skills import get_skill

    return TaskSpec.build(get_skill(skill_name), arguments)


# ==================== P0-1：信号量调度 + 单 Agent 超时隔离 ====================

class TestRunMultiIsolation:
    def test_timeout_agent_does_not_discard_siblings(self, mock_llm, tmp_repo, monkeypatch):
        """一个 Agent 超时（小 AGENT_TIMEOUT）→ 其余 Agent 成果保留，综合照常。"""
        from app.harness import runner as runner_mod
        from app.harness.runner import AgentRunner
        from app.harness.prompts import get_role_label
        from app.skills import get_skill

        monkeypatch.setattr(runner_mod, "AGENT_TIMEOUT", 0.3)
        mock_llm["financial-analyst"] = (5.0, "不应出现")      # 超时被取消
        mock_llm["industry-researcher"] = (0.1, "行业研究结论")  # 比快 Agent 后完成
        mock_llm["business-analyst"] = (0, "商业模式结论")
        mock_llm["risk-assessor"] = (0, "风险评估结论")

        spec = _make_spec()
        report = asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))

        # 综合照常进行，成功 Agent 的成果保留
        assert "综合报告内容" in report
        assert "商业模式结论" in report
        assert "行业研究结论" in report
        assert "风险评估结论" in report
        assert "不应出现" not in report
        # 失败计数体现在执行模式行
        assert "3/4 Agent 成功（1 个失败）" in report

        # 附录顺序 = 名册顺序，与完成顺序无关
        roster = get_skill("investment-team")["agents"]
        labels = [get_role_label(n) for n in roster]
        appendix = report.split("## 附录：各Agent独立研究报告", 1)[1]
        positions = [appendix.find(f"## {label}") for label in labels]
        present = [p for p in positions if p >= 0]
        assert len(present) == 3  # 超时 Agent 不进附录
        assert present == sorted(present), "附录必须保持名册顺序"

    def test_artifacts_saved_for_every_agent(self, mock_llm, tmp_repo, monkeypatch):
        """每个研究 Agent（含超时失败）都写入 task_artifacts。"""
        from app.harness import runner as runner_mod
        from app.harness.runner import AgentRunner

        monkeypatch.setattr(runner_mod, "AGENT_TIMEOUT", 0.3)
        mock_llm["financial-analyst"] = (5.0, "不应出现")

        spec = _make_spec()
        asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))

        artifacts = dict(tmp_repo.get_artifacts(spec.task_id))
        assert set(artifacts) == {
            "business-analyst", "financial-analyst", "industry-researcher", "risk-assessor",
        }
        assert artifacts["financial-analyst"].startswith("[错误] Agent 超时")
        assert artifacts["business-analyst"] == "business-analyst 分析结论"


# ==================== P0-2：全局并发闸 + WS 限流 ====================

class TestConcurrencyGate:
    def test_busy_run_rejected_without_registration(self, tmp_repo, monkeypatch):
        """MAX_CONCURRENT_RUNS=1：第二个 run 立即收到 busy 错误，不注册不计费。"""
        import app.harness as harness
        from app.harness.events import EventBus, EventType

        monkeypatch.setattr(harness, "MAX_CONCURRENT_RUNS", 1)
        harness._run_semaphores.clear()

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_execute(spec, bus, **kwargs):
            started.set()
            await release.wait()
            return "# 报告\n\n正文", []

        monkeypatch.setattr(harness, "_execute", fake_execute)

        async def main():
            bus = EventBus()
            spec1, spec2 = _make_spec(), _make_spec()
            q2 = bus.subscribe(spec2.task_id)

            t1 = asyncio.create_task(harness.run(spec1, bus))
            await asyncio.wait_for(started.wait(), timeout=2)

            r2 = await harness.run(spec2, bus)
            assert r2.status == "error"
            assert "繁忙" in r2.error
            assert harness.active_count() == 1  # 只有 spec1 注册为 running
            assert spec2.task_id not in harness._active

            ev = q2.get_nowait()
            assert ev.type == EventType.ERROR
            assert ev.payload.get("code") == "busy"
            assert "繁忙" in ev.payload.get("message", "")

            release.set()
            r1 = await t1
            assert r1.status == "completed"

        asyncio.run(main())

    def test_ws_rate_limit_rejects_over_limit(self, monkeypatch):
        """ws_research 在 JWT 校验后应用与 /api/research 相同的限流。"""
        import app.main as main_mod
        import app.web_common as wc_mod

        monkeypatch.setattr(wc_mod, "_ws_verify_jwt", lambda token: True)

        ip = "203.0.113.7"
        wc_mod._rate_limiter.pop(ip, None)
        for _ in range(wc_mod._RATE_LIMIT_MAX):
            assert wc_mod._check_rate_limit(ip)

        sent = []

        class _FakeWS:
            client = SimpleNamespace(host=ip)

            async def accept(self):
                pass

            async def receive_text(self):
                return json.dumps({"arguments": "测试目标"})

            async def send_json(self, obj):
                sent.append(obj)

            async def close(self, code=None):
                self.closed = code

        ws = _FakeWS()
        try:
            asyncio.run(main_mod.ws_research(ws, "investment-team"))
        finally:
            wc_mod._rate_limiter.pop(ip, None)

        assert sent == [{"type": "error", "message": "请求过于频繁，请稍后再试"}]
        assert ws.closed == 1008


# ==================== P0-3：中间产物 + 部分报告 ====================

class TestPartialReports:
    def test_cancelled_run_writes_partial_report(self, tmp_repo, monkeypatch):
        """取消的运行有 artifacts 但无最终报告 → 生成 partial 报告文件。"""
        import app.harness as harness
        import app.harness.persist as persist_mod
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            await persist_mod.get_repository().asave_artifact(spec.task_id, "agent-a", "已完成内容")
            raise asyncio.CancelledError

        monkeypatch.setattr(harness, "_execute", fake_execute)

        spec = _make_spec()
        result = asyncio.run(harness.run(spec, EventBus()))
        assert result.status == "cancelled"

        reports = list(persist_mod.REPORTS_DIR.glob("*.md"))
        assert len(reports) == 1
        body = reports[0].read_text(encoding="utf-8")
        assert body.startswith("# AI Berkshire 投研报告（部分结果）")
        assert "> ⚠️ 本报告为部分结果（任务取消时已完成的 1 个 Agent 成果）" in body
        assert "已完成内容" in body

        meta = json.loads(reports[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["partial"] is True
        assert meta["skill_name"] == "investment-team"

    def test_failed_run_writes_partial_report(self, tmp_repo, monkeypatch):
        """失败的运行同样生成 partial 报告。"""
        import app.harness as harness
        import app.harness.persist as persist_mod
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            await persist_mod.get_repository().asave_artifact(spec.task_id, "agent-b", "部分成果")
            raise RuntimeError("LLM 服务不可用")

        monkeypatch.setattr(harness, "_execute", fake_execute)

        spec = _make_spec()
        result = asyncio.run(harness.run(spec, EventBus()))
        assert result.status == "failed"

        reports = list(persist_mod.REPORTS_DIR.glob("*.md"))
        assert len(reports) == 1
        assert "任务失败时" in reports[0].read_text(encoding="utf-8")
        meta = json.loads(reports[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["partial"] is True

    def test_completed_run_has_no_partial(self, tmp_repo, monkeypatch):
        """正常完成 → 只有正式报告，meta 无 partial 标记。"""
        import app.harness as harness
        import app.harness.persist as persist_mod
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            await persist_mod.get_repository().asave_artifact(spec.task_id, "agent-a", "成果")
            return "# 正式报告\n\n正文", []

        monkeypatch.setattr(harness, "_execute", fake_execute)

        spec = _make_spec()
        result = asyncio.run(harness.run(spec, EventBus()))
        assert result.status == "completed"

        reports = list(persist_mod.REPORTS_DIR.glob("*.md"))
        assert len(reports) == 1
        assert "部分结果" not in reports[0].read_text(encoding="utf-8")
        meta = json.loads(reports[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert "partial" not in meta

    def test_run_without_artifacts_writes_no_partial(self, tmp_repo, monkeypatch):
        """没有任何 artifact 的失败运行 → 不生成部分报告。"""
        import app.harness as harness
        import app.harness.persist as persist_mod
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            raise RuntimeError("context 构建失败")

        monkeypatch.setattr(harness, "_execute", fake_execute)

        spec = _make_spec()
        result = asyncio.run(harness.run(spec, EventBus()))
        assert result.status == "failed"
        assert list(persist_mod.REPORTS_DIR.glob("*.md")) == []


# ==================== P1-2：工具循环兜底保留工具结果 ====================

class TestToolLoopFallback:
    def test_fallback_reuses_messages_with_tool_results(self, monkeypatch):
        """第 1 轮 tool_call → 第 2 轮 LLM 异常 → 无工具兜底收到含工具结果的消息。"""
        import app.core.llm as llm_mod

        calls = []

        class _FakeCompletions:
            async def create(self, **kwargs):
                calls.append(kwargs)
                n = len(calls)
                if n == 1:
                    tc = SimpleNamespace(
                        id="call_1", type="function",
                        function=SimpleNamespace(name="calc", arguments="{}"),
                    )
                    msg = SimpleNamespace(content=None, tool_calls=[tc])
                    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
                if n == 2:
                    raise RuntimeError("boom round2")
                assert "tools" not in kwargs, "兜底调用不得再带 tools"
                msg = SimpleNamespace(content="最终回答", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
        monkeypatch.setattr(llm_mod, "get_client", lambda *a, **kw: fake_client)

        async def tool_executor(name, args):
            return "TOOL_RESULT_42"

        out = asyncio.run(llm_mod.chat_complete_with_tools(
            "系统提示", "用户问题",
            tools=[{"type": "function", "function": {"name": "calc"}}],
            tool_executor=tool_executor,
        ))

        assert out == "最终回答"
        assert len(calls) == 3
        fallback_messages = calls[2]["messages"]
        assert any(
            m.get("role") == "tool" and "TOOL_RESULT_42" in m.get("content", "")
            for m in fallback_messages
        ), "兜底补全必须沿用包含工具结果的既有消息"


# ==================== P1-3：结构闸门不注入横幅；截断唯一归属 OutputGuard ====================

class TestGuardHygiene:
    def test_structure_gate_warns_without_banner(self, monkeypatch):
        from app.harness import guards

        monkeypatch.setattr(guards, "_required_sections", lambda skill: ["必备章节甲", "必备章节乙"])
        report, warnings = guards.OutputGuard().check(
            "# 标题\n\n正文内容。", skill_name="x", strategy="single",
        )
        assert any(w["gate"] == "structure" for w in warnings)
        assert "结构闸门" not in report
        assert report.startswith("# 标题")

    def test_oversized_series_truncated_once(self, monkeypatch):
        from app.harness import guards

        monkeypatch.setattr(guards, "MAX_SERIES_REPORT_LENGTH", 100)
        report, warnings = guards.OutputGuard().check("# 系列\n" + "x" * 500, strategy="series")
        assert any(w["gate"] == "length" for w in warnings)
        assert report.count("报告已截断") == 1

    def test_series_length_constant_single_source(self):
        """MAX_SERIES_REPORT_LENGTH 唯一定义在 guards，runner 仅为兼容再导出。"""
        from app.harness import guards, runner

        assert runner.MAX_SERIES_REPORT_LENGTH == guards.MAX_SERIES_REPORT_LENGTH == 200000
