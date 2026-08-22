"""ContextBuilder — wraps core.context.build_full_context with token
budgets, A/B/C information-richness grading, and a context_ready event.

The underlying collection logic (time / market / company / web search /
skill knowledge / master knowledge) stays in core.context; this builder
owns:
- per-category token budgets (chars/4 rough tokens) with trimming
- information-richness grade:
    A (充裕) — market + financials + news all present
    B (适中) — partial data
    C (稀缺) — retrieval conclusions only → hint first-principles mode
- a context_ready event so the frontend/eval can see what was injected
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.context import build_full_context
from ..core.goal_parser import parse_goal
from .decision_log import load_decisions as _load_decisions
from .events import EventBus, EventType

logger = logging.getLogger("ai_berkshire.harness.context")

# tokens 级预算（粗算 字符/4）
BUDGET = {
    "market": 1500,
    "financials": 2000,
    "news": 1200,
    "skill_knowledge": 800,
    "master": 600,
    "web": 1500,
    "time": 400,
}


@dataclass
class ContextBundle:
    context: str                       # final system-prompt suffix
    grade: str = "C"                   # A / B / C
    injected: dict = field(default_factory=dict)
    truncated_tokens: int = 0
    goal_hints: Optional[dict] = None  # goal-parser output (E3: decision company hint)

    def summary(self) -> dict:
        return {
            "grade": self.grade,
            "injected": self.injected,
            "truncated_tokens": self.truncated_tokens,
        }


def _split_sections(context: str) -> list:
    """Split a composed context string into (title, body) sections."""
    sections = []
    current_title = "_preamble"
    current_lines = []
    for line in context.split("\n"):
        if line.startswith("## "):
            if current_lines:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = line[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_title, "\n".join(current_lines)))
    return sections


def _categorize(title: str, body: str) -> str:
    t = title.lower()
    if "时间" in title or "time" in t:
        return "time"
    if "指数" in title or "行情" in title:
        return "market"
    if "财务" in title or "财报" in title or "PE" in body[:200]:
        return "financials"
    if "新闻" in title or "news" in t:
        return "news"
    if "搜索" in title or "网页" in title or "产业链" in title or " crawl" in t:
        return "web"
    if "能力领域" in title or "技能" in title or "框架" in title:
        return "skill_knowledge"
    if "大师" in title or "哲学" in title or "知识库" in title:
        return "master"
    return "web"


def _grade(sections: list) -> str:
    """A/B/C information-richness grade from section presence."""
    cats = {_categorize(title, body) for title, body in sections}
    has_market = "market" in cats
    has_fin = "financials" in cats
    news_count = 0
    for title, body in sections:
        if _categorize(title, body) in ("news", "web"):
            news_count += len(re.findall(r"^- \[", body, re.M))
    if has_market and (has_fin or news_count >= 3):
        return "A"
    if has_market or has_fin or news_count >= 1:
        return "B"
    return "C"


class ContextBuilder:
    BUDGET = BUDGET

    def __init__(self, bus: Optional[EventBus] = None, task_id: Optional[str] = None):
        self.bus = bus
        self.task_id = task_id or "adhoc"

    async def build(self, arguments: str, skill_name: str = "", llm_config: Optional[dict] = None) -> ContextBundle:
        """Build context with budget trimming + grading; emits context_ready.

        Long free-form goals are first run through the goal parser (LLM) to
        extract symbol/company/search hints; short inputs and any parse
        failure fall back to the legacy extraction inside build_full_context.
        """
        raw = ""
        goal_hints = None
        try:
            goal_hints = await parse_goal(arguments, llm_config)
        except Exception as e:
            logger.debug(f"ContextBuilder: goal parsing skipped: {e}")
            goal_hints = None
        try:
            raw = await build_full_context(arguments, skill_name=skill_name, goal_hints=goal_hints)
        except Exception as e:
            logger.warning(f"ContextBuilder: collection failed: {e}")

        # P3: inject past decision log entries for the same stock
        try:
            decisions = _load_decisions(arguments)
            if decisions:
                raw = decisions + "\n" + raw
        except Exception as e:
            logger.debug(f"ContextBuilder: decision log injection skipped: {e}")

        # P3.5: inject stock memory (recent reports for the same stock)
        try:
            from ..tools.stock_memory import build_memory_context
            from .decision_log import _extract_stock_name

            stock = _extract_stock_name(arguments)
            if stock:
                memory = build_memory_context(stock, limit=3)
                if memory:
                    raw = memory + "\n" + raw
        except Exception as e:
            logger.debug(f"ContextBuilder: stock memory injection skipped: {e}")

        bundle = self._trim_and_grade(raw)
        bundle.goal_hints = goal_hints

        # Grade A: 信息充裕 → 提示对核心论点做反面检验
        if bundle.grade == "A":
            bundle.context += (
                "\n\n## 信息丰富度：A（信息充裕）\n"
                "检索数据充足，请充分引用；同时对核心投资论点做反面检验："
                "列出可能推翻结论的证据与做空逻辑，给出 Bull/Bear 对称分析。"
            )
        # Grade C: scarce information → first-principles hint
        elif bundle.grade == "C":
            bundle.context += (
                "\n\n## 信息丰富度：C（信息稀缺）\n"
                "公开检索信息有限，请切换到第一性原理分析模式：从商业本质出发推演，"
                "明确标注哪些结论是推算、置信度多少，不要假装拥有数据。"
            )
        elif bundle.grade == "B":
            bundle.context += (
                "\n\n## 信息丰富度：B（信息适中）\n"
                "部分数据来自推算或缓存，请为推算数据标注置信度。"
            )

        if self.bus is not None:
            self.bus.emit(self.task_id, EventType.CONTEXT_READY, **bundle.summary())
        return bundle

    def _trim_and_grade(self, raw: str) -> ContextBundle:
        if not raw:
            return ContextBundle(context="", grade="C", injected={"empty": True})

        sections = _split_sections(raw)
        grade = _grade(sections)

        out_parts = []
        injected = {}
        truncated = 0
        for title, body in sections:
            cat = _categorize(title, body)
            budget_chars = self.BUDGET.get(cat, 1200) * 4
            injected.setdefault(cat, True)
            if len(body) > budget_chars:
                truncated += (len(body) - budget_chars) // 4
                body = body[:budget_chars] + "\n*（本节因 token 预算裁剪）*"
            out_parts.append(body)

        # news count for the event payload
        news_hits = 0
        for title, body in sections:
            if _categorize(title, body) in ("news", "web"):
                news_hits += len(re.findall(r"^- \[", body, re.M))
        injected["news"] = news_hits

        return ContextBundle(
            context="\n".join(out_parts),
            grade=grade,
            injected=injected,
            truncated_tokens=truncated,
        )
