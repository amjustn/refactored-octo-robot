#!/usr/bin/env python3
"""daily-briefing DSML 泄漏三层修复的回归测试（全 mock，零真实 LLM）。

覆盖：
  1. _strip_dsml 各变体（无前缀/全角/混用/整块/孤立标签）剥离且幂等
  2. OutputGuard 对工具标记残留判 fatal
  3. run_single 对 tools_enabled 技能路由到 chat_complete_with_tools，
     未开启技能仍走老路径，agent_role 路径不变
  4. registry 中 daily-briefing 声明 tools_enabled
  5. harness run() 端到端：fatal 命中 → 任务 failed + partial 报告落盘

Run: cd <repo-root> && venv/bin/python -m pytest tests/test_dsml_leak_fix.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

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


# ============================================================
# 1. _strip_dsml 变体剥离与幂等
# ============================================================

class TestStripDsmlVariants:
    """第2层防御：各形态 DSML 工具标记必须剥干净且不误伤正文。"""

    def test_unprefixed_whole_block(self):
        from app.harness.runner import _strip_dsml
        text = (
            "前言分析\n\n"
            '<tool_calls>\n<invoke name="web_search">\n'
            '<parameter name="query">今日股市行情</parameter>\n</invoke>\n'
            "</tool_calls>\n\n后续结论"
        )
        result = _strip_dsml(text)
        assert "tool_calls" not in result
        assert "invoke" not in result
        assert "今日股市行情" not in result  # 整块含内容一起剥掉
        assert "前言分析" in result and "后续结论" in result

    def test_fullwidth_prefixed_block(self):
        from app.harness.runner import _strip_dsml
        text = (
            "开头\n"
            '<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="get_stock_price">\n'
            '<｜｜DSML｜｜parameter name="ticker">0700.HK</｜｜DSML｜｜parameter>\n'
            "</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>\n结尾"
        )
        result = _strip_dsml(text)
        assert "DSML" not in result
        assert "invoke" not in result
        assert "0700.HK" not in result
        assert "开头" in result and "结尾" in result

    def test_mixed_prefix_block(self):
        """实测泄漏样本形态：无前缀开标签 + 混用闭合（eval 证据报告）。"""
        from app.harness.runner import _strip_dsml
        text = (
            "我将严格按照您提供的研究框架执行分析流程。\n\n"
            "<tool_calls>\n"
            '<invoke name="fetch_market_indices">\n'
            '<parameter name="indices">上证指数,深证成指</parameter>\n'
            "</invoke>\n"
            '<invoke name="web_search">\n'
            '<parameter name="query">今日财经新闻</parameter>\n'
            "</invoke>\n"
            "</｜｜DSML｜｜invoke>\n"
            "</tool_calls>\n"
            "\n---\n*以上分析由AI基于公开数据生成，仅供参考。*"
        )
        result = _strip_dsml(text)
        assert "tool_calls" not in result
        assert "invoke" not in result
        assert "DSML" not in result
        assert "parameter" not in result
        assert "研究框架" in result
        assert "仅供参考" in result

    def test_orphan_tags_without_block(self):
        """孤立标签（无外层 tool_calls 块）也要剥离。"""
        from app.harness.runner import _strip_dsml
        text = (
            '正文开头 <invoke name="web_search"> 中间文字 '
            '<parameter name="query">某查询</parameter> </invoke> '
            "混用闭合 </｜｜DSML｜｜invoke> 结尾"
        )
        result = _strip_dsml(text)
        for marker in ("invoke", "parameter", "DSML"):
            assert marker not in result
        assert "正文开头" in result and "中间文字" in result and "结尾" in result

    def test_idempotent(self):
        from app.harness.runner import _strip_dsml
        text = (
            'a\n<tool_calls>\n<invoke name="web_search">'
            '<parameter name="query">q</parameter></invoke>\n</｜｜DSML｜｜invoke>\n</tool_calls>\nb'
        )
        once = _strip_dsml(text)
        twice = _strip_dsml(once)
        assert once == twice

    def test_normal_prose_untouched(self):
        """正常中文研报正文（含"工具"等词与财务数字）不受影响。"""
        from app.harness.runner import _strip_dsml
        prose = (
            "# 投研报告\n\n## 护城河分析\n\n"
            "该公司的分析工具与数据获取能力构成壁垒，营收 123.4 亿元"
            "（来源：公司公告 2024年报），同比增长 45.6%。\n\n"
            "政策工具箱仍有空间。投资有风险，决策需谨慎。"
        )
        assert _strip_dsml(prose) == prose


# ============================================================
# 2. guards 工具标记残留判 fatal
# ============================================================

class TestGuardToolMarkersFatal:
    """第3层闸门：tool_calls / ｜｜DSML｜｜ / <invoke name= 任一残留即 fatal。"""

    def _check(self, report: str):
        from app.harness.guards import OutputGuard
        guard = OutputGuard()
        _, warnings = guard.check(report, "daily-briefing", "single")
        return guard, warnings

    def test_tool_calls_marker_fatal(self):
        guard, warnings = self._check("# 报告\n\n<tool_calls>\n</tool_calls>\n正文")
        assert guard.fatals and guard.fatals[0]["gate"] == "tool_markers"
        assert any(w["gate"] == "tool_markers" for w in warnings)

    def test_dsml_prefix_marker_fatal(self):
        guard, _ = self._check("# 报告\n\n残留 </｜｜DSML｜｜invoke> 标记")
        assert guard.fatals

    def test_invoke_name_marker_fatal(self):
        guard, _ = self._check('# 报告\n\n<invoke name="web_search"> 残留')
        assert guard.fatals

    def test_case_insensitive(self):
        guard, _ = self._check("# 报告\n\n<TOOL_CALLS>\n</TOOL_CALLS>")
        assert guard.fatals

    def test_clean_report_not_fatal(self):
        guard, _ = self._check(
            "# 金融日报\n\n## 一、今日市场概览\n\n上证指数 3,500.12 点"
            "（来源：交易所 2026-08），工具已正常取数。\n\n不构成投资建议。"
        )
        assert guard.fatals == []


# ============================================================
# 3. run_single 技能级工具路由
# ============================================================

class TestRunSingleToolRouting:
    """第1层治本：tools_enabled 技能路由到 chat_complete_with_tools。"""

    def _patch_llm(self, monkeypatch):
        from app.harness import runner as runner_mod
        calls = {"with_tools": [], "complete": [], "stream": []}

        async def fake_with_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
            calls["with_tools"].append({"tools": tools, "tool_executor": tool_executor})
            return "tool-loop result"

        async def fake_complete(system, user, llm_config=None, **kw):
            calls["complete"].append({})
            return "plain complete"

        async def fake_stream(system, user, llm_config=None, **kw):
            calls["stream"].append({})
            yield "piece"

        monkeypatch.setattr(runner_mod, "chat_complete_with_tools", fake_with_tools)
        monkeypatch.setattr(runner_mod, "chat_complete", fake_complete)
        monkeypatch.setattr(runner_mod, "chat_stream", fake_stream)
        return calls

    def test_tools_enabled_skill_routes_to_tool_loop(self, monkeypatch):
        from app.harness.runner import AgentRunner
        from app.harness.tools import ALL_TOOL_SCHEMAS
        calls = self._patch_llm(monkeypatch)
        runner = AgentRunner(task_id="t-route-1")
        # context 非空 → 跳过 ContextBuilder（纯路由测试）
        result = asyncio.run(runner.run_single("daily-briefing", "今日日报", context="ctx"))
        assert result == "tool-loop result"
        assert len(calls["with_tools"]) == 1
        assert calls["with_tools"][0]["tools"] is ALL_TOOL_SCHEMAS
        assert callable(calls["with_tools"][0]["tool_executor"])
        assert calls["complete"] == [] and calls["stream"] == []
        # 工具执行器以技能名注册（软循环限额按此 key 计数）
        assert "daily-briefing" in runner._tool_call_counts

    def test_tools_enabled_stream_emits_single_chunk(self, monkeypatch):
        from app.harness.events import EventBus, EventType
        from app.harness.runner import AgentRunner
        calls = self._patch_llm(monkeypatch)
        bus = EventBus()
        runner = AgentRunner(bus=bus, task_id="t-route-2")
        queue = bus.subscribe("t-route-2")
        result = asyncio.run(
            runner.run_single("daily-briefing", "今日日报", context="ctx", stream=True)
        )
        assert result == "tool-loop result"
        chunks = []
        while not queue.empty():
            ev = queue.get_nowait()
            if ev.type == EventType.CHUNK:
                chunks.append(ev.payload.get("content"))
        bus.unsubscribe("t-route-2", queue)
        # 工具循环非流式：完成后一次性发出整个结果
        assert chunks == ["tool-loop result"]

    def test_plain_skill_keeps_legacy_paths(self, monkeypatch):
        from app.harness.runner import AgentRunner
        calls = self._patch_llm(monkeypatch)
        runner = AgentRunner(task_id="t-route-3")
        # dyp-ask 未开启 tools_enabled：非流式走 chat_complete
        r1 = asyncio.run(runner.run_single("dyp-ask", "护城河是什么", context="ctx"))
        assert r1 == "plain complete"
        # 流式走 chat_stream
        r2 = asyncio.run(
            runner.run_single("dyp-ask", "护城河是什么", context="ctx", stream=True)
        )
        assert r2 == "piece"
        assert calls["with_tools"] == []
        assert len(calls["complete"]) == 1 and len(calls["stream"]) == 1

    def test_agent_role_path_unchanged(self, monkeypatch):
        from app.harness.runner import AgentRunner
        calls = self._patch_llm(monkeypatch)
        runner = AgentRunner(task_id="t-route-4")
        # agent_role 在 TOOL_ENABLED_AGENTS 中，即使技能未开 tools_enabled
        # 也走工具循环（既有行为不变）
        result = asyncio.run(
            runner.run_single("dyp-ask", "目标", agent_role="researcher", context="ctx")
        )
        assert result == "tool-loop result"
        assert len(calls["with_tools"]) == 1
        assert "researcher" in runner._tool_call_counts


# ============================================================
# 4. registry 声明
# ============================================================

class TestRegistryToolsEnabled:
    def test_daily_briefing_tools_enabled(self):
        from app.skills import get_skill
        skill = get_skill("daily-briefing")
        assert skill is not None
        assert skill.get("tools_enabled") is True
        assert skill.get("is_multi_agent") is False

    def test_no_other_skill_flagged(self):
        """tools_enabled 白名单：broker-reports(2026-08-18 function-calling) + daily-briefing。"""
        from app.skills import list_skills
        flagged = [s["name"] for s in list_skills() if s.get("tools_enabled")]
        assert sorted(flagged) == ["broker-reports", "daily-briefing"]


# ============================================================
# 5. harness run() 端到端：fatal → failed + partial 落盘
# ============================================================

class TestHarnessFatalFlow:
    def test_tool_marker_report_marks_task_failed(self, tmp_repo, monkeypatch):
        import app.harness as harness
        from app.harness.events import EventBus
        from app.harness.spec import TaskSpec
        from app.skills import get_skill

        leaked = (
            "我将严格按照您提供的研究框架执行分析流程。\n\n"
            "<tool_calls>\n"
            '<invoke name="fetch_market_indices">\n'
            '<parameter name="indices">上证指数</parameter>\n'
            "</invoke>\n</tool_calls>\n"
        )

        async def fake_execute(spec, bus, **kw):
            return leaked, []
        monkeypatch.setattr(harness, "_execute", fake_execute)

        spec = TaskSpec.build(get_skill("daily-briefing"), "今日日报")
        result = asyncio.run(harness.run(spec, EventBus()))
        assert result.status == "failed"
        assert "工具调用标记" in result.error

        # 任务落库为 failed（历史列表据此出现「⟳ 继续任务」入口）
        task = tmp_repo.get_task(spec.task_id)
        assert task is not None and task.status == "failed"

        # 破损报告已存为 partial（可审计），meta 标记 partial
        import app.harness.persist as persist_mod
        reports = list(persist_mod.REPORTS_DIR.glob("*.md"))
        assert len(reports) == 1
        assert leaked.splitlines()[0] in reports[0].read_text(encoding="utf-8")
        meta = json.loads(reports[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta.get("partial") is True

    def test_clean_report_still_completes(self, tmp_repo, monkeypatch):
        import app.harness as harness
        from app.harness.events import EventBus
        from app.harness.spec import TaskSpec
        from app.skills import get_skill

        async def fake_execute(spec, bus, **kw):
            return "# 金融日报\n\n## 一、今日市场概览\n\n正文（来源：交易所 2026-08）。", []

        async def fake_summarize(report, spec):
            return "一句话结论"

        monkeypatch.setattr(harness, "_execute", fake_execute)
        monkeypatch.setattr(harness, "_summarize_report", fake_summarize)

        spec = TaskSpec.build(get_skill("daily-briefing"), "今日日报")
        result = asyncio.run(harness.run(spec, EventBus()))
        assert result.status == "completed"
        assert "金融日报" in result.report
