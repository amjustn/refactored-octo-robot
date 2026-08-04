"""Skill loader for AI Berkshire Web

Loads skill .md prompt templates from app/skills/ directory.
Falls back to generated prompts when .md files are missing.
"""
from pathlib import Path
from typing import Optional

from .registry import SKILLS

SKILLS_DIR = Path(__file__).parent

# Cache loaded prompts
_prompt_cache: dict[str, str] = {}


def list_skills():
    """Return all registered skills."""
    return SKILLS


def get_skill(name: str) -> Optional[dict]:
    """Get skill metadata by name."""
    for s in SKILLS:
        if s["name"] == name:
            return s
    return None


def _load_md_file(name: str) -> Optional[str]:
    """Load a skill's .md prompt file."""
    md_path = SKILLS_DIR / f"{name}.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8")
    return None


def _generate_fallback_prompt(skill: dict) -> str:
    """Generate a prompt when the .md file is missing."""
    name = skill["display_name"]
    desc = skill["description"]
    hint = skill.get("input_hint", "输入分析目标")
    is_multi = skill.get("is_multi_agent", False)

    prompt = f"""# {name}

{desc}

你是一位精通价值投资的分析师，遵循巴菲特、芒格、段永平、李录四位大师的方法论。

## 分析目标
用户将提供：{hint}

## 分析要求
1. 强制给出明确结论（通过/不通过/灰色地带），不打太极
2. 使用具体数据和事实支撑分析，不泛泛而谈
3. 标注所有数据的来源和时效性
4. 信息不足时诚实标注"数据不足"，不强行填充
5. 区分"AI分析置信度"和"真实投资确定性"
"""
    if is_multi:
        agents = skill.get("agents", [])
        prompt += "\n## 多Agent角色\n"
        prompt += f"本分析由{len(agents)}个专业Agent并行执行，最终由Team Lead综合。\n"

    return prompt


def load_skill_prompt(name: str) -> str:
    """Load a skill's full prompt text. Falls back to generated if .md missing."""
    if name in _prompt_cache:
        return _prompt_cache[name]

    skill = get_skill(name)
    if not skill:
        raise ValueError(f"Unknown skill: {name}")

    # Try loading the .md file first
    prompt = _load_md_file(name)
    if prompt:
        _prompt_cache[name] = prompt
        return prompt

    # Generate fallback
    prompt = _generate_fallback_prompt(skill)
    _prompt_cache[name] = prompt
    return prompt


def build_user_prompt(skill_name: str, arguments: str, attachments: str = "") -> str:
    """Build the user-facing prompt from skill template + user args.

    attachments: extracted text from user-uploaded files. Kept separate
    from arguments (never used for context building); appended as a
    dedicated reference section when non-empty.
    """
    skill = get_skill(skill_name)
    if not skill:
        raise ValueError(f"Unknown skill: {skill_name}")

    # Replace $ARGUMENTS placeholder
    skill_prompt = load_skill_prompt(skill_name)
    skill_prompt = skill_prompt.replace("$ARGUMENTS", arguments)

    attachment_block = ""
    if attachments:
        attachment_block = (
            f"\n\n## 用户上传的参考资料\n{attachments}\n\n请将上述资料作为分析的重要依据。"
        )

    return f"""请使用以下研究框架进行分析：

{skill_prompt}

---

## 用户输入
{arguments}
{attachment_block}
请严格按照上述框架执行分析，输出结构完整的投研报告。
"""
