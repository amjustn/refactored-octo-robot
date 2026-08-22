"""OutputGuard — four quality gates on every generated report.

| gate      | rule                                                        | on fail |
|-----------|-------------------------------------------------------------|---------|
| structure | required `## ` sections from the skill md exist (single)    | warning |
| citation  | financial-looking numbers have a source marker nearby       | warning |
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
from ..skills import load_skill_prompt
from .events import EventBus, EventType

logger = logging.getLogger("ai_berkshire.harness.guards")

# Series reports legitimately run long; single definition, re-exported by runner
MAX_SERIES_REPORT_LENGTH = 200000

DISCLAIMER = "\n\n---\n*以上分析由AI基于公开数据生成，仅供参考，不构成投资建议。投资有风险，决策需谨慎。*"

# Words that count as a source attribution near a number
_SOURCE_MARKERS = re.compile(
    r"(来源|数据来源|据|截至|报表|财报|公告|年报|季报|交易所|官网|"
    r"akshare|yfinance|yahoo|雅虎|腾讯|新浪|东财|东方财富|wind|同花顺|"
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

# Sections that are framework instructions, not output headings
_STRUCTURE_SKIP = re.compile(
    r"(输入|目标|要求|工具|数据获取|框架|风格|方法|步骤|流程|原则|视角|"
    r"背景|角色|约束|示例|注意|纪律|说明|指引|指南|提示)"
)


def _required_sections(skill_name: str) -> list:
    """Parse `## ` headings from the skill md as required output sections."""
    try:
        prompt = load_skill_prompt(skill_name)
    except Exception:
        return []
    sections = []
    for m in re.finditer(r"^##\s+(.+?)\s*$", prompt, re.M):
        title = m.group(1).strip()
        title = re.sub(r"^[0-9一二三四五六七八九十]+[\.、\)]?\s*", "", title)
        if len(title) >= 2 and not _STRUCTURE_SKIP.search(title):
            sections.append(title)
    return sections


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
        report = self._gate_disclaimer(report)
        report = self._gate_length(report, strategy)
        return report, list(self.warnings)

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
        for m in _NUMBER.finditer(report):
            token = m.group(0)
            # only care about numbers that look financial (with unit or magnitude)
            if not re.search(r"(亿|万|%|元|港元|美元|倍|,)", token):
                continue
            window = report[max(0, m.start() - 80): m.end() + 80]
            if not _SOURCE_MARKERS.search(window):
                unsourced += 1
                if len(examples) < 3:
                    examples.append(token.strip())
        if unsourced:
            self._warn("citation", f"{unsourced} 处财务数字邻近未见来源标注，如：{'、'.join(examples)}")

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
