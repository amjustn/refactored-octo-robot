#!/usr/bin/env python3
"""全球/中国宏观分析多Agent技能测试。

验证：
1. 两个技能通过 skills registry API（list_skills/get_skill）正确注册；
2. 两个 md 文件通过 load_skill_prompt 加载且包含 $ARGUMENTS；
3. 7 个新 Agent 角色在 AGENT_ROLE_PROMPTS 与 TOOL_ENABLED_AGENTS 中均可解析；
4. TaskSpec.build 对每个技能推导出 strategy="multi" 与正确的 Agent 名单。

Run: cd . && venv/bin/python -m pytest tests/test_macro_skills.py -v
"""
import sys

sys.path.insert(0, ".")

import pytest


GLOBAL_AGENTS = ["macro-institutional-economist", "macro-forecaster", "macro-strategist"]
CHINA_AGENTS = ["cn-policy-framework", "cn-data-cycle", "cn-fx-external", "cn-structure-trend"]
ALL_NEW_AGENTS = GLOBAL_AGENTS + CHINA_AGENTS


# ==================== 1. Registry 注册 ====================

class TestRegistry:
    @pytest.mark.parametrize("name,display,agents", [
        ("global-macro-analysis", "全球宏观分析", GLOBAL_AGENTS),
        ("china-macro-analysis", "中国宏观分析", CHINA_AGENTS),
    ])
    def test_skill_registered(self, name, display, agents):
        from app.skills import get_skill, list_skills
        skill = get_skill(name)
        assert skill is not None, f"{name} not in registry"
        assert skill in list_skills()
        assert skill["display_name"] == display
        assert skill["category"] == "深度研究"
        assert skill["is_multi_agent"] is True
        assert skill["agent_count"] == len(agents)
        assert skill["agents"] == agents
        assert skill.get("input_hint"), "input_hint missing"


# ==================== 2. md 文件加载 ====================

class TestSkillPrompt:
    @pytest.mark.parametrize("name", ["global-macro-analysis", "china-macro-analysis"])
    def test_md_loads_with_arguments_placeholder(self, name):
        from app.skills import load_skill_prompt
        prompt = load_skill_prompt(name)
        assert "$ARGUMENTS" in prompt
        # md 真实加载（非 fallback 生成）：fallback 不含团队结构表
        assert "团队结构" in prompt

    @pytest.mark.parametrize("name,agents", [
        ("global-macro-analysis", GLOBAL_AGENTS),
        ("china-macro-analysis", CHINA_AGENTS),
    ])
    def test_md_mentions_all_agents(self, name, agents):
        from app.skills import load_skill_prompt
        prompt = load_skill_prompt(name)
        for agent in agents:
            assert agent in prompt, f"{agent} not mentioned in {name}.md"


# ==================== 3. 角色 prompt 与工具使能 ====================

class TestAgentRoles:
    @pytest.mark.parametrize("agent", ALL_NEW_AGENTS)
    def test_role_prompt_resolves(self, agent):
        from app.harness.prompts import AGENT_ROLE_PROMPTS, get_role_prompt
        prompt = get_role_prompt(agent)
        assert prompt, f"{agent} missing in AGENT_ROLE_PROMPTS"
        assert AGENT_ROLE_PROMPTS[agent] is prompt
        assert len(prompt) > 100, f"{agent} role prompt suspiciously short"

    @pytest.mark.parametrize("agent", ALL_NEW_AGENTS)
    def test_role_tool_enabled(self, agent):
        from app.harness.tools import TOOL_ENABLED_AGENTS
        assert agent in TOOL_ENABLED_AGENTS, f"{agent} missing in TOOL_ENABLED_AGENTS"


# ==================== 4. TaskSpec 推导 ====================

class TestTaskSpec:
    @pytest.mark.parametrize("name,agents", [
        ("global-macro-analysis", GLOBAL_AGENTS),
        ("china-macro-analysis", CHINA_AGENTS),
    ])
    def test_build_derives_multi_strategy(self, name, agents):
        from app.skills import get_skill
        from app.harness.spec import TaskSpec
        spec = TaskSpec.build(get_skill(name), arguments="测试宏观问题")
        assert spec.strategy == "multi"
        assert spec.agent_names == tuple(agents)
        assert spec.skill_name == name
