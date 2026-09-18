"""OutputGuard — four quality gates on every generated report.

| gate      | rule                                                        | on fail |
|-----------|-------------------------------------------------------------|---------|
| structure | declared `output_sections` from the registry exist (single) | warning |
| citation  | financial-looking numbers have a source marker in-block     | warning |
| freshness | report says "需要核实" while context already has the data     | warning |
| disclaimer| report ends with a no-investment-advice statement           | auto-fix|
| length    | ≤ MAX_REPORT_LENGTH (50k; series 200k)                      | truncate|
| tool_markers | leftover <tool_calls>/｜｜DSML｜｜/<invoke name= markers   | fatal   |

tool_markers 为第3层闸门：报告残留工具调用标记说明模型对着未下发的
tools 空喊（技能未走 function-calling 循环），报告实质破损。命中时
记入 ``guard.fatals``，由 harness run() 存 partial 报告并将任务标
failed（复用 AllAgentsFailedError 路径），历史列表出现「⟳ 继续任务」。

Every warning becomes a guard_warning event (auditable) and is persisted
to tasks.guard_warnings via the Repository. Warnings never alter the
report body (no banner injection); only disclaimer append and length
truncation modify it. OutputGuard is the sole owner of length truncation
for all strategies (runner no longer truncates).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..core.config import MAX_REPORT_LENGTH
from ..skills import get_skill
from .events import EventBus, EventType

logger = logging.getLogger("ai_berkshire.harness.guards")

# Series reports legitimately run long; single definition, re-exported by runner
MAX_SERIES_REPORT_LENGTH = 200000

DISCLAIMER = "\n\n---\n*以上分析由AI基于公开数据生成，仅供参考，不构成投资建议。投资有风险，决策需谨慎。*"

# Words that count as a source attribution near a number.
# 机构名本身就是出处（"交银国际给出1600元"/"中信证券指出"），旧的词表只认
# "来源/公告/年报"这类字样，会把写明机构名的句子误判成无来源。
_SOURCE_MARKERS = re.compile(
    r"(来源|数据来源|据|截至|报表|财报|公告|年报|季报|交易所|官网|"
    r"[\u4e00-\u9fff]{2,4}(?:证券|建投|国际|投行|资管|研究院|研究所|基金)|"
    r"akshare|yfinance|yahoo|雅虎|腾讯|新浪|东财|东方财富|wind|同花顺|"
    r"Morgan|Goldman|Citi|UBS|Macquarie|Nomura|CLSA|"
    r"20\d{2}[年/-]|Q[1-4])",
    re.I,
)

# Financial-looking numbers: 1,234.56 / 123亿 / 45.6% / ¥123 / HK$123
_NUMBER = re.compile(
    r"(?:¥|HK\$|\$)?\d[\d,]*\.?\d*\s?(?:亿|万|%|元|港元|美元|倍)?"
)

# 工具调用标记残留（第3层闸门）：任一命中即 fatal。
# 覆盖 DSML 全角前缀、整块 tool_calls、以及混用形态的 <invoke name=。
_TOOL_MARKER_RE = re.compile(r"(｜｜DSML｜｜|<\s*tool_calls\b|<\s*invoke\s+name\s*=)", re.I)

# 独立来源说明行（"来源：…"/"数据来源：…"），并入其上方所属块后统一判定。
_SOURCE_CAPTION = re.compile(r"^\s*[\*#>\-\s]*(?:数据|资料)?来源", re.I)

# 报告自算/派生数字的语境标记：算式运算符，或情景/假设/隐含反推等自证语境。
# 命中即豁免来源标注要求——这些数字本就无外部来源可标。
_DERIVED_MARKERS = re.compile(
    r"[=＝≈≒×÷]|情景|悲观|中性|乐观|假设|测算|推算|推导|计算|加权|期望|赔率|"
    r"目标区间|估值区间|隐含|反推|倒推|反向求解|相对现价|较现价|现价对比|"
    r"折价|溢价|上行空间|下行空间|占比|占总|仓位|持仓|权重|本报告|"
    r"交叉验证|多源比对|偏差|校验|敏感性|为支撑现价所需|退出PE|支撑现价|"
    r"我们(?:预计|测算|估算|假设|认为)|对应(?:PE|PB|市值|估值)"
)


def _iter_blocks(report: str) -> list:
    """Split a report into citation-judging blocks.

    A block is a markdown table (consecutive ``|`` lines), a heading, or a
    blank-line-delimited paragraph. A standalone source caption
    (``来源：…``) is merged into the preceding block, so a source at the
    table tail covers the whole table and a source in the table header
    covers every row. One source marker anywhere in a block covers it.
    """
    blocks: list[str] = []
    buf: list[str] = []
    in_table = False

    def flush():
        if buf:
            blocks.append("\n".join(buf))
            buf.clear()

    for raw in report.splitlines():
        line = raw.strip()
        if not line:
            flush()
            in_table = False
            continue
        if line.startswith("#"):
            flush()
            in_table = False
            blocks.append(line)
            continue
        is_table = line.startswith("|")
        if is_table != in_table:
            flush()
            in_table = is_table
        buf.append(raw)
    flush()

    merged: list[str] = []
    for block in blocks:
        if merged and _SOURCE_CAPTION.match(block.strip()):
            merged[-1] = f"{merged[-1]}\n{block}"
        else:
            merged.append(block)
    return merged


def _required_sections(skill_name: str) -> list:
    """Explicitly declared required output sections from the skill registry.

    Framework/step headings in a skill .md (``## 第一步：…``) are execution
    instructions, not report headings, so they are never inferred from the
    prompt text. A skill without ``output_sections`` is not checked.
    """
    try:
        skill = get_skill(skill_name)
    except Exception:
        return []
    if not skill:
        return []
    return [s for s in (skill.get("output_sections") or []) if isinstance(s, str) and s.strip()]


class OutputGuard:
    def __init__(self, bus: Optional[EventBus] = None, task_id: Optional[str] = None):
        self.bus = bus
        self.task_id = task_id or "adhoc"
        self.warnings: list[dict] = []
        # fatal 命中清单（非 warning）：harness run() 据此判任务失败
        self.fatals: list[dict] = []

    def _warn(self, gate: str, detail: str):
        warning = {"gate": gate, "detail": detail[:300]}
        self.warnings.append(warning)
        if self.bus is not None:
            self.bus.emit(self.task_id, EventType.GUARD_WARNING, gate=gate, detail=detail[:300])

    # ---------- gates ----------

    def check(self, report: str, skill_name: str = "", strategy: str = "single") -> tuple[str, list]:
        """Run all gates; returns (possibly modified report, warnings).

        Fatal hits are collected in ``self.fatals`` (not returned); the
        caller (harness run) turns them into a hard task failure.
        """
        self.warnings = []
        self.fatals = []
        self._gate_tool_markers(report)
        report = self._gate_structure(report, skill_name, strategy)
        self._gate_citation(report)
        self._gate_freshness(report)
        report = self._gate_disclaimer(report)
        report = self._gate_length(report, strategy)
        return report, list(self.warnings)

    def _gate_freshness(self, report: str):
        """第4层闸门（P2，2026-09-13）：报告写"需要核实"，但上下文其实已有数据。

        判据来自 harness.context 的上下文留痕（按 task_id）；没有留痕时不告警，
        避免误报。只产 warning，不改报告正文。
        """
        try:
            from .context import get_last_context
            from .freshness_gate import check_freshness

            ctx = get_last_context(self.task_id)
            for w in check_freshness(report, ctx):
                self._warn(w.get("gate", "freshness"), w.get("detail", ""))
        except Exception as e:  # noqa: BLE001 - 门闸自身异常绝不阻断出报告
            logger.debug(f"freshness gate skipped: {e}")

    def _gate_tool_markers(self, report: str):
        """第3层闸门：最终报告残留工具调用标记 → fatal。

        这些标记出现即说明模型试图调用未下发的 tools（单Agent技能未
        启用 tools_enabled 路由），报告内容实质是未执行的调用计划。
        """
        m = _TOOL_MARKER_RE.search(report)
        if m:
            fatal = {
                "gate": "tool_markers",
                "detail": f"报告含未执行的工具调用标记（{m.group(0)!r}），工具未实际执行",
            }
            self.fatals.append(fatal)
            self._warn("tool_markers", fatal["detail"])

    def _gate_structure(self, report: str, skill_name: str, strategy: str) -> str:
        if strategy != "single":
            # multi/series have their own fixed report skeletons
            if not re.search(r"^#\s", report, re.M):
                self._warn("structure", "报告缺少顶层标题")
            return report
        required = _required_sections(skill_name)
        if not required:
            return report
        missing = [s for s in required if s not in report]
        if missing:
            shown = "、".join(missing[:5])
            self._warn("structure", f"必备章节缺失（{len(missing)}/{len(required)}）：{shown}")
        return report

    def _gate_citation(self, report: str):
        unsourced = 0
        examples = []
        for block in _iter_blocks(report):
            # 块内任一处标了来源即覆盖整块（表头/表尾/段落皆然）
            if _SOURCE_MARKERS.search(block):
                continue
            # 本报告自算/情景假设数字，无外部来源可标，放行
            if _DERIVED_MARKERS.search(block):
                continue
            for m in _NUMBER.finditer(block):
                token = m.group(0)
                # only care about numbers that look financial (unit or magnitude)
                if not re.search(r"(亿|万|%|元|港元|美元|倍|,)", token):
                    continue
                unsourced += 1
                if len(examples) < 3:
                    examples.append(token.strip())
        if unsourced:
            self._warn("citation", f"{unsourced} 处财务数字块内未见来源标注，如：{'、'.join(examples)}")

    def _gate_disclaimer(self, report: str) -> str:
        tail = report[-800:]
        if "不构成投资建议" in tail or "不构成任何投资建议" in tail:
            return report
        return report + DISCLAIMER

    def _gate_length(self, report: str, strategy: str) -> str:
        limit = MAX_SERIES_REPORT_LENGTH if strategy == "series" else MAX_REPORT_LENGTH
        if len(report) > limit:
            self._warn("length", f"报告超长（{len(report)} 字符），已截断至 {limit}")
            return report[:limit] + "\n\n---\n*报告已截断（超过最大长度限制）*\n"
        return report
