#!/usr/bin/env python3
"""per-skill Team Lead 综合 prompt 覆盖（SKILL_SYNTHESIS_PROMPTS）功能测试。

用 mock 验证：
1. get_synthesis_prompt 对两个宏观技能返回覆盖 prompt，对其他技能返回
   TEAM_LEAD_PROMPT（默认回归）；
2. run_multi Phase 2 的 Team Lead 综合调用在 skill=global-macro-analysis /
   china-macro-analysis 时收到的 system prompt 为宏观覆盖版（概率化情景树等），
   而非个股研报版 TEAM_LEAD_PROMPT；
3. 回归：investment-team 的综合调用仍使用 TEAM_LEAD_PROMPT。

"""
import os
import sys
import asyncio

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


MACRO_SKILLS = ["global-macro-analysis", "china-macro-analysis"]


# ==================== 1. getter 单元行为 ====================

class TestGetSynthesisPrompt:
    def test_global_macro_override(self):
        from app.harness.prompts import get_synthesis_prompt, SKILL_SYNTHESIS_PROMPTS
        prompt = get_synthesis_prompt("global-macro-analysis")
        assert prompt is SKILL_SYNTHESIS_PROMPTS["global-macro-analysis"]
        # 宏观形状的关键构件
        for kw in ["概率化情景树", "触发点", "跨资产", "Regime", "证伪", "局限性"]:
            assert kw in prompt, f"global-macro-analysis 综合 prompt 缺少「{kw}」"
        # 明确禁止个股评分
        assert "个股" in prompt and "严禁" in prompt

    def test_china_macro_override(self):
        from app.harness.prompts import get_synthesis_prompt, SKILL_SYNTHESIS_PROMPTS
        prompt = get_synthesis_prompt("china-macro-analysis")
        assert prompt is SKILL_SYNTHESIS_PROMPTS["china-macro-analysis"]
        for kw in ["政策反应函数", "宽货币", "宽信用", "政策底", "高频证据链",
                   "汇率", "潜在增", "概率化情景树", "触发点", "证伪"]:
            assert kw in prompt, f"china-macro-analysis 综合 prompt 缺少「{kw}」"
        assert "个股" in prompt and "严禁" in prompt

    @pytest.mark.parametrize("name", ["investment-team", "earnings-team", "news-pulse"])
    def test_default_for_other_skills(self, name):
        from app.harness.prompts import get_synthesis_prompt, TEAM_LEAD_PROMPT
        assert get_synthesis_prompt(name) is TEAM_LEAD_PROMPT

    def test_default_for_none_and_unknown(self):
        from app.harness.prompts import get_synthesis_prompt, TEAM_LEAD_PROMPT
        assert get_synthesis_prompt(None) is TEAM_LEAD_PROMPT
        assert get_synthesis_prompt("") is TEAM_LEAD_PROMPT
        assert get_synthesis_prompt("no-such-skill") is TEAM_LEAD_PROMPT


# ==================== 2. runner 接线（mock LLM） ====================

@pytest.fixture
def chat_calls(monkeypatch):
    """拦截 runner 的 LLM 入口，记录 (kind, system_prompt, llm_config)。"""
    from app.harness import runner as runner_mod
    from app.harness.context import ContextBuilder

    calls = []

    async def fake_chat_stream(system, user, llm_config=None, **kw):
        calls.append(("stream", system, dict(llm_config) if llm_config else None))
        yield "流式综合内容"

    async def fake_chat_complete(system, user, llm_config=None, **kw):
        calls.append(("complete", system, dict(llm_config) if llm_config else None))
        return "完整输出"

    async def fake_chat_complete_with_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
        calls.append(("tools", system, dict(llm_config) if llm_config else None))
        return "研究结论"

    monkeypatch.setattr(runner_mod, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(runner_mod, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(runner_mod, "chat_complete_with_tools", fake_chat_complete_with_tools)

    class _FakeBundle:
        context = ""

    async def _fake_build(self, arguments, skill_name=None, llm_config=None):
        return _FakeBundle()

    monkeypatch.setattr(ContextBuilder, "build", _fake_build)
    return calls


def _synthesis_call(calls, base_prompt):
    """按综合 prompt 前缀定位 Team Lead 综合调用（其余为研究/辩论调用）。"""
    prefix = base_prompt[:40]
    lead = [c for c in calls if c[1].startswith(prefix)]
    assert len(lead) == 1, "Team Lead 综合应恰好调用一次"
    return lead[0]


def _run_multi(chat_calls, skill_name):
    from app.harness.spec import TaskSpec
    from app.harness.runner import AgentRunner
    from app.skills import get_skill

    skill = get_skill(skill_name)
    spec = TaskSpec.build(skill, "测试目标")
    report = asyncio.run(AgentRunner().run(spec))
    assert "流式综合内容" in report  # 综合结果确实进入最终报告


class TestRunnerWiring:
    def test_global_macro_uses_override(self, chat_calls):
        from app.harness.prompts import SKILL_SYNTHESIS_PROMPTS, TEAM_LEAD_PROMPT
        _run_multi(chat_calls, "global-macro-analysis")
        lead = _synthesis_call(chat_calls, SKILL_SYNTHESIS_PROMPTS["global-macro-analysis"])
        assert lead[0] == "stream", "综合应走 chat_stream"
        assert not lead[1].startswith(TEAM_LEAD_PROMPT[:40]), "宏观技能不应使用个股版 TEAM_LEAD_PROMPT"
        assert "概率化情景树" in lead[1]

    def test_china_macro_uses_override(self, chat_calls):
        from app.harness.prompts import SKILL_SYNTHESIS_PROMPTS, TEAM_LEAD_PROMPT
        _run_multi(chat_calls, "china-macro-analysis")
        lead = _synthesis_call(chat_calls, SKILL_SYNTHESIS_PROMPTS["china-macro-analysis"])
        assert lead[0] == "stream"
        assert not lead[1].startswith(TEAM_LEAD_PROMPT[:40])
        assert "政策反应函数" in lead[1]

    def test_investment_team_still_uses_default(self, chat_calls):
        from app.harness.prompts import TEAM_LEAD_PROMPT, SKILL_SYNTHESIS_PROMPTS
        _run_multi(chat_calls, "investment-team")
        lead = _synthesis_call(chat_calls, TEAM_LEAD_PROMPT)
        assert lead[0] == "stream"
        for override in SKILL_SYNTHESIS_PROMPTS.values():
            assert not lead[1].startswith(override[:40])

    def test_legacy_wrapper_uses_override(self, chat_calls):
        """旧版 run_multi_agent 入口（不传 spec）同样命中技能覆盖。"""
        from app.harness.prompts import SKILL_SYNTHESIS_PROMPTS
        from app.harness.runner import run_multi_agent

        report = asyncio.run(run_multi_agent("global-macro-analysis", "测试目标"))
        assert "流式综合内容" in report
        _synthesis_call(chat_calls, SKILL_SYNTHESIS_PROMPTS["global-macro-analysis"])
