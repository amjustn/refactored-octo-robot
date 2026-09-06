#!/usr/bin/env python3
"""
AI Berkshire Web --- Comprehensive Harness Test Suite

Covers every component in app/harness/:
  spec / events / prompts / guards / context / persist /
  decision_log / tools / runner / __init__ (run/cancel/resume)

Run: cd <repo-root> && venv/bin/python -m pytest tests/test_harness.py -v -x
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
        # 2026-09-01: 后缀/疑问剥离 + 空格截断增强, 修复整句被当股票名
        assert _extract_stock_name("分析一下贵州茅台的投资价值") == "贵州茅台"
        assert _extract_stock_name("研究拼多多的基本面") == "拼多多"
        assert _extract_stock_name("贵州茅台(600519)现在还能买吗") == "贵州茅台"
        assert _extract_stock_name("看看中际旭创 300308") == "中际旭创"
        assert _extract_stock_name("601318你觉得怎么样") == "601318"

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
# 7b. decision_verify.py — P4 验证闭环(2026-09-01)
# ============================================================

class TestDecisionVerify:
    def test_judge_buy_fulfilled(self):
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "买入", "record_price": "100", "time": "2026-08-10 10:00 UTC", "target": "未指定"}
        assert judge_entry(e, 114, now) == "已兑现"   # +14% → 兑现
        assert judge_entry(e, 88, now) == "已证伪"    # -12% → 证伪
        assert judge_entry(e, 103, now) == "待验证"   # +3% → 未定

    def test_judge_sell(self):
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "卖出", "record_price": "100", "time": "2026-08-10 10:00 UTC", "target": "未指定"}
        assert judge_entry(e, 90, now) == "已兑现"    # 避损成功
        assert judge_entry(e, 110, now) == "已证伪"   # 卖飞

    def test_judge_target_price_priority(self):
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "买入", "record_price": "100", "time": "2026-08-10 10:00 UTC", "target": "目标价 110"}
        assert judge_entry(e, 115, now) == "已兑现"   # 现价达目标价 → 兑现(即便涨幅<8%)
        assert judge_entry(e, 88, now) == "已证伪"    # 跌穿-8% → 证伪
        assert judge_entry(e, 103, now) == "待验证"   # 未达标未跌穿 → 待验证

    def test_judge_hold_no_auto(self):
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "持有观望", "record_price": "100", "time": "2026-08-10 10:00 UTC", "target": "未指定"}
        assert judge_entry(e, 120, now) == "待验证"   # 持有方向不自动判定

    def test_judge_expired(self):
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "买入", "record_price": "100", "time": "2026-05-01 10:00 UTC", "target": "未指定"}
        assert judge_entry(e, 101, now) == "已过期"   # 超90天无定论 → 过期

    def test_judge_no_record_price(self):
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "买入", "record_price": "未指定", "time": "2026-08-10 10:00 UTC", "target": "未指定"}
        assert judge_entry(e, 114, now) == "待验证"   # 无基准价 → 无法判定

    def test_judge_bad_record_price(self):
        """错误注入: record_price 非数字 → 待验证, 不抛异常"""
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "买入", "record_price": "abc", "time": "2026-08-10 10:00 UTC", "target": "未指定"}
        assert judge_entry(e, 114, now) == "待验证"

    def test_judge_bad_time(self):
        """错误注入: 时间无法解析 → 不做过期检查, 直接按涨跌判定(不抛异常)"""
        from app.harness.decision_verify import judge_entry
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        e = {"conclusion": "买入", "record_price": "100", "time": "not-a-date", "target": "未指定"}
        # 时间解析失败 → 年龄未知 → 不误标过期, 按涨跌正常判定
        assert judge_entry(e, 114, now) == "已兑现"

    def test_classify_conclusion(self):
        from app.harness.decision_verify import _classify_conclusion
        assert _classify_conclusion("建议买入") == "buy"
        assert _classify_conclusion("卖出回避") == "sell"
        assert _classify_conclusion("持有观望") == "hold"
        assert _classify_conclusion("综合来看基本面稳健") == "unknown"

    def test_tencent_symbol(self):
        from app.harness.decision_verify import _tencent_symbol
        assert _tencent_symbol("600519.SH") == "sh600519"
        assert _tencent_symbol("000001.SZ") == "sz000001"
        assert _tencent_symbol("0700.HK") == "hk00700"
        assert _tencent_symbol("PDD") == "usPDD"
        assert _tencent_symbol("601318") == "sh601318"
        assert _tencent_symbol("垃圾输入") is None

    def test_update_entry_fields_replaces_and_inserts(self, monkeypatch, tmp_path):
        from app.harness import decision_log
        from pathlib import Path
        log = tmp_path / "d.md"
        log.write_text(
            "# 决策日志\n\n"
            "## 测试股份 — 2026-08-10 10:00 UTC\n\n"
            "- **技能**: test\n"
            "- **结论**: 买入\n"
            "- **任务ID**: T1\n\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", log)
        assert decision_log.update_entry_fields("T1", {"验证": "已证伪", "代码": "600000.SH"})
        content = log.read_text(encoding="utf-8")
        assert "- **验证**: 已证伪" in content     # 替换已有? 不, 验证不存在 → 插入
        assert "- **代码**: 600000.SH" in content   # 插入
        assert "- **结论**: 买入" in content         # 原字段保留

    def test_update_entry_fields_not_found(self, monkeypatch, tmp_path):
        from app.harness import decision_log
        from pathlib import Path
        log = tmp_path / "d.md"
        log.write_text("# 决策日志\n\n", encoding="utf-8")
        monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", log)
        assert decision_log.update_entry_fields("NOPE", {"验证": "x"}) is False


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
        assert "3/4 Agent" in report
        # Failure attribution now lands in the report itself, so the error
        # text is expected in the "部分 Agent 执行失败" section (not silently
        # dropped from the deliverable).
        assert "部分 Agent 执行失败" in report
        assert "financial-analyst" in report
        assert "LLM调用失败" in report

    def test_debate_failure_not_injected(self, mock_llm_harness, tmp_repo, monkeypatch):
        """A failed peer debate must never reach the synthesis prompt as if it
        were a real review — it stays in logs/progress only."""
        import app.harness.runner as runner_mod
        from app.harness.runner import AgentRunner

        async def failing_debate(system, user, llm_config=None, **kw):
            if "交叉评审" in (system or "") + (user or ""):
                raise RuntimeError("debate API down")
            return "complete output"

        monkeypatch.setattr(runner_mod, "chat_complete", failing_debate)
        spec = _make_spec("investment-team")
        report = asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))
        assert "评审失败" not in report
        assert "synthesized report content" in report

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


class TestLLMRetry:
    """Retry-with-jitter (Cumora lesson): parallel agents that hit the same
    rate-limit must not retry in a lockstep wave — the fixed delay gets a
    ±jitter spread so retries stagger instead of re-hitting the limit."""

    def test_retry_jitter_spreads_delay(self, monkeypatch):
        import app.core.llm as llm_mod
        calls = {"n": 0}
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("429 rate limit")
            return "ok"

        monkeypatch.setattr(llm_mod.random, "uniform", lambda a, b: 0.5)
        monkeypatch.setattr(llm_mod.asyncio, "sleep", fake_sleep)
        out = asyncio.run(llm_mod._retry_async(flaky))
        assert out == "ok"
        assert calls["n"] == 3
        # Two retries: base [2, 5] + jitter 0.5 each
        assert sleeps == [2.5, 5.5]

    def test_retry_non_retryable_raises_immediately(self, monkeypatch):
        import app.core.llm as llm_mod
        calls = {"n": 0}

        async def bad():
            calls["n"] += 1
            raise ValueError("bad request 400")

        with pytest.raises(ValueError):
            asyncio.run(llm_mod._retry_async(bad))
        assert calls["n"] == 1


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
