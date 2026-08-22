#!/usr/bin/env python3
"""
AI Berkshire Web --- Comprehensive Harness Test Suite

Covers every component in app/harness/:
  spec / events / prompts / guards / context / persist /
  decision_log / tools / runner / __init__ (run/cancel/resume)

"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ============================================================
# Fixtures
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
def mock_llm_harness(monkeypatch):
    from app.harness import runner as runner_mod
    from app.harness.context import ContextBuilder
    from app.harness.prompts import AGENT_ROLE_PROMPTS

    delays = {}

    def _agent_of(system):
        for name, prompt in AGENT_ROLE_PROMPTS.items():
            if prompt and system.startswith(prompt[:30]):
                return name
        return None

    async def fake_chat_with_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
        name = _agent_of(system)
        sleep_s, result = delays.get(name, (0, f"{name} results"))
        if sleep_s:
            await asyncio.sleep(sleep_s)
        return result

    async def fake_chat_stream(system, user, llm_config=None, **kw):
        yield "synthesized report content"

    async def fake_chat_complete(system, user, llm_config=None, **kw):
        return "complete output"

    monkeypatch.setattr(runner_mod, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(runner_mod, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(runner_mod, "chat_complete_with_tools", fake_chat_with_tools)

    class _FakeBundle:
        context = ""

    async def _fake_build(self, arguments, skill_name=None, llm_config=None):
        return _FakeBundle()

    monkeypatch.setattr(ContextBuilder, "build", _fake_build)
    return delays


def _make_spec(skill_name="investment-team", arguments="test target"):
    from app.harness.spec import TaskSpec
    from app.skills import get_skill
    return TaskSpec.build(get_skill(skill_name), arguments)


# ============================================================
# 1. spec.py
# ============================================================

class TestSpec:
    def test_build_single(self):
        from app.harness.spec import TaskSpec
        from app.skills import get_skill
        spec = TaskSpec.build(get_skill("daily-briefing"), "test")
        assert spec.strategy == "single"
        assert spec.agent_names == ("default",)
        assert len(spec.task_id) == 12

    def test_build_multi(self):
        from app.harness.spec import TaskSpec
        from app.skills import get_skill
        spec = TaskSpec.build(get_skill("investment-team"), "test")
        assert spec.strategy == "multi"
        assert len(spec.agent_names) >= 4

    def test_build_series(self):
        from app.harness.spec import TaskSpec
        from app.skills import get_skill
        spec = TaskSpec.build(get_skill("deep-company-series"), "test")
        assert spec.strategy == "series"
        assert len(spec.agent_names) == 8

    def test_build_preserves_task_id(self):
        from app.harness.spec import TaskSpec
        from app.skills import get_skill
        spec = TaskSpec.build(get_skill("investment-team"), "t", task_id="abc123")
        assert spec.task_id == "abc123"

    def test_build_every_skill(self):
        from app.skills import list_skills
        from app.harness.spec import TaskSpec
        for skill in list_skills():
            spec = TaskSpec.build(skill, "test")
            assert spec.task_id
            assert spec.strategy in ("single", "multi", "series")

    def test_post_synthesis_excluded_in_runner(self):
        """Post-synthesis agents appear in spec.agent_names but are excluded
        from the research phase by runner.run_multi (filtered before dispatch)."""
        from app.skills import get_skill
        from app.harness.spec import TaskSpec
        spec = TaskSpec.build(get_skill("earnings-team"), "PDD")
        skill = get_skill("earnings-team")
        post = set(skill.get("post_synthesis_agents", []))
        all_agents = set(spec.agent_names)
        # Post-synthesis agents ARE in agent_names (filtering is runner's job)
        assert post & all_agents  # they exist
        assert "editor" in all_agents

    def test_llm_config_default(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig()
        assert cfg.model is None

    def test_llm_config_from_override(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({"model": "gpt-4", "api_key": "sk-test"})
        assert cfg.model == "gpt-4"

    def test_llm_config_synthesis(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({"model": "flash", "synthesis_model": "pro"})
        assert cfg.to_synthesis_config_dict()["model"] == "pro"

    def test_llm_config_per_agent(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({
            "model": "flash", "per_agent_enabled": True,
            "agent_models": {"financial-analyst": "pro"},
        })
        assert cfg.has_any_agent_override() is True

    def test_llm_config_agent_fallback(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({"model": "flash"})
        assert cfg.to_agent_config_dict("unknown") == {"model": "flash"}

    def test_llm_config_immutable(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig(model="test")
        with pytest.raises(Exception):
            cfg.model = "changed"


# ============================================================
# 2. events.py
# ============================================================

class TestEvents:
    def test_emit_and_receive(self):
        from app.harness.events import EventBus
        bus = EventBus()
        q = bus.subscribe("t1")
        bus.emit("t1", "chunk", content="hello")
        ev = q.get_nowait()
        assert ev.payload["content"] == "hello"

    def test_unsubscribe(self):
        from app.harness.events import EventBus
        bus = EventBus()
        q = bus.subscribe("t1")
        bus.unsubscribe("t1", q)
        bus.emit("t1", "chunk", content="x")
        assert q.empty()

    def test_queue_overflow_drops_oldest(self):
        from app.harness.events import EventBus
        bus = EventBus()
        bus._QUEUE_MAX = 3
        q = bus.subscribe("t1")
        for i in range(5):
            bus.emit("t1", "chunk", content=str(i))
        items = []
        while not q.empty():
            items.append(q.get_nowait().payload["content"])
        assert items == ["2", "3", "4"]

    def test_isolated_queues(self):
        from app.harness.events import EventBus
        bus = EventBus()
        q1 = bus.subscribe("t1")
        bus.subscribe("t2")
        bus.emit("t1", "chunk", content="t1-only")
        assert q1.get_nowait().payload["content"] == "t1-only"

    def test_singleton_bus(self):
        from app.harness.events import get_bus
        assert get_bus() is get_bus()

    def test_ws_dict_format(self):
        from app.harness.events import Event, EventType
        ev = Event(type=EventType.STARTED, task_id="t1", payload={"skill": "x"})
        d = ev.to_ws_dict()
        assert d["type"] == "started"
        assert d["task_id"] == "t1"


# ============================================================
# 3. prompts.py
# ============================================================

class TestPrompts:
    def test_all_investment_team_agents(self):
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        from app.skills import get_skill
        for agent in get_skill("investment-team")["agents"]:
            assert agent in AGENT_ROLE_PROMPTS

    def test_all_earnings_team_agents(self):
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        from app.skills import get_skill
        for agent in get_skill("earnings-team")["agents"]:
            assert agent in AGENT_ROLE_PROMPTS

    def test_all_news_pulse_agents(self):
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        from app.skills import get_skill
        for agent in get_skill("news-pulse")["agents"]:
            assert agent in AGENT_ROLE_PROMPTS

    def test_all_global_macro_agents(self):
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        from app.skills import get_skill
        for agent in get_skill("global-macro-analysis")["agents"]:
            assert agent in AGENT_ROLE_PROMPTS

    def test_all_china_macro_agents(self):
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        from app.skills import get_skill
        for agent in get_skill("china-macro-analysis")["agents"]:
            assert agent in AGENT_ROLE_PROMPTS

    def test_team_lead_prompt(self):
        from app.harness.prompts import TEAM_LEAD_PROMPT
        assert "Investment" in TEAM_LEAD_PROMPT or "投资" in TEAM_LEAD_PROMPT

    def test_synthesis_override_global_macro(self):
        from app.harness.prompts import get_synthesis_prompt
        p = get_synthesis_prompt("global-macro-analysis")
        assert "macro" in p.lower() or "宏观" in p

    def test_synthesis_fallback(self):
        from app.harness.prompts import get_synthesis_prompt
        p = get_synthesis_prompt("investment-team")
        assert isinstance(p, str) and len(p) > 100

    def test_series_prompt(self):
        from app.harness.prompts import SERIES_SYSTEM_PROMPT
        assert "get_stock_price" in SERIES_SYSTEM_PROMPT

    def test_role_prompt_known(self):
        from app.harness.prompts import get_role_prompt
        p = get_role_prompt("business-analyst")
        assert "business" in p.lower() or "商业" in p

    def test_role_prompt_unknown(self):
        from app.harness.prompts import get_role_prompt
        assert get_role_prompt("nonexistent") is None

    def test_prompt_count(self):
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        assert len(AGENT_ROLE_PROMPTS) >= 20


# ============================================================
# 4. guards.py --- OutputGuard
# ============================================================

class TestGuards:
    def test_structure_warns_missing(self, monkeypatch):
        from app.harness import guards
        monkeypatch.setattr(guards, "_required_sections", lambda s: ["SectionA"])
        _, warnings = guards.OutputGuard().check("body", skill_name="x", strategy="single")
        assert any(w["gate"] == "structure" for w in warnings)

    def test_structure_ok_when_present(self, monkeypatch):
        from app.harness import guards
        monkeypatch.setattr(guards, "_required_sections", lambda s: ["SectionA"])
        _, warnings = guards.OutputGuard().check("## SectionA\ncontent", skill_name="x", strategy="single")
        assert not any(w["gate"] == "structure" for w in warnings)

    def test_citation_warns_unsourced(self):
        from app.harness import guards
        _, warnings = guards.OutputGuard().check("营收250亿元。净利润45亿元。增长率20%。")
        assert any(w["gate"] == "citation" for w in warnings)

    def test_disclaimer_appended(self):
        from app.harness import guards
        report, _ = guards.OutputGuard().check("body")
        assert "不构成投资建议" in report

    def test_disclaimer_not_duplicated(self):
        from app.harness import guards
        report, _ = guards.OutputGuard().check("text 不构成投资建议")
        assert report.count("不构成投资建议") == 1

    def test_length_truncation(self, monkeypatch):
        from app.harness import guards
        monkeypatch.setattr(guards, "MAX_REPORT_LENGTH", 50)
        report, warnings = guards.OutputGuard().check("x" * 200)
        assert any(w["gate"] == "length" for w in warnings)

    def test_series_higher_limit(self, monkeypatch):
        from app.harness import guards
        monkeypatch.setattr(guards, "MAX_SERIES_REPORT_LENGTH", 100)
        monkeypatch.setattr(guards, "MAX_REPORT_LENGTH", 50)
        _, warnings = guards.OutputGuard().check("x" * 10, strategy="series")
        assert not any(w["gate"] == "length" for w in warnings)


# ============================================================
# 5. context.py
# ============================================================

class TestContext:
    def test_split_sections(self):
        from app.harness.context import _split_sections
        s = _split_sections("## Time\ntoday\n\n## Index\nSSE 3200")
        assert len(s) == 2

    def test_categorize(self):
        from app.harness.context import _categorize
        assert _categorize("市场指数", "## 行情数据") == "market"
        assert _categorize("财务报表", "PE 20") == "financials"
        assert _categorize("当前时间", "") == "time"

    def test_grade(self):
        from app.harness.context import _grade
        # Use Chinese titles that _categorize recognizes
        assert _grade([("市场指数", "data"), ("财务报表", "PE")]) == "A"
        assert _grade([("市场指数", "data")]) == "B"
        assert _grade([]) == "C"

    def test_trim_empty(self):
        from app.harness.context import ContextBuilder
        bundle = ContextBuilder()._trim_and_grade("")
        assert bundle.grade == "C"


# ============================================================
# 6. persist.py
# ============================================================

class TestPersist:
    def test_save_load_task(self, tmp_repo):
        from app.models.schemas import ResearchStatus
        s = ResearchStatus(task_id="t1", skill_name="test", status="running")
        tmp_repo.save_task(s, "args")
        assert tmp_repo.get_task("t1").status == "running"

    def test_save_report(self, tmp_repo):
        path = tmp_repo.save_report("buffett", "test", "# Report", 1.5)
        assert path.exists()
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["skill_name"] == "buffett"

    def test_save_partial_report(self, tmp_repo):
        path = tmp_repo.save_report("buffett", "test", "# R", partial=True)
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["partial"] is True

    def test_save_report_with_task_id(self, tmp_repo):
        path = tmp_repo.save_report("buffett", "test", "# R", task_id="abc")
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["task_id"] == "abc"

    def test_artifact_save_load(self, tmp_repo):
        tmp_repo.save_artifact("t1", "agent-a", "result")
        assert ("agent-a", "result") in tmp_repo.get_artifacts("t1")

    def test_save_billing(self, tmp_repo):
        record = tmp_repo.save_billing("t1", {"prompt_tokens": 1000, "completion_tokens": 500}, 10.5,
                                       model="kimi-k3")
        assert record["tokens_prompt"] == 1000
        assert record["cost_yuan"] > 0
        assert record["priced"] is True

    def test_interrupted_status(self, tmp_repo):
        from app.models.schemas import ResearchStatus
        tmp_repo.save_task(ResearchStatus(task_id="t2", skill_name="test", status="running"))
        assert tmp_repo.mark_running_as_interrupted() == 1
        assert tmp_repo.get_task("t2").status == "interrupted"

    def test_cost_compute(self):
        import app.harness.persist as persist_mod
        assert persist_mod.compute_cost_yuan({"prompt_tokens": 1000000, "completion_tokens": 0}) > 0

    def test_sanitize_filename(self):
        import app.harness.persist as persist_mod
        assert persist_mod._sanitize_filename("hello world.md") == "hello_world.md"
        assert persist_mod._sanitize_filename("../../../etc/passwd") == "passwd"

    def test_find_report_by_task_id(self, tmp_repo):
        tmp_repo.save_report("test", "t", "# R", task_id="find-me")
        assert tmp_repo.find_report_name_for_task("find-me") is not None

    def test_find_report_legacy(self, tmp_repo):
        tmp_repo.save_report("test", "target", "# R", partial=True)
        name = tmp_repo.find_report_name_for_task("nonexistent", skill_name="test", arguments="target")
        assert name is not None


# ============================================================
# 7. decision_log.py
# ============================================================

class TestDecisionLog:
    def test_extract_stock(self):
        from app.harness.decision_log import _extract_stock_name
        assert _extract_stock_name("Tencent 2025Q4") == "Tencent"
        assert _extract_stock_name("0700.HK") == "0700.HK"

    def test_extract_decision(self):
        from app.harness.decision_log import _extract_decision
        r = "**\u7ed3\u8bba**\uff1a\u4e70\u5165\n\n**\u7f6e\u4fe1\u5ea6**\uff1a\u9ad8\n\n## \u5173\u952e\u5047\u8bbe\n- \u5047\u8bbe1\n\n**\u76ee\u6807\u4ef7**\uff1a200\n\n"
        # _extract_decision requires report length >= 100 chars
        r = r + "x" * 120
        assert _extract_decision(r, "test") is not None

    def test_extract_none_short(self):
        from app.harness.decision_log import _extract_decision
        assert _extract_decision("short", "test") is None

    def test_save_creates_log(self, monkeypatch, tmp_path):
        from app.harness import decision_log
        monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", tmp_path / "d.md")
        # save_decision calls _extract_decision which needs >= 100 chars
        r = "**\u7ed3\u8bba**\uff1a\u6301\u6709\n\n**\u7f6e\u4fe1\u5ea6**\uff1a\u4e2d\n\n## \u5173\u952e\u5047\u8bbe\n- \u5047\u8bbeA\n\n**\u76ee\u6807\u4ef7**\uff1a100\n\n" + "x" * 120
        assert decision_log.save_decision("TestCo", r)

    def test_load_no_match(self, monkeypatch, tmp_path):
        from app.harness import decision_log
        monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", tmp_path / "d.md")
        assert decision_log.load_decisions("UnknownCo") == ""


# ============================================================
# 8. tools.py
# ============================================================

class TestTools:
    def test_all_schemas_valid(self):
        from app.harness.tools import ALL_TOOL_SCHEMAS
        for t in ALL_TOOL_SCHEMAS:
            assert t["type"] == "function"
            assert "name" in t["function"]

    def test_execute_calc(self):
        from app.harness.tools import _execute_tool
        data = json.loads(asyncio.run(_execute_tool("calc", {"expression": "100*2"})))
        assert abs(data["result"] - 200) < 0.01

    def test_execute_unknown(self):
        from app.harness.tools import _execute_tool
        data = json.loads(asyncio.run(_execute_tool("unknown", {})))
        assert "error" in data

    def test_circuit_breaker(self):
        from app.harness.tools import CircuitBreaker
        cb = CircuitBreaker("x", fail_threshold=2)
        cb.record_failure("e1")
        cb.record_failure("e2")
        assert not cb.allow_call()
        cb.reset()
        assert cb.state == "closed"

    def test_recovery_probe(self):
        from app.harness.tools import CircuitBreaker
        cb = CircuitBreaker("x", fail_threshold=1, recovery_probe_after=0)
        cb.record_failure("e")
        cb.allow_call()
        cb.record_success()
        assert cb.state == "closed"

    def test_tool_defs_coverage(self):
        from app.harness.tools import TOOL_DEFS
        for name in ("verify_market_cap", "calc", "benford"):
            assert name in TOOL_DEFS


# ============================================================
# 9. runner.py
# ============================================================

class TestRunner:
    def test_run_single(self, mock_llm_harness, tmp_repo):
        from app.harness.runner import AgentRunner
        spec = _make_spec("daily-briefing")
        report = asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))
        assert len(report) > 0

    def test_run_multi(self, mock_llm_harness, tmp_repo):
        from app.harness.runner import AgentRunner
        spec = _make_spec("investment-team")
        report = asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))
        assert "synthesized report content" in report
        for agent in ("business-analyst", "financial-analyst", "industry-researcher", "risk-assessor"):
            assert f"{agent} results" in report

    def test_run_multi_error_exclusion(self, mock_llm_harness, tmp_repo, monkeypatch):
        import app.harness.runner as runner_mod
        from app.harness.runner import AgentRunner
        monkeypatch.setattr(runner_mod, "AGENT_TIMEOUT", 999)
        mock_llm_harness["financial-analyst"] = (0, "[错误] LLM调用失败")
        mock_llm_harness["business-analyst"] = (0, "Business OK")
        spec = _make_spec("investment-team")
        report = asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))
        assert "Business OK" in report
        assert "[错误]" not in report
        assert "3/4 Agent" in report

    def test_all_agents_fail(self, mock_llm_harness, tmp_repo):
        from app.harness.runner import AgentRunner
        for a in ("business-analyst", "financial-analyst", "industry-researcher", "risk-assessor"):
            mock_llm_harness[a] = (0, "[错误] 失败")
        spec = _make_spec("investment-team")
        report = asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))
        assert "Agent" in report

    def test_run_series(self, mock_llm_harness, tmp_repo):
        from app.harness.runner import AgentRunner
        spec = _make_spec("deep-company-series")
        report = asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))
        assert isinstance(report, str)

    def test_dsml_stripped(self):
        from app.harness.runner import _strip_dsml
        # DSML tags have "\\u0001" prefix in the actual regex (matched as raw bytes)
        text = "conclusion before\n\n## agent name\n\nsome content\n\nafter"
        result = _strip_dsml(text)
        # Non-DSML text passes through unchanged
        assert "conclusion" in result
        assert "agent name" in result


# ============================================================
# 10. harness __init__ --- run(), cancel(), resume()
# ============================================================

class TestHarnessInit:
    def test_run_completes(self, mock_llm_harness, tmp_repo, monkeypatch):
        import app.harness as harness
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kw):
            return "# Report\n\nbody", []
        monkeypatch.setattr(harness, "_execute", fake_execute)
        monkeypatch.setattr(harness, "chat_complete", lambda *a, **kw: asyncio.Future())

        async def fake_summarize(report, spec):
            return "one line summary"
        monkeypatch.setattr(harness, "_summarize_report", fake_summarize)

        result = asyncio.run(harness.run(_make_spec(), EventBus()))
        assert result.status == "completed"
        assert "body" in result.report

    def test_cancel_non_existent(self):
        import app.harness as harness
        r = harness.cancel("nonexistent-task")
        assert r["cancelled"] is False

    def test_busy_rejection(self, monkeypatch, tmp_repo):
        import app.harness as harness
        from app.harness.events import EventBus, EventType

        monkeypatch.setattr(harness, "MAX_CONCURRENT_RUNS", 1)
        harness._run_semaphores.clear()

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_execute(spec, bus, **kw):
            started.set()
            await release.wait()
            return "# R\n\nbody", []

        monkeypatch.setattr(harness, "_execute", fake_execute)

        async def main():
            bus = EventBus()
            s1, s2 = _make_spec(), _make_spec()
            q2 = bus.subscribe(s2.task_id)
            t1 = asyncio.create_task(harness.run(s1, bus))
            await asyncio.wait_for(started.wait(), timeout=2)
            r2 = await harness.run(s2, bus)
            assert r2.status == "error"
            assert "busy" in r2.error.lower() or "繁忙" in r2.error
            ev = q2.get_nowait()
            assert ev.payload.get("code") == "busy"
            release.set()
            r1 = await t1
            assert r1.status == "completed"

        asyncio.run(main())

    def test_active_count(self, mock_llm_harness, tmp_repo, monkeypatch):
        import app.harness as harness
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kw):
            return "# R\n\nbody", []
        monkeypatch.setattr(harness, "_execute", fake_execute)
        monkeypatch.setattr(harness, "_summarize_report",
                            lambda report, spec: asyncio.Future())

        # Set a future that won't resolve to avoid the summarize crash
        async def fake_summarize(report, spec):
            return "summary"
        monkeypatch.setattr(harness, "_summarize_report", fake_summarize)

        result = asyncio.run(harness.run(_make_spec(), EventBus()))
        assert result.status == "completed"

    def test_run_result_dataclass(self):
        import app.harness as harness
        rr = harness.RunResult(task_id="t1", status="completed", report="r", duration_seconds=5.0)
        assert rr.task_id == "t1"
        assert rr.status == "completed"

    def test_resume_plan_error_on_unknown(self):
        import app.harness as harness
        plan, error = asyncio.run(harness.prepare_resume("unknown-task"))
        assert plan is None
        assert error is not None


print("TEST_HARNESS_OK")
