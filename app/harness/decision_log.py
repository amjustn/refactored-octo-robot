"""P3: Decision log — cross-session investment thesis tracking.

Append-only Markdown log of every investment decision made during research.
On next analysis of the same stock, past decisions are injected into context
so the LLM builds on prior work rather than starting from scratch.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ai_berkshire.harness.decision_log")

DECISION_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "decision-log.md"

_DECISION_LOG_HEADER = """# AI Berkshire 决策日志

> 每次研究完成后自动记录。下次分析同内容时自动注入上下文。
> 状态: pending（待验证）→ resolved（已验证/已过期）
> 格式: 时间 | 股票 | 结论 | 置信度 | 关键假设 | 状态

---

"""


def _ensure_log() -> Path:
    """Create decision log if it doesn't exist."""
    DECISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DECISION_LOG_PATH.exists():
        DECISION_LOG_PATH.write_text(_DECISION_LOG_HEADER, encoding="utf-8")
    return DECISION_LOG_PATH


def _one_line(s) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces — log fields
    must never carry a raw newline that could fake a section header."""
    return " ".join(str(s or "").split())


def save_decision(
    arguments: str,
    report: str,
    skill_name: str = "",
    task_id: str = "",
    company_hint: str = "",
) -> bool:
    """Extract decision from a completed report and append to decision log.

    Looks for explicit decision markers in the report, then falls back to
    extracting the first strong opinion found. Appends one entry to
    decision-log.md.

    company_hint: trusted company name from the goal parser — preferred over
    heuristic extraction from arguments.  When no credible name (>= 2
    chars after cleaning) can be determined, the entry is skipped entirely.

    Returns True if a decision was extracted and saved.
    """
    # Try to extract decision from report
    decision = _extract_decision(report, arguments)
    if not decision:
        logger.debug(f"No decision found in report for '{arguments[:60]}'")
        return False

    try:
        log_path = _ensure_log()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        # Prefer the goal parser's company hint; fall back to heuristic
        # extraction.  Names too short to be credible are skipped rather
        # than logged as garbage section headers.
        stock = _one_line(company_hint) or _extract_stock_name(arguments)
        if len(stock) < 2:
            logger.debug(f"Skip decision save: no credible stock name in '{arguments[:60]}'")
            return False

        entry = (
            f"\n## {stock} — {ts}\n\n"
            f"- **技能**: {_one_line(skill_name) or '未指定'}\n"
            f"- **结论**: {_one_line(decision['conclusion'])}\n"
            f"- **置信度**: {_one_line(decision['confidence'])}\n"
            f"- **关键假设**: {_one_line(decision['assumptions'])}\n"
            f"- **目标价/信号**: {_one_line(decision.get('target', '未指定'))}\n"
            f"- **状态**: pending\n"
            f"- **任务ID**: {_one_line(task_id)}\n"
            f"\n---\n"
        )

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)

        # P3-闭环(2026-08-28): 新决策已入库, 同股旧 pending 自动销账
        try:
            _resolve_superseded(log_path, stock, ts)
        except Exception as e:
            logger.warning(f"resolve_superseded skipped: {e}")

        logger.info(f"Decision saved: {stock} — {decision['conclusion']}")
        return True

    except Exception as e:
        logger.warning(f"Failed to save decision: {e}")
        return False


def _resolve_superseded(log_path: Path, stock: str, new_ts: str) -> int:
    """P3-闭环(2026-08-28): 同股票保存新决策时, 旧 pending 记录自动销账。

    新分析覆盖了旧观点: 无论结论是重申还是反转, 旧条目都已不是"最新待验证"
    状态 — 标记为 resolved（已被新分析覆盖）并注明覆盖来源, 避免旧 pending
    永远滞留、时效提示无限循环。

    Returns number of entries resolved.
    """
    try:
        content = log_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"resolve_superseded: cannot read log: {e}")
        return 0

    sections = re.split(r"(\n(?=## ))", content)
    resolved = 0
    # 时间戳精度为分钟, 同分钟写入的新旧条目标题完全相同, 无法靠标题区分。
    # 语义: 最新一条(该股票最后一个匹配 section)就是刚写入的新决策, 保持
    # pending; 之前所有同股 pending 一律销账 — "新分析覆盖旧观点"。
    last_match = max(
        (i for i, sec in enumerate(sections)
         if sec.startswith("## ") and f"{stock} —" in sec.split("\n")[0]),
        default=-1,
    )
    for i, sec in enumerate(sections):
        if i == last_match:
            continue
        if not sec.startswith("## ") or f"{stock} —" not in sec.split("\n")[0]:
            continue
        if "**状态**: pending" in sec:
            sections[i] = sec.replace(
                "**状态**: pending",
                f"**状态**: resolved\n- **复核**: 已被 {new_ts} 的同股新分析覆盖（自动销账）",
                1,
            )
            resolved += 1

    if resolved:
        try:
            log_path.write_text("".join(sections), encoding="utf-8")
            logger.info(f"resolve_superseded: {stock} — {resolved} 条旧决策已销账")
        except Exception as e:
            logger.warning(f"resolve_superseded: write failed: {e}")
            return 0
    return resolved


def resolve_decision_by_task_id(task_id: str, note: str = "人工复核销账") -> bool:
    """Manually resolve a single pending decision by its task ID.

    Returns True if the entry was found and resolved.
    """
    log_path = _ensure_log()
    try:
        content = log_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"resolve_decision_by_task_id: read failed: {e}")
        return False

    sections = re.split(r"(\n(?=## ))", content)
    for i, sec in enumerate(sections):
        if sec.startswith("## ") and f"**任务ID**: {task_id}" in sec:
            if "**状态**: pending" in sec:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                sections[i] = sec.replace(
                    "**状态**: pending",
                    f"**状态**: resolved\n- **复核**: {note}（{ts}）",
                    1,
                )
                try:
                    log_path.write_text("".join(sections), encoding="utf-8")
                    logger.info(f"Decision resolved by task_id {task_id}")
                    return True
                except Exception as e:
                    logger.warning(f"resolve_decision_by_task_id: write failed: {e}")
            return False
    return False


async def asave_decision(*args, **kwargs) -> bool:
    """Async wrapper — decision-log file I/O runs off the event loop."""
    return await asyncio.to_thread(save_decision, *args, **kwargs)


def list_decisions(max_entries: int = 100) -> list:
    """P3 frontend: parse decision-log.md into structured entries, newest first.

    Each entry: {stock, time, skill, conclusion, confidence, assumptions,
    target, status, task_id}. Returns [] when the log does not exist.
    """
    if not DECISION_LOG_PATH.exists():
        return []

    try:
        content = DECISION_LOG_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read decision log: {e}")
        return []

    entries = []
    sections = re.split(r"\n(?=## )", content)
    for section in sections:
        section = section.strip()
        if not section.startswith("## "):
            continue
        lines = section.split("\n")
        # Title: "## 贵州茅台 — 2026-08-04 12:00 UTC"
        title = lines[0][3:].strip()
        stock, _, ts = title.partition(" — ")

        def _field(name: str) -> str:
            for ln in lines[1:]:
                m = re.match(rf"- \*\*{name}\*\*[：:]\s*(.*)", ln.strip())
                if m:
                    return m.group(1).strip()
            return ""

        entries.append({
            "stock": stock.strip(),
            "time": ts.strip(),
            "skill": _field("技能"),
            "conclusion": _field("结论"),
            "confidence": _field("置信度"),
            "assumptions": _field("关键假设"),
            "target": _field("目标价/信号"),
            "status": _field("状态") or "pending",
            "task_id": _field("任务ID"),
        })

    entries.reverse()  # file is append-only; newest last → newest first
    return entries[:max_entries]


def load_decisions(arguments: str, max_entries: int = 5) -> str:
    """Load past decisions relevant to the current analysis.

    Searches decision-log.md for entries matching the stock/company name
    extracted from arguments. Returns formatted context string for injection,
    or empty string if no relevant decisions found.
    """
    def _entry_age_days(ts: str) -> float:
        """Parse '2026-08-06 16:32 UTC' → age in days. Unparseable → inf."""
        try:
            dt = datetime.strptime(_one_line(ts), "%Y-%m-%d %H:%M UTC").replace(
                tzinfo=timezone.utc
            )
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except Exception:
            return float("inf")

    stock = _extract_stock_name(arguments)
    if not stock:
        return ""  # can't extract a meaningful stock name

    try:
        if not DECISION_LOG_PATH.exists():
            return ""

        content = DECISION_LOG_PATH.read_text(encoding="utf-8")

        # Find sections mentioning this stock (case-insensitive)
        sections = re.split(r"\n(?=## )", content)
        relevant = []
        stale_pending = 0

        for section in sections:
            if not section.strip() or not section.startswith("## "):
                continue
            if stock.lower() in section.lower():
                # Extract key info
                lines = section.strip().split("\n")
                # Keep it compact: title + first 6 detail lines
                compact = "\n".join(lines[:8])

                # P3-闭环(2026-08-27): 超过14天仍pending的旧决策,
                # 注入时明确标注"待你复核" — 防止旧观点被当作已验证结论沿用。
                m_ts = re.search(r"— (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", compact)
                m_st = re.search(r"\*\*状态\*\*: *(\w+)", compact)
                age = _entry_age_days(m_ts.group(1) + " UTC") if m_ts else float("inf")
                status = (m_st.group(1).strip() if m_st else "pending") or "pending"
                if status == "pending" and age > 14:
                    stale_pending += 1
                    compact = (
                        compact
                        + f"\n- **⚠ 时效提示**: 本条决策记录于 {int(age)} 天前且尚未复核，"
                        "请先验证其关键假设是否仍然成立，再决定是否参考"
                    )
                relevant.append(compact)

        if not relevant:
            return ""

        # Return most recent first, limited to max_entries
        stale_note = (
            f"（其中 {stale_pending} 条超过两周未复核——优先验证时效性）\n\n"
            if stale_pending
            else "\n\n"
        )
        context = (
            "\n## 📋 历史决策记录（来自 decision-log.md）\n\n"
            "你之前分析过这只股票，以下是历史结论。请参考这些记录，"
            "验证之前的关键假设是否仍然成立，并据此给出更新后的分析。"
            + stale_note
        )
        for entry in relevant[-max_entries:]:
            context += entry + "\n"

        logger.info(f"Loaded {len(relevant[-max_entries:])} past decisions for '{stock}'")
        return context

    except Exception as e:
        logger.warning(f"Failed to load decisions: {e}")
        return ""


def _extract_stock_name(arguments: str) -> str:
    """Extract a stock/company name from user input.

    Whitespace (incl. newlines) is collapsed first and leading markdown
    heading/quote markers stripped, so a multi-line goal cannot inject a
    section header into the log through the returned name.
    2026-08-27 增强：剥离"分析一下/帮我看看/研究一下"等动词前缀、
    在括号(代码/补充说明)处截断——否则整句话被当作股票名存入日志,
    导致按名字检索历史决策时永远匹配不到。
    """
    arg = " ".join(arguments.split()).lstrip("#> ").strip()
    if not arg:
        return ""

    # 在括号处截断: "中际讯创(300308)" → "中际讯创"
    arg = re.split(r"[（(【\[]", arg)[0].strip()

    # 剥离常见动词前缀 (循环剥离直到不再变化)
    _PREFIXES = ["分析一下", "帮我看一下", "帮我看看", "看一下", "看看", "分析", "研究", "评估", "复盘"]
    changed = True
    while changed:
        changed = False
        for pre in _PREFIXES:
            if arg.startswith(pre) and len(arg) > len(pre) + 1:
                arg = arg[len(pre):].lstrip("，,。. 、的")
                changed = True

    # Try to extract first meaningful name segment
    # Common patterns: "腾讯 2025Q4", "拼多多", "茅台 分析", "0700.HK"
    # Remove common suffixes
    for suffix in ["的分析", "分析", "研究", "财报", "2025", "2026", "Q1", "Q2", "Q3", "Q4"]:
        idx = arg.find(suffix)
        if idx > 0:
            arg = arg[:idx].strip()

    # If it's a stock code (digits + exchange suffix), keep as-is
    if re.match(r"^\d{4,6}\.[A-Z]{2,3}$", arg):
        return arg

    # Otherwise take first 15 chars as company name
    return arg[:15].rstrip("，,。. ")


def _extract_decision(report: str, arguments: str) -> Optional[dict]:
    """Extract investment decision from a research report.

    Returns dict with conclusion/confidence/assumptions/target or None.
    """
    if not report or len(report) < 100:
        return None

    # Look for explicit decision markers
    patterns = [
        # Combined: **结论**：买入
        r"\*\*结论\*\*[：:]\s*(.{2,80})",
        r"\*\*投资建议\*\*[：:]\s*(.{2,80})",
        r"\*\*最终结论\*\*[：:]\s*(.{2,80})",
        # Chinese markers
        r"投资建议[：:]\s*(.{2,80})",
        r"操作建议[：:]\s*(.{2,80})",
    ]

    conclusion = ""
    for pat in patterns:
        m = re.search(pat, report)
        if m:
            conclusion = m.group(1).strip()
            break

    if not conclusion:
        return None

    # Extract confidence
    confidence = "中"
    conf_pat = r"\*\*置信度\*\*[：:]\s*(.{1,20})"
    m = re.search(conf_pat, report)
    if m:
        confidence = m.group(1).strip()

    # Extract key assumptions (first few bullet points after "关键假设")
    assumptions = "未指定"
    asm_section = re.search(
        r"(?:关键假设|核心假设|主要假设).*?\n((?:\s*[-*]\s*.+\n){1,5})", report, re.DOTALL
    )
    if asm_section:
        lines = re.findall(r"[-*]\s*(.+)", asm_section.group(1))
        if lines:
            assumptions = "; ".join(lines[:3])

    # Extract target price
    target = "未指定"
    target_pat = r"(?:\*\*)?(?:目标价|合理估值|合理价格)(?:\*\*)?[：:]\s*(.{1,30})"
    m = re.search(target_pat, report)
    if m:
        target = m.group(1).strip()

    return {
        "conclusion": conclusion[:120],
        "confidence": confidence[:20],
        "assumptions": assumptions[:200],
        "target": target[:50],
    }
