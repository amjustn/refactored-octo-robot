#!/usr/bin/env python3
"""P5 交叉辩论机制修复回归测试。

覆盖 2026-09-17 二次核对报告的行动项：
1. 辩论输入改为「头+尾」截断，不再砍掉尾部结论；
2. 删除"基于这份报告的逻辑"投票语义，恢复独立判断；
3. 评审输出按边界截断，消除残句；
4. 评审者按 per-agent 配置使用异构模型（同源自评缓解）；
5. 受限取证通道（debate_tools，白名单 + 预算上限）；
6. 被评审者答辩权（debate_rebuttal）；
7. 结构化分歧条目解析（对象｜反证据｜置信度）。

Run: cd <repo-root> && venv/bin/python -m pytest tests/test_debate_review.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

DEBATE_MARKER = "请从你的专业角度审视这份报告"
REBUTTAL_MARKER = "交叉质疑"


# ==================== fixtures ====================

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
def captured_llm(monkeypatch):
    """拦截三个 LLM 入口，记录调用并返回可控的长报告 / 评审 / 答辩文本。"""
    import app.harness.runner as runner_mod
    from app.harness.context import ContextBuilder

    calls: list[dict] = []

    def _rec(kind, system, user, llm_config, tools=None, tool_executor=None):
        calls.append({
            "kind": kind,
            "system": system or "",
            "user": user or "",
            "llm_config": dict(llm_config) if llm_config else None,
            "tools": tools,
            "tool_executor": tool_executor,
        })

    async def fake_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
        _rec("tools", system, user, llm_config, tools, tool_executor)
        if DEBATE_MARKER in (system or ""):
            return "评审输出"
        return "HEAD_MARKER" + ("x" * 4000) + "TAIL_MARKER"

    async def fake_complete(system, user, llm_config=None, **kw):
        _rec("complete", system, user, llm_config)
        if DEBATE_MARKER in (system or ""):
            return "同意。\n- 财务｜毛利率口径不符｜高\n独立判断：HOLD"
        if REBUTTAL_MARKER in (system or ""):
            return "答辩：口径已核对，差异来自并表范围。"
        return "complete output"

    async def fake_stream(system, user, llm_config=None, **kw):
        _rec("stream", system, user, llm_config)
        yield "综合报告"

    monkeypatch.setattr(runner_mod, "chat_stream", fake_stream)
    monkeypatch.setattr(runner_mod, "chat_complete", fake_complete)
    monkeypatch.setattr(runner_mod, "chat_complete_with_tools", fake_tools)

    class _FakeBundle:
        context = ""

    async def _fake_build(self, arguments, skill_name=None, llm_config=None):
        return _FakeBundle()

    monkeypatch.setattr(ContextBuilder, "build", _fake_build)
    return calls


def _make_spec(skill_name="investment-team", **kw):
    from app.harness.spec import TaskSpec
    from app.skills import get_skill
    return TaskSpec.build(get_skill(skill_name), "测试目标", **kw)


def _run(spec):
    from app.harness.runner import AgentRunner
    return asyncio.run(AgentRunner(task_id=spec.task_id).run(spec))


def _debate_calls(calls):
    return [c for c in calls if DEBATE_MARKER in c["system"]]


def _rebuttal_calls(calls):
    return [c for c in calls if REBUTTAL_MARKER in c["system"]]


# ==================== 纯函数 ====================

class TestTruncationHelpers:
    def test_head_tail_keeps_both_ends(self):
        from app.harness.runner import _truncate_head_tail
        text = "HEAD" + ("m" * 5000) + "TAIL"
        out = _truncate_head_tail(text, 1200, 1800)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")
        assert "中间内容省略" in out
        assert len(out) < len(text)

    def test_head_tail_short_text_unchanged(self):
        from app.harness.runner import _truncate_head_tail
        assert _truncate_head_tail("short", 1200, 1800) == "short"

    def test_boundary_truncation_no_broken_sentence(self):
        from app.harness.runner import _truncate_at_boundary
        text = "第一句。" * 300 + "最后一句。"
        out = _truncate_at_boundary(text, 200)
        assert len(out) <= 201  # 200 + ellipsis
        assert out.endswith("…")
        assert "第一句。" in out

    def test_boundary_truncation_short_unchanged(self):
        from app.harness.runner import _truncate_at_boundary
        assert _truncate_at_boundary("短文本", 1500) == "短文本"


class TestStructuredDisputes:
    def test_parse_disputes_full(self):
        from app.harness.runner import _parse_disputes
        body = "同意。\n- 财务｜毛利率口径不符｜高\n2. 行业｜份额数据过时｜中"
        out = _parse_disputes(body)
        assert out[0] == {"target": "财务", "evidence": "毛利率口径不符", "confidence": "高"}
        assert out[1]["confidence"] == "中"

    def test_parse_disputes_old_format_returns_empty(self):
        from app.harness.runner import _parse_disputes
        assert _parse_disputes("同意，但风险提示不足。") == []

    def test_summarize_debate_includes_disputes(self):
        from app.harness.runner import _summarize_debate
        debate = (
            "\n\n### 交叉评审：financial-analyst→industry-researcher\n"
            "同意。\n- 财务｜毛利率口径不符｜高\n独立判断：BUY\n"
        )
        votes, reviews = _summarize_debate(debate)
        assert votes["BUY"] == 1
        assert reviews[0]["disputes"][0]["target"] == "财务"


# ==================== 集成（mock LLM） ====================

class TestDebateIntegration:
    def test_input_keeps_tail_and_head(self, captured_llm, tmp_repo):
        _run(_make_spec())
        debates = _debate_calls(captured_llm)
        assert debates, "应产生交叉辩论调用"
        for c in debates:
            assert "HEAD_MARKER" in c["system"]
            assert "TAIL_MARKER" in c["system"]
            assert "中间内容省略" in c["system"]

    def test_vote_semantics_removed(self, captured_llm, tmp_repo):
        _run(_make_spec())
        for c in _debate_calls(captured_llm):
            assert "不是你的个人观点" not in c["system"]
            assert "独立判断" in c["system"]

    def test_reviewer_uses_per_agent_model(self, captured_llm, tmp_repo):
        from app.harness.prompts import get_role_label
        from app.skills import get_skill

        agents = get_skill("investment-team")["agents"]
        agent_models = {a: {"model": f"model-{a}"} for a in agents}
        spec = _make_spec(llm_override={
            "model": "global-model",
            "per_agent_enabled": True,
            "agent_models": agent_models,
        })
        _run(spec)

        role_to_agent = {get_role_label(a): a for a in agents}
        debates = _debate_calls(captured_llm)
        assert debates
        for c in debates:
            role = c["system"].split("你是", 1)[1].split("。以下是", 1)[0]
            agent = role_to_agent.get(role)
            assert agent is not None, f"无法识别评审者角色: {role}"
            assert c["llm_config"]["model"] == f"model-{agent}"

    def test_skill_metadata_enables_debate_tools(self, captured_llm, tmp_repo):
        spec = _make_spec()
        assert spec.debate_tools is True, "investment-team 应在 registry 元数据默认开启 debate_tools"
        assert spec.debate_rebuttal is True, "investment-team 应在 registry 元数据默认开启 debate_rebuttal"
        _run(spec)
        assert [c for c in _debate_calls(captured_llm) if c["kind"] == "tools"]
        assert _rebuttal_calls(captured_llm), "registry 默认开启答辩权后应产生答辩调用"

    def test_explicit_flag_overrides_skill_metadata(self):
        assert _make_spec(debate_tools=False).debate_tools is False
        assert _make_spec(debate_tools=True).debate_tools is True

    def test_debate_tools_uses_whitelist(self, captured_llm, tmp_repo):
        from app.harness.tools import DEBATE_TOOL_WHITELIST

        spec = _make_spec(debate_tools=True)
        _run(spec)

        tool_debates = [c for c in _debate_calls(captured_llm) if c["kind"] == "tools"]
        assert tool_debates, "debate_tools=True 时评审应走工具通道"
        for c in tool_debates:
            names = {(s.get("function") or {}).get("name") for s in c["tools"]}
            assert names and names <= DEBATE_TOOL_WHITELIST

    def test_debate_tool_executor_budget_and_whitelist(self):
        from app.harness.runner import AgentRunner
        runner = AgentRunner()

        ex = runner._debate_tool_executor("reviewer", max_calls=0)
        denied = asyncio.run(ex("web_search", {"query": "x"}))
        assert "TOOL_NOT_ALLOWED" in denied
        # 白名单工具在预算为 0 时也直接收口，不触达网络
        exhausted = asyncio.run(ex("calc", {"expression": "1+1"}))
        assert "TOOL_BUDGET_EXHAUSTED" in exhausted

    def test_rebuttal_round_added(self, captured_llm, tmp_repo):
        spec = _make_spec(debate_rebuttal=True)
        _run(spec)

        assert _rebuttal_calls(captured_llm), "debate_rebuttal=True 时应产生答辩调用"
        synthesis = [c for c in captured_llm if c["kind"] == "stream"]
        assert synthesis and "### 答辩：" in synthesis[-1]["system"]

    def test_rebuttal_default_off(self, captured_llm, tmp_repo):
        # 显式关闭时行为与旧版一致：无答辩调用、综合不含答辩段
        _run(_make_spec(debate_rebuttal=False))
        assert not _rebuttal_calls(captured_llm)
        synthesis = [c for c in captured_llm if c["kind"] == "stream"]
        assert synthesis and "### 答辩：" not in synthesis[-1]["system"]
        assert "### 交叉评审：" in synthesis[-1]["system"]
