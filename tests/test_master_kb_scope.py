"""大师知识库作用域护栏（2026-09-13）。

用户要求：管理层深研 / 未上市公司研究 / 券商研报等技能不再使用四位投资大师
（巴菲特·芒格·段永平·李录）相关内容 —— 它们原先通过两条【共享层】通道被无条件注入：

  1. app/harness/prompts.DEFAULT_ANALYST_PROMPT （所有单 Agent 技能的默认系统提示词）
  2. app/core/context.build_knowledge_context()   （实测 4317 字符/次，21 个技能人人有份）

本文件锁死收窄后的事实，防止以后有人把白名单改回"无条件注入"：
  - 默认系统提示词不含大师名；原句只保留给"四大师综合分析"（SKILL_SYSTEM_OVERRIDES）
  - 知识块只给 MASTER_KB_SKILLS 内技能；缺省 / 非白名单 = 0 字符
  - 报告头「数据源」标注跟随同一白名单（否则报告头会做假陈述）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.context import MASTER_KB_SKILLS, build_knowledge_context, data_source_label
from app.harness.prompts import (
    DEFAULT_ANALYST_PROMPT,
    SKILL_SYSTEM_OVERRIDES,
    get_default_system_prompt,
)

MASTER_NAMES = ("巴菲特", "芒格", "段永平", "李录")

# 用户点名要求"不要用到四大师内容"的三个技能
TARGET_SKILLS = ("management-deep-dive", "private-company-research", "broker-reports")
# 本体就是四大师框架、应保留注入的技能
KEEP_SKILLS = ("investment-research", "investment-team", "earnings-team", "thesis-tracker")


def _has_master_name(text: str) -> bool:
    return any(name in text for name in MASTER_NAMES)


# ── 通道一：默认系统提示词 ──────────────────────────────────────


def test_default_prompt_has_no_master_names():
    assert not _has_master_name(DEFAULT_ANALYST_PROMPT), (
        f"默认系统提示词又带上大师名了：{DEFAULT_ANALYST_PROMPT}"
    )


def test_default_prompt_keeps_value_investing_identity():
    # 收窄的是"大师方法论"，不是"价值投资分析师"这个身份
    assert "价值投资" in DEFAULT_ANALYST_PROMPT


@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_target_skills_system_prompt_clean(skill: str):
    prompt = get_default_system_prompt(skill)
    assert not _has_master_name(prompt), f"{skill} 的系统提示词仍含大师名：{prompt}"


def test_four_masters_skill_keeps_original_override():
    # 原句从默认值挪成按技能覆盖 —— 四大师综合分析行为必须不变
    assert "investment-research" in SKILL_SYSTEM_OVERRIDES
    prompt = get_default_system_prompt("investment-research")
    assert all(name in prompt for name in MASTER_NAMES), (
        f"四大师综合分析的原句丢了：{prompt}"
    )


def test_unknown_skill_falls_back_to_clean_default():
    assert get_default_system_prompt("no-such-skill") == DEFAULT_ANALYST_PROMPT


# ── 通道二：知识库注入作用域 ────────────────────────────────────


def test_master_kb_whitelist_contents():
    assert set(KEEP_SKILLS) <= MASTER_KB_SKILLS, "白名单漏了本体是四大师框架的技能"
    for skill in TARGET_SKILLS:
        assert skill not in MASTER_KB_SKILLS, f"{skill} 不该在白名单内"


@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_target_skills_get_no_master_knowledge(skill: str):
    assert build_knowledge_context(skill) == "", f"{skill} 仍在注入大师知识块"


@pytest.mark.parametrize("skill", KEEP_SKILLS)
def test_keep_skills_still_get_master_knowledge(skill: str):
    block = build_knowledge_context(skill)
    assert block, f"{skill} 应该有大师知识块，实际为空"
    assert _has_master_name(block)


def test_missing_skill_name_injects_nothing():
    """缺省必须关闭 —— 防止"没传技能名就无条件注入"的回归。"""
    assert build_knowledge_context() == ""


def test_other_skills_get_no_master_knowledge():
    for skill in ("daily-briefing", "industry-research", "portfolio-review", "earnings-review", "industry-funnel"):
        assert build_knowledge_context(skill) == "", f"{skill} 不该注入大师知识块"


# ── 报告头「数据源」标注必须跟随同一白名单 ──────────────────────


@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_report_header_does_not_claim_master_kb(skill: str):
    label = data_source_label(skill)
    assert "投资大师知识库" not in label, f"{skill} 报告头仍在声称使用大师知识库"
    assert label == "实时市场数据"


@pytest.mark.parametrize("skill", KEEP_SKILLS)
def test_report_header_claims_master_kb_for_whitelist(skill: str):
    assert "投资大师知识库" in data_source_label(skill)


def test_report_header_default_no_master_kb():
    assert "投资大师知识库" not in data_source_label()


# ── 技能文件自带的负向约束（防模型自发引大师语录）────────────────


@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_target_skill_files_carry_output_discipline(skill: str):
    md = Path(__file__).resolve().parents[1] / "app" / "skills" / f"{skill}.md"
    text = md.read_text(encoding="utf-8")
    assert "输出纪律" in text, f"{skill}.md 缺少输出纪律段落"
    assert "不得引用" in text, f"{skill}.md 的输出纪律没写「不得引用」"
