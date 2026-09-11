"""ContextBuilder — wraps core.context.build_full_context with token
budgets, A/B/C information-richness grading, and a context_ready event.

The underlying collection logic (time / market / company / web search /
skill knowledge / master knowledge) stays in core.context; this builder
owns:
- per-category token budgets, counted precisely via tiktoken
  (cl100k_base); falls back to a CJK-aware heuristic when tiktoken is
  unavailable (中文/全角 1.5 token/字, ASCII 0.25 token/字)
- two-level trimming when a section exceeds its budget:
    L1 结构化裁剪 — markdown 表格保留表头+前几行（标注裁剪行数），
       段落按句边界截断，不切断句子
    L2 LLM 摘要压缩 — 仅 financials / news 且超预算 1.5 倍以上时触发
       （可用 ENABLE_LLM_COMPRESS 关闭），失败/超时回退 L1
- information-richness grade:
    A (充裕) — market + financials + news all present
    B (适中) — partial data
    C (稀缺) — retrieval conclusions only → hint first-principles mode
- a context_ready event so the frontend/eval can see what was injected
  (payload 含各类别精确 token 数 token_counts 与被压缩类别 compressed)
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.context import build_full_context
from ..core.goal_parser import parse_goal
from .decision_log import load_decisions as _load_decisions
from .events import EventBus, EventType

logger = logging.getLogger("ai_berkshire.harness.context")

# tokens 级预算（精确 token 计数，见 count_tokens）
BUDGET = {
    "market": 1500,
    "financials": 2000,
    "news": 1200,
    "skill_knowledge": 800,
    "master": 600,
    "web": 1500,
    "time": 400,
    # 历史决策复盘块单列预算（批C，2026-09-11）：块体积上界实测 ≈1974 token
    # （全库最大 5 条相加 + 框架开销），若与 web 共 1500 预算，尾部会被 L1 按句截断，
    # 而时间线「由旧到新」的尾部正是最新一版结论 —— 会静默丢掉最新判断。
    "decision_history": 2200,
}

# L2 LLM 摘要压缩开关：置 False 时只做 L1 结构化裁剪
ENABLE_LLM_COMPRESS = True
# 只对信息密度高的类别做 LLM 摘要
LLM_COMPRESS_CATS = {"financials", "news"}
# 超预算倍数阈值：超过 1.5 倍预算才值得动用 LLM
LLM_COMPRESS_THRESHOLD = 1.5
# LLM 摘要超时（秒），超时即回退 L1
LLM_COMPRESS_TIMEOUT = 15

# tiktoken 精确编码器（cl100k_base）；加载失败则全程走启发式
try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception as e:  # noqa: BLE001 - 任何导入/加载失败都回退启发式
    logger.warning(f"tiktoken 不可用，token 计数回退启发式: {e}")
    _ENC = None


def count_tokens(text: str) -> int:
    """精确 token 计数：优先 tiktoken(cl100k_base)，失败时用 CJK 感知启发式。

    启发式：中文/全角字符按 1.5 token/字，ASCII 按 0.25 token/字。
    （旧的 字符/4 对中文严重低估：中文实际约 1.5-2 token/字。）
    """
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:  # noqa: BLE001 - 编码异常同样回退启发式
            pass
    wide = sum(1 for ch in text if ord(ch) > 0x2E7F)  # CJK、全角、中文标点
    narrow = len(text) - wide
    return int(wide * 1.5 + narrow * 0.25 + 0.5)


# 句末标点：结构化裁剪时优先在这些位置收尾
_SENT_END = "。！？!?；;\n"


def _trim_text_sentence(text: str, budget_tokens: int) -> str:
    """把纯文本截到 token 预算内，优先在句边界收尾，不切断句子。"""
    if count_tokens(text) <= budget_tokens:
        return text
    # 二分查找最长不超限前缀
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(text[:mid]) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    # 在末尾附近回退到最近的句末标点
    for i in range(len(cut) - 1, max(len(cut) - 200, -1), -1):
        if cut[i] in _SENT_END:
            cut = cut[: i + 1]
            break
    return cut.rstrip("\n")


def _structured_trim(body: str, budget_tokens: int) -> str:
    """L1 结构化裁剪：按语义边界裁剪超预算的 section。

    - markdown 表格（>=3 行以 | 开头）：保留表头+分隔行与前若干数据行，
      并标注「共 N 行，已裁剪 M 行」
    - 其余段落：在 token 预算内按句边界截断
    """
    if count_tokens(body) <= budget_tokens:
        return body
    lines = body.split("\n")
    tbl_start = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("|")), None
    )
    n_tbl = sum(1 for ln in lines if ln.strip().startswith("|"))
    if tbl_start is not None and n_tbl >= 3:
        head = lines[: tbl_start + 2]            # 表头行 + 分隔行
        rows = lines[tbl_start + 2 :]            # 数据行（可能含表尾文字）
        kept: list = []
        reserve = 30                             # 给裁剪标注预留 token
        for row in rows:
            if count_tokens("\n".join(head + kept + [row])) > max(
                budget_tokens - reserve, 1
            ):
                break
            kept.append(row)
        total = sum(1 for r in rows if r.strip().startswith("|"))
        cut_n = sum(1 for r in kept if r.strip().startswith("|"))
        note = f"\n*（表格共 {total} 行，已裁剪 {max(total - cut_n, 0)} 行）*"
        return "\n".join(head + kept).rstrip("\n") + note
    cut = _trim_text_sentence(body, max(budget_tokens - 15, 1))
    return cut + "\n*（本节因 token 预算裁剪）*"


@dataclass
class ContextBundle:
    context: str                       # final system-prompt suffix
    grade: str = "C"                   # A / B / C
    injected: dict = field(default_factory=dict)
    truncated_tokens: int = 0
    goal_hints: Optional[dict] = None  # goal-parser output (E3: decision company hint)
    token_counts: dict = field(default_factory=dict)  # 各类别注入后的精确 token 数
    compressed: list = field(default_factory=list)    # 经过 LLM 摘要压缩的类别

    def summary(self) -> dict:
        return {
            "grade": self.grade,
            "injected": self.injected,
            "truncated_tokens": self.truncated_tokens,
            "token_counts": self.token_counts,
            "compressed": self.compressed,
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
    # 批C：历史决策复盘块单列一类（不与 web 共预算，避免尾部最新结论被截）
    if "历史决策复盘" in title:
        return "decision_history"
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

        bundle = await self._trim_and_grade(raw, llm_config)
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

    async def _llm_compress(
        self, cat: str, body: str, budget_tokens: int, llm_config: Optional[dict]
    ) -> Optional[str]:
        """L2：用快速 LLM 把超预算 section 压缩进预算。

        保留所有数字与事实，去掉修饰。失败/超时返回 None，由调用方回退 L1。
        摘要调用的 token 消耗由 core.llm.chat_complete 内部的 _record_usage
        统一计入现有 usage 追踪，这里不再单独记账。
        """
        try:
            from ..core.llm import chat_complete

            system = (
                "你是金融数据压缩器。把用户给出的资料压缩到指定 token 预算内。"
                "硬性要求：保留所有数字、日期、金额、比率与事实结论；"
                "删除修饰性语句、重复表述与背景铺垫；"
                "输出压缩后的资料本身，不要解释、不要加前后缀。"
            )
            user = f"【token 预算：{budget_tokens}】\n{body}"
            result = await asyncio.wait_for(
                chat_complete(system, user, temperature=0.2, llm_config=llm_config),
                timeout=LLM_COMPRESS_TIMEOUT,
            )
            result = (result or "").strip()
            if not result:
                return None
            return result
        except Exception as e:  # noqa: BLE001 - 超时/网络/解析异常统一回退 L1
            logger.warning(f"ContextBuilder: LLM 压缩 {cat} 失败，回退结构化裁剪: {e}")
            return None

    async def _trim_and_grade(self, raw: str, llm_config: Optional[dict] = None) -> ContextBundle:
        if not raw:
            return ContextBundle(context="", grade="C", injected={"empty": True})

        sections = _split_sections(raw)
        grade = _grade(sections)

        out_parts = []
        injected = {}
        truncated = 0
        token_counts = {}
        compressed = []
        for title, body in sections:
            cat = _categorize(title, body)
            budget = self.BUDGET.get(cat, 1200)
            injected.setdefault(cat, True)
            tokens = count_tokens(body)
            if tokens > budget:
                new_body = None
                # L2：信息密度高的类别且超预算 1.5 倍以上 → 先尝试 LLM 摘要
                if (
                    ENABLE_LLM_COMPRESS
                    and cat in LLM_COMPRESS_CATS
                    and tokens > budget * LLM_COMPRESS_THRESHOLD
                ):
                    new_body = await self._llm_compress(cat, body, budget, llm_config)
                    if new_body is not None:
                        # LLM 偶尔压不进预算，兜底再做一次 L1
                        if count_tokens(new_body) > budget:
                            new_body = _structured_trim(new_body, budget)
                        compressed.append(cat)
                # L1：结构化裁剪（LLM 未触发或失败时的回退路径）
                if new_body is None:
                    truncated += tokens - budget
                    new_body = _structured_trim(body, budget)
                body = new_body
                tokens = count_tokens(body)
            token_counts[cat] = token_counts.get(cat, 0) + tokens
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
            token_counts=token_counts,
            compressed=compressed,
        )
