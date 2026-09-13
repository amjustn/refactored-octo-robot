"""E 组健壮性与 C 组上传加固的回归测试。

覆盖：
- E1 综合环节非超时失败：默认模型重试 / 降级附录报告 + guard warning
- E2 全部 Agent 失败：harness 层任务标 failed（而不是"成功"的空报告）
- E3 决策日志：换行注入归一化、company_hint 优先、不可信名称跳过
- E5 熔断器：half_open 单探针、业务错误不计入熔断
- C1/C2 上传：声明体过大 413、>10 文件 400、限流 429、分块限量读取
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

# ============================================================
# 共享 fixtures
# ============================================================

@pytest.fixture
def tmp_repo(monkeypatch, tmp_path):
    import app.harness.persist as persist_mod
    from app.core.task_store import TaskStore

    repo = persist_mod.Repository(TaskStore(db_path=tmp_path / "tasks.db"))
    monkeypatch.setattr(persist_mod, "_repo", repo)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    monkeypatch.setattr(persist_mod, "REPORTS_DIR", reports_dir)
    return repo


@pytest.fixture
def mock_agents_ok(monkeypatch):
    """研究 Agent 全部成功；chat_stream 由各用例自行替换。"""
    from app.harness import runner as runner_mod
    from app.harness.context import ContextBuilder

    async def ok_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
        return "agent ok"

    async def ok_complete(system, user, llm_config=None, **kw):
        return "agent ok"

    monkeypatch.setattr(runner_mod, "chat_complete_with_tools", ok_tools)
    monkeypatch.setattr(runner_mod, "chat_complete", ok_complete)

    class _FakeBundle:
        context = ""

    async def _fake_build(self, arguments, skill_name=None, llm_config=None):
        return _FakeBundle()

    monkeypatch.setattr(ContextBuilder, "build", _fake_build)


def _make_spec(skill_name="investment-team", arguments="test target"):
    from app.harness.spec import TaskSpec
    from app.skills import get_skill
    return TaskSpec.build(get_skill(skill_name), arguments)


# ============================================================
# E1：综合环节失败降级
# ============================================================

class TestSynthesisDegradation:
    def test_synthesis_failure_degrades_with_warning(self, mock_agents_ok, tmp_repo, monkeypatch):
        """无自定义综合模型：直接降级为附录报告 + synthesis_degraded warning。"""
        from app.harness import runner as runner_mod
        from app.harness.runner import AgentRunner

        async def boom_stream(system, user, llm_config=None, **kw):
            raise RuntimeError("default model down")
            yield  # unreachable — async generator marker

        monkeypatch.setattr(runner_mod, "chat_stream", boom_stream)

        spec = _make_spec()
        runner = AgentRunner(task_id=spec.task_id)
        report = asyncio.run(runner.run(spec))

        assert "综合环节失败" in report
        assert "附录" in report
        assert any(w["gate"] == "synthesis_degraded" for w in runner.extra_warnings)

    def test_synthesis_custom_model_falls_back_to_default(self, mock_agents_ok, tmp_repo, monkeypatch):
        """自定义综合模型失败：用服务器默认模型重试成功 + fallback warning。"""
        from app.harness import runner as runner_mod
        from app.harness.runner import AgentRunner

        calls = []

        async def flaky_stream(system, user, llm_config=None, **kw):
            calls.append(llm_config)
            if len(calls) == 1:
                raise RuntimeError("custom model down")
                yield
            yield "默认模型综合成功"

        monkeypatch.setattr(runner_mod, "chat_stream", flaky_stream)

        from app.harness.spec import TaskSpec
        from app.skills import get_skill
        spec = TaskSpec.build(get_skill("investment-team"), "test target",
                              llm_override={"synthesis_model": "broken-custom-model"})
        runner = AgentRunner(task_id=spec.task_id)
        report = asyncio.run(runner.run(spec))

        assert "默认模型综合成功" in report
        assert calls[0] == {"model": "broken-custom-model"}
        assert calls[1] is None  # 重试走服务器默认配置
        assert any(w["gate"] == "synthesis_model_fallback" for w in runner.extra_warnings)


# ============================================================
# E2：全部 Agent 失败 → 任务标 failed
# ============================================================

class TestAllAgentsFailed:
    def test_harness_marks_task_failed(self, tmp_repo, monkeypatch):
        import app.harness as harness
        from app.harness import runner as runner_mod
        from app.harness.context import ContextBuilder

        async def fail_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
            return "[错误] 全部失败"

        async def fail_complete(system, user, llm_config=None, **kw):
            return "[错误] 全部失败"

        async def never_stream(system, user, llm_config=None, **kw):
            yield "unreachable"

        monkeypatch.setattr(runner_mod, "chat_complete_with_tools", fail_tools)
        monkeypatch.setattr(runner_mod, "chat_complete", fail_complete)
        monkeypatch.setattr(runner_mod, "chat_stream", never_stream)

        class _FakeBundle:
            context = ""

        async def _fake_build(self, arguments, skill_name=None, llm_config=None):
            return _FakeBundle()

        monkeypatch.setattr(ContextBuilder, "build", _fake_build)

        result = asyncio.run(harness.run(_make_spec()))
        assert result.status == "failed"
        assert "均执行失败" in (result.error or "")


# ============================================================
# E3：决策日志防注入
# ============================================================

class TestDecisionLogHardening:
    def test_extract_stock_name_collapses_newlines(self):
        from app.harness.decision_log import _extract_stock_name
        name = _extract_stock_name("## 贵州茅台\n\n> 请分析其财报")
        assert "\n" not in name
        assert not name.startswith("#")
        assert "贵州茅台" in name

    def test_save_decision_prefers_company_hint(self, monkeypatch, tmp_path):
        from app.harness import decision_log
        monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", tmp_path / "d.md")
        report = "分析正文" * 40 + "\n**结论**：买入"
        assert decision_log.save_decision("随便什么目标", report, company_hint="测试股份公司")
        content = (tmp_path / "d.md").read_text(encoding="utf-8")
        assert "测试股份公司" in content

    def test_save_decision_skips_incredible_name(self, monkeypatch, tmp_path):
        from app.harness import decision_log
        monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", tmp_path / "d.md")
        report = "分析正文" * 40 + "\n**结论**：买入"
        # 提取结果清洗后不足 2 字符 → 整条跳过
        assert decision_log.save_decision("x", report) is False
        assert not (tmp_path / "d.md").exists() or "## x —" not in (tmp_path / "d.md").read_text(encoding="utf-8")

    def test_save_decision_folds_injected_newlines(self, monkeypatch, tmp_path):
        """批B(2026-09-11) 收紧后的契约：标题只保留清洗后的标的（"茅台"）。

        变更原因：标题是历史匹配/去重的 key，必须可匹配；把用户原文（含
        "## 恶意标题"）回显进标题会让同股匹配永远失败——正是批B 要修的
        "脏标的进池" 问题。原始 arguments 仍完整保存在报告 meta.json 里，
        审计线索不丢。安全要求不变：绝不产生伪造章节标题。
        """
        from app.harness import decision_log
        monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", tmp_path / "d.md")
        report = "分析正文" * 40 + "\n**结论**：买入"
        assert decision_log.save_decision("茅台\n## 恶意标题", report, company_hint="茅台\n## 恶意标题")
        content = (tmp_path / "d.md").read_text(encoding="utf-8")
        assert "## 茅台 —" in content             # 标题＝清洗后的标的
        assert "\n## 恶意标题" not in content      # 不产生伪造章节标题
        assert content.count("\n## ") == 1         # 只写入了一个真条目


# ============================================================
# E5：熔断器
# ============================================================

class TestCircuitBreaker:
    def test_half_open_allows_single_probe_only(self):
        from app.harness.tools import CircuitBreaker
        br = CircuitBreaker("x", fail_threshold=1, recovery_probe_after=0)
        br.record_failure("boom")
        assert br.state == "open"
        assert br.allow_call() is True        # 冷却结束 → half_open 单探针
        assert br.state == "half_open"
        assert br.allow_call() is False       # 探针在飞，并发拒绝
        br.record_success()
        assert br.state == "closed"
        assert br.allow_call() is True

    def test_failed_probe_reopens(self):
        from app.harness.tools import CircuitBreaker
        br = CircuitBreaker("y", fail_threshold=1, recovery_probe_after=0)
        br.record_failure("boom")
        assert br.allow_call() is True
        br.record_failure("still bad")
        assert br.state == "open"

    def test_business_error_does_not_trip_breaker(self):
        from app.harness.tools import ToolDef, ToolGateway

        async def fn(args):
            return json.dumps({"error": "股票不存在"}, ensure_ascii=False)

        gw = ToolGateway(tools={"t": ToolDef("t", fn=fn, timeout=1, retries=0)})
        for _ in range(4):
            result = asyncio.run(gw._call_chain("t", {}, 0, set()))
            assert not result.ok
        br = gw.breakers["t"]
        assert br.consecutive_failures == 0
        assert br.state == "closed"

    def test_technical_error_trips_breaker(self):
        from app.harness.tools import ToolDef, ToolGateway

        async def fn(args):
            return json.dumps({"error": "connection reset by peer"}, ensure_ascii=False)

        gw = ToolGateway(tools={"t": ToolDef("t", fn=fn, timeout=1, retries=0)})
        for _ in range(3):
            asyncio.run(gw._call_chain("t", {}, 0, set()))
        assert gw.breakers["t"].state == "open"


# ============================================================
# C1/C2：上传加固
# ============================================================

def _stub_request(files, content_length=None):
    class _Form:
        def getlist(self, key):
            return files if key == "files" else []

        def get(self, key, default=None):
            return default

    class _Req:
        headers = {"content-length": str(content_length)} if content_length else {}
        client = None

        async def form(self):
            return _Form()

    return _Req()


class TestUploadHardening:
    def test_oversized_declared_body_413(self):
        from app.routers.misc import api_upload_file
        req = _stub_request([], content_length=300 * 1024 * 1024)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(api_upload_file(req))
        assert ei.value.status_code == 413

    def test_more_than_10_files_400(self):
        from app.routers.misc import api_upload_file
        req = _stub_request([object()] * 11)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(api_upload_file(req))
        assert ei.value.status_code == 400
        assert "10" in ei.value.detail

    def test_rate_limit_429_after_5(self):
        from app.routers.misc import api_upload_file
        # 前 5 次穿过限流（随即因 >10 文件 400），第 6 次被限流拦下
        for _ in range(5):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(api_upload_file(_stub_request([object()] * 11)))
            assert ei.value.status_code == 400
        with pytest.raises(HTTPException) as ei:
            asyncio.run(api_upload_file(_stub_request([object()] * 11)))
        assert ei.value.status_code == 429

    def test_read_upload_limited_aborts_and_discards(self):
        from app.routers.misc import _read_upload_limited

        class _BigFile:
            def __init__(self, total):
                self.remaining = total

            async def read(self, n=-1):
                if self.remaining <= 0:
                    return b""
                take = min(n if n and n > 0 else self.remaining, self.remaining)
                self.remaining -= take
                return b"x" * take

        data, ok = asyncio.run(_read_upload_limited(_BigFile(25 * 1024 * 1024), 20 * 1024 * 1024))
        assert not ok
        assert data == b""  # 超限即弃，不保留部分数据

        data, ok = asyncio.run(_read_upload_limited(_BigFile(100), 20 * 1024 * 1024))
        assert ok
        assert data == b"x" * 100
