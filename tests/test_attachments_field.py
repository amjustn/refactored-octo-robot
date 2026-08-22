"""Tests for the first-class attachments field:
- ResearchRequest schema limits (arguments 10000, attachments 30000)
- build_user_prompt appends the reference section only when attachments exist
- TaskSpec.build stores attachments
"""
import pytest
from pydantic import ValidationError

from app.models.schemas import ResearchRequest
from app.skills import build_user_prompt, get_skill
from app.harness.spec import TaskSpec


def test_arguments_over_2000_chars_accepted():
    req = ResearchRequest(skill_name="investment-research", arguments="分" * 5000)
    assert len(req.arguments) == 5000


def test_arguments_over_10000_chars_rejected():
    with pytest.raises(ValidationError):
        ResearchRequest(skill_name="investment-research", arguments="分" * 10001)


def test_attachments_field_accepted():
    req = ResearchRequest(
        skill_name="investment-research",
        arguments="分析腾讯",
        attachments="这是上传文件提取的文本" * 100,
    )
    assert req.attachments.startswith("这是上传文件")


def test_attachments_default_none():
    req = ResearchRequest(skill_name="investment-research", arguments="分析腾讯")
    assert req.attachments is None


def test_attachments_over_30000_chars_rejected():
    with pytest.raises(ValidationError):
        ResearchRequest(
            skill_name="investment-research",
            arguments="分析腾讯",
            attachments="x" * 30001,
        )


def test_build_user_prompt_with_attachments():
    prompt = build_user_prompt(
        "investment-research", "分析腾讯", attachments="2024年报关键数据：营收6600亿"
    )
    assert "## 用户上传的参考资料" in prompt
    assert "2024年报关键数据：营收6600亿" in prompt
    assert "请将上述资料作为分析的重要依据" in prompt
    # attachment block comes after the user-input section
    assert prompt.index("## 用户输入") < prompt.index("## 用户上传的参考资料")


def test_build_user_prompt_without_attachments_unchanged():
    prompt = build_user_prompt("investment-research", "分析腾讯")
    assert "## 用户上传的参考资料" not in prompt
    assert "## 用户输入" in prompt
    # byte-identical to the legacy format: arguments followed by a blank line
    assert "分析腾讯\n\n请严格按照上述框架执行分析" in prompt


def test_task_spec_build_stores_attachments():
    skill = get_skill("investment-research")
    spec = TaskSpec.build(skill, "分析腾讯", attachments="参考资料文本")
    assert spec.attachments == "参考资料文本"
    spec2 = TaskSpec.build(skill, "分析腾讯")
    assert spec2.attachments == ""
