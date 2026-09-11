"""P3: Decision log — cross-session investment thesis tracking.

Append-only Markdown log of every investment decision made during research.
On next analysis of the same stock, past decisions are injected into context
so the LLM builds on prior work rather than starting from scratch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ai_berkshire.harness.decision_log")

DECISION_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "decision-log.md"

# 批B(2026-09-11): 结论抽取失败的留痕文件。旧版失败只打 logger.debug，
# 生产日志级别下等于静默丢数据——违反"数据管道不能静默失败"。
# 该 jsonl 记录每次跳过的原因，可直接统计漏失率。
DECISION_SKIP_PATH = DECISION_LOG_PATH.with_name("decision-skip-log.jsonl")

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
    stock_code: str = "",
    record_price: Optional[float] = None,
) -> bool:
    """Extract decision from a completed report and append to decision log.

    Looks for explicit decision markers in the report, then falls back to
    extracting the first strong opinion found. Appends one entry to
    decision-log.md.

    company_hint: trusted company name from the goal parser — preferred over
    heuristic extraction from arguments.  When no credible name (>= 2
    chars after cleaning) can be determined, the entry is skipped entirely.

    stock_code / record_price: P4-验证闭环(2026-09-01). 代码与决策时点价格,
    由后台任务对照真实行情验证结论对错。可缺省 — 验证任务会尝试回填,
    回填失败则跳过该条目(不阻塞保存)。
    """
    # Try to extract decision from report
    decision = _extract_decision(report, arguments)
    if not decision:
        # 批B: 旧版这里是 logger.debug —— 生产级别下看不见，等于静默丢结论。
        # 现在升级为告警 + jsonl 留痕（英维克那两篇就是这么丢的）。
        _log_skip("no_conclusion_marker", arguments, skill_name, task_id)
        return False

    try:
        log_path = _ensure_log()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        # 批B(2026-09-11): 标的解析顺序改为「清洗后的 hint → 启发式 → 代码」，
        # 且一律过可信度闸门。旧版无条件信任 company_hint，'你看一下还'、
        # '能不能买300308' 这类残句会被当标的写进日志头，同股匹配/去重随之失效。
        hint = _clean_company_hint(company_hint)
        code = _extract_stock_code(stock_code) or _extract_stock_code(arguments)
        stock = hint or _extract_stock_name(arguments) or code
        if not (_is_credible_name(stock) or re.match(r"^\d{4,6}(\.[A-Za-z]{2,3})?$", stock or "")):
            _log_skip("no_credible_stock_name", arguments, skill_name, task_id,
                      extra={"company_hint": company_hint, "code": code, "extracted": stock})
            return False

        entry = (
            f"\n## {stock} — {ts}\n\n"
            f"- **技能**: {_one_line(skill_name) or '未指定'}\n"
            f"- **代码**: {code or '未指定'}\n"
            f"- **记录价**: {f'{record_price:.2f}' if record_price else '未指定'}\n"
            f"- **结论**: {_one_line(decision['conclusion'])}\n"
            f"- **置信度**: {_one_line(decision['confidence'])}\n"
            f"- **关键假设**: {_one_line(decision['assumptions'])}\n"
            f"- **目标价/信号**: {_one_line(decision.get('target', '未指定'))}\n"
            f"- **状态**: pending\n"
            f"- **验证**: 待验证\n"
            f"- **任务ID**: {_one_line(task_id)}\n"
            f"\n---\n"
        )

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)

        # P3-闭环(2026-08-28): 新决策已入库, 同股旧 pending 自动销账
        # 批B(2026-09-11): 销账改为「代码 + 公司名」多 key 匹配 —— 旧版只比对标题，
        # 同一标的换一种写法（中际旭创 / 300308 / 300308.SZ）就销不掉，于是堆重复条目。
        # 批C(2026-09-11): key 再扩到「同规范代码分组内的全部历史别名写法」
        # （_resolve_match_keys），与注入层的分组口径保持一致。
        try:
            keys = _resolve_match_keys(log_path, stock, code)
            _resolve_superseded(log_path, keys, ts)
        except Exception as e:
            logger.warning(f"resolve_superseded skipped: {e}")

        logger.info(f"Decision saved: {stock} ({code or '无代码'}) "
                    f"via {decision.get('matched', 'unknown')} — {decision['conclusion'][:60]}")
        return True

    except Exception as e:
        logger.warning(f"Failed to save decision: {e}")
        return False


def _resolve_superseded(log_path: Path, keys: list[str], new_ts: str) -> int:
    """P3-闭环(2026-08-28): 同股票保存新决策时, 旧 pending 记录自动销账。

    新分析覆盖了旧观点: 无论结论是重申还是反转, 旧条目都已不是"最新待验证"
    状态 — 标记为 resolved（已被新分析覆盖）并注明覆盖来源, 避免旧 pending
    永远滞留、时效提示无限循环。

    批B(2026-09-11): `keys` 支持多写法（代码 + 公司名），匹配走 `_section_matches`。

    Returns number of entries resolved.
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return 0
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
         if sec.startswith("## ") and _section_matches(sec, keys)),
        default=-1,
    )
    for i, sec in enumerate(sections):
        if i == last_match:
            continue
        if not sec.startswith("## ") or not _section_matches(sec, keys):
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
            logger.info(f"resolve_superseded: {keys[0]} — {resolved} 条旧决策已销账")
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
    for section in re.split(r"\n(?=## )", content):
        parsed = _parse_section(section)
        if parsed:
            entries.append(parsed)

    entries.reverse()  # file is append-only; newest last → newest first
    return entries[:max_entries]


def update_entry_fields(task_id: str, fields: dict) -> bool:
    """Update markdown fields of one decision-log entry by task ID.

    Fields is {field_label: value} — label is the Chinese bold-label text
    (e.g. "代码", "记录价", "验证", "上次验证").  Existing field lines are
    replaced in place; missing field lines are inserted after the title.
    Returns True if the entry was found and updated.
    """
    if not task_id:
        return False
    log_path = _ensure_log()
    try:
        content = log_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"update_entry_fields: read failed: {e}")
        return False

    sections = re.split(r"(\n(?=## ))", content)
    found = False
    for i, sec in enumerate(sections):
        if not sec.startswith("## "):
            continue
        # Only touch matching entries — 同一任务可能产生多条同名决策,
        # 全部更新(不 break), 避免第二条漏更新。
        lines = sec.split("\n")
        matched = any(f"**任务ID**: {task_id}" in ln for ln in lines[:12])
        if not matched:
            continue
        found = True
        for label, value in fields.items():
            new_line = f"- **{label}**: {value}"
            replaced = False
            for j, ln in enumerate(lines):
                m = re.match(rf"^- \*\*{re.escape(label)}\*\*[：:]", ln.strip())
                if m:
                    lines[j] = new_line
                    replaced = True
                    break
            if not replaced:
                # Insert after the title line (index 0)
                lines.insert(1, new_line)
        sections[i] = "\n".join(lines)

    if found:
        try:
            log_path.write_text("".join(sections), encoding="utf-8")
            logger.info(f"update_entry_fields: task {task_id} → {fields}")
        except Exception as e:
            logger.warning(f"update_entry_fields: write failed: {e}")
            return False
    return found


def load_decisions(arguments: str, max_entries: int = 5) -> str:
    """Load past decisions relevant to the current analysis.

    批C(2026-09-11) 重写注入层，三件事一起做（原实现的两个静默缺陷见下）：

    1. 同标的历史**按规范代码聚合成一组** —— 打名字或打代码命中同一组。
       旧版按单一子串匹配：打"中际旭创"只看到 8 月的 4 条，9 月那两条按代码
       写的（300308.SZ / 300308）永远看不见 —— 不报错、不提示的静默漏历史。
    2. **完全重复的结论合并成一条并标注** —— 旧版同一天跑两次、结论一字不差
       的两条各占一个注入名额，越跑能看见的历史越少。
    3. **立场冲突显式标记** —— 同组内并列相反立场时，标出哪条是最后一版、
       差异在哪，并在报告头写明上次立场（改口必须说明原因）。

    返回可直接注入 prompt 的文本；无相关历史返回空串。
    """
    try:
        if not DECISION_LOG_PATH.exists():
            return ""

        content = DECISION_LOG_PATH.read_text(encoding="utf-8")
        # 文件 append-only → 段顺序 = 由旧到新
        sections = [s for s in re.split(r"\n(?=## )", content)
                    if s.strip().startswith("## ")]
        index = _alias_index(sections)
        group_code, keys = _stock_group_keys(arguments, index)
        if not keys:
            return ""  # can't extract a meaningful stock name or code
        stock = _extract_stock_name(arguments) or group_code

        # ── 1. 组内条目（名称/代码任一写法命中即入组，文件顺序=时间顺序） ──
        group: list[dict] = []
        for sec in sections:
            if not _section_matches(sec, keys):
                continue
            parsed = _parse_section(sec)
            if not parsed:
                continue
            parsed["stance"] = _classify_stance(parsed)
            group.append(parsed)
        if not group:
            return ""

        # ── 2. 完全重复合并（结论归一后 + 记录价都相同才算重复） ──────────
        effective: list[dict] = []
        by_dupkey: dict[tuple, dict] = {}
        dup_merged = 0
        for e in group:
            dk = _dup_key(e)
            if dk and dk in by_dupkey:
                by_dupkey[dk].setdefault("dups", []).append(e)
                dup_merged += 1
                continue  # 保留后写的那条，重复条目只留痕不占名额
            e["dups"] = []
            if dk:
                by_dupkey[dk] = e
            effective.append(e)

        # ── 3. 立场冲突（同组出现 ≥2 种明确立场） ──────────────────────
        labels = {e["stance"] for e in effective if e["stance"] != "unknown"}
        last_e = effective[-1]
        conflict = None
        if len(labels) >= 2:
            prev_e = next((e for e in reversed(effective[:-1])
                           if e["stance"] != last_e["stance"]), None)
            if prev_e is not None:
                conflict = {
                    "last": last_e,
                    "prev": prev_e,
                    "opposite": bool({"bear", "bull"} <= labels),
                }

        # ── 报告头 ─────────────────────────────────────────────────
        head = [
            "## 📋 历史决策复盘（来自 decision-log.md）",
            "",
            f"标的: {stock}"
            + (f"（规范代码 {group_code}）" if group_code else "")
            + f"｜同组历史 {len(group)} 条 → 有效 {len(effective)} 条"
            + (f"（合并 {dup_merged} 条完全重复，腾出 {dup_merged} 个注入名额）"
               if dup_merged else ""),
            f"上次立场: {_STANCE_LABEL[last_e['stance']]}"
            f"（{last_e['time'] or '时间未知'} 那一版）",
        ]
        if last_e["stance"] != "unknown":
            head.append("本次若与上次立场不同，必须在结论里写明改口依据 —— 不要沉默改口。")
        else:
            head.append("历史各版未给出明确买卖口径（多为描述性结论），本次请给出明确立场。")

        if conflict:
            L, P = conflict["last"], conflict["prev"]
            head += [
                "",
                f"⚠ 立场冲突: 该标的同组历史存在 {len(labels)} 种立场并列，请先解释口径变化再往下做 ——",
                f"  · 最后一版 {L['time']}（{L.get('code') or '代码未指定'}，"
                f"{L.get('status') or 'pending'}）: {_STANCE_LABEL[L['stance']]}",
                f"  · 上一版   {P['time']}（{P.get('code') or '代码未指定'}，"
                f"{P.get('status') or 'pending'}）: {_STANCE_LABEL[P['stance']]}",
                f"  · 差异一句话: 上一版主张「{_brief(P.get('conclusion'))}」，"
                f"最后一版主张「{_brief(L.get('conclusion'))}」 —— 口径由"
                f"「{_STANCE_LABEL[P['stance']]}」变为「{_STANCE_LABEL[L['stance']]}」"
                + ("（方向相反）" if conflict["opposite"] else "（口径位移）"),
                "  · 以最后一版为准；若本次要沿用上一版口径，必须说明为什么。",
            ]

        # ── 时间线（由旧到新） ─────────────────────────────────────
        shown = effective[-max_entries:] if max_entries and max_entries > 0 else effective
        lines = ["", f"### 时间线（由旧到新；本次注入 {len(shown)}/{len(effective)} 条）"]
        for i, e in enumerate(shown, 1):
            row = f"{i}. {e['time'] or '时间未知'}｜{_STANCE_LABEL[e['stance']]}"
            if e.get("code") and e["code"] != "未指定":
                row += f"｜{e['code']}"
            if e is last_e:
                row += "｜★ 最后一版"
            if e.get("status") and e["status"] != "pending":
                row += f"｜{e['status']}"
            if e.get("legacy"):
                note = _one_line(e.get("legacy_note") or "")
                if "非标的" in note:
                    row += "｜⚠ 标的存疑（历史组合/主题条目，代码字段只代表其一）"
                else:
                    row += "｜（历史条目：原标题不可信，标的由代码/任务ID恢复）"
            lines.append(row)
            lines.append(f"   上次结论: {_one_line(e.get('conclusion') or '')[:120]}")
            rp = e.get("record_price")
            lp = e.get("latest_price")
            if rp and rp != "未指定":
                lines.append(f"   记录价: {rp}")
            if lp and lp != "未指定":
                try:
                    ret = (float(lp) - float(rp)) / float(rp) * 100
                    lines.append(f"   最新价: {lp}（{ret:+.1f}%）")
                except (TypeError, ValueError):
                    lines.append(f"   最新价: {lp}")
            lines.append(f"   验证: {e.get('verify') or '待验证'}")
            if e.get("review"):
                lines.append(f"   复核: {_one_line(e['review'])[:80]}")
            dups = e.get("dups") or []
            if dups:
                when = "、".join(sorted({d["time"] for d in dups if d.get("time")}))
                lines.append(f"   （含 {len(dups)} 条完全重复，已合并；重复条目时间: "
                             f"{when or '未知'}）")
            # P3-闭环(2026-08-27): 超14天仍 pending → 明确标注"待复核"
            age = _entry_age_days(e["time"]) if e.get("time") else float("inf")
            if (e.get("status") or "pending") == "pending" and age > 14:
                lines.append(f"   ⚠ 时效提示: 本条记录于 {int(age)} 天前且尚未复核，"
                             "请先验证其关键假设是否仍然成立，再决定是否参考")
        lines += [
            "",
            "请基于以上复盘（含冲突与重复标注）更新你的分析，特别关注验证状态为"
            "已证伪/已过期的历史结论——它们意味着上次判断被市场否定或失效。",
        ]

        logger.info(f"Loaded {len(shown)}/{len(group)} past decisions for "
                    f"'{stock}' (code={group_code or 'n/a'}, merged_dups={dup_merged}, "
                    f"conflict={'yes' if conflict else 'no'})")
        return "\n".join(head + lines) + "\n"

    except Exception as e:
        logger.warning(f"Failed to load decisions: {e}")
        return ""


# ── 批C(2026-09-11) 同标的聚合 / 重复合并 / 立场冲突标记 ──────────
# 立场关键词表。刻意保守：只有出现明确买卖口径的词才判定立场；
# 描述性结论（"这是一门好生意"）一律 未判定 —— 给描述性结论乱贴"看多"
# 比不贴更有害，会污染报告头并误导后续判断。
_STANCE_BEAR_KEYS = ("不建议", "不宜", "不推荐", "不参与", "别买", "不会买", "回避",
                     "卖出", "减仓", "止损", "看空", "已证伪", "证伪", "高估", "风险大于")
_STANCE_BULL_KEYS = ("建议买入", "建议增持", "可以买入", "买入评级", "增持", "加仓",
                     "看多", "值得买入", "显著低估", "低估")
_STANCE_NEUTRAL_KEYS = ("持有", "观望", "等待", "HOLD", "中性", "合理区间", "合理估值",
                        "不便宜", "不贵", "安全边际不厚")
_STANCE_LABEL = {"bear": "谨慎/看空", "bull": "看多",
                 "neutral": "持有/中性", "unknown": "未判定"}

# 标题是规范代码（不是公司名）
_CODE_HEAD_RE = re.compile(r"^\d{4,6}(\.[A-Za-z]{2,3})?$")


def _parse_section(section: str) -> Optional[dict]:
    """把一条 markdown 决策条目解析成结构化 dict。

    list_decisions（前端/API）与注入层共用这一份解析，避免出现两套口径。
    返回 None 表示这段不是决策条目（文件头、空段）。
    """
    section = (section or "").strip()
    if not section.startswith("## "):
        return None
    lines = section.split("\n")
    title = lines[0][3:].strip()
    stock, _, ts = title.partition(" — ")

    def _field(name: str) -> str:
        for ln in lines[1:]:
            # 批B: 字段名必须转义 —— "原标题(历史)" 这类带括号的标签
            # 不转义会被当成正则分组，永远解析不到。
            m = re.match(rf"- \*\*{re.escape(name)}\*\*[：:]\s*(.*)", ln.strip())
            if m:
                return m.group(1).strip()
        return ""

    return {
        "stock": stock.strip(),
        "time": ts.strip(),
        "skill": _field("技能"),
        "code": _field("代码"),
        "record_price": _field("记录价"),
        "latest_price": _field("最新价"),
        "conclusion": _field("结论"),
        "confidence": _field("置信度"),
        "assumptions": _field("关键假设"),
        "target": _field("目标价/信号"),
        "status": _field("状态") or "pending",
        "verify": _field("验证") or "待验证",
        "task_id": _field("任务ID"),
        "review": _field("复核"),
        # 批B 补充：历史脏条目被标记后，前端可以显式提示"标的不可信/非单一标的"，
        # 而不是把它当成一条正常结论展示（标题已不再参与同股匹配）。
        "legacy": bool(_field("标的识别")),
        "legacy_note": _field("标的识别"),
        "legacy_title": _field("原标题(历史)"),
    }


def _entry_age_days(ts: str) -> float:
    """Parse '2026-08-06 16:32 UTC' → age in days. Unparseable → inf."""
    try:
        dt = datetime.strptime(_one_line(ts), "%Y-%m-%d %H:%M UTC").replace(
            tzinfo=timezone.utc
        )
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return float("inf")


def _brief(text: Optional[str], limit: int = 60) -> str:
    """结论摘要（单行 + 截断），用于冲突块的"差异一句话"原文对照。"""
    s = _one_line(text or "").replace("**", "")
    return s[:limit] + ("…" if len(s) > limit else "")


def _dup_key(e: dict) -> tuple:
    """重复判定键：结论归一文本 + 记录价。空键（无结论）不参与合并。"""
    norm = _norm_conclusion(e)
    if not norm:
        return ()
    return (norm, _one_line(e.get("record_price") or ""))


def _norm_conclusion(e: dict) -> str:
    """结论归一：去 markdown 强调符 / 空白 / 标点 —— 只用于判定"完全重复"。"""
    s = _one_line(e.get("conclusion") or "")
    s = re.sub(r"[*`_#>|]", "", s)
    s = re.sub(r"[\s，。；、：:！？!?.,;\"'“”‘’（）()【】\[\]—\-~]", "", s)
    return s


def _classify_stance(e: dict) -> str:
    """从结论/目标价文本判立场: bear / bull / neutral / unknown。

    判定顺序 bear → bull → neutral：否定口径必须先判，
    "不建议买入" 里的"买入"不能让整条变成 bull。
    """
    raw = _one_line((e.get("conclusion") or "") + " " + (e.get("target") or ""))
    if not raw:
        return "unknown"
    up = raw.upper()
    for kw in _STANCE_BEAR_KEYS:
        if kw in raw:
            return "bear"
    for kw in _STANCE_BULL_KEYS:
        if kw in raw:
            return "bull"
    for kw in _STANCE_NEUTRAL_KEYS:
        if kw.upper() in up:
            return "neutral"
    return "unknown"


def _code_variants(code: str) -> list[str]:
    """一个代码的写法集合：300308.SZ ↔ 300308。"""
    if not code:
        return []
    out = [code]
    if "." in code:
        out.append(code.split(".")[0])
    return out


def _alias_index(sections: list[str]) -> dict:
    """从日志自身学「公司名 ↔ 规范代码」索引。

    只用日志内部可核对的事实配对：标题里的可信公司名 + `**代码**` 字段。
    刻意不引外部行情表 —— 外部表把"中芯国际"映射到港股 0981.HK，与 A 股
    688981.SH 是两回事，错并组比不并组更糟。
    """
    name2code: dict = {}
    code2names: dict = {}
    for sec in sections:
        parsed = _parse_section(sec)
        if not parsed:
            continue
        head = parsed["stock"]
        credible = bool(head) and (_is_credible_name(head) or _CODE_HEAD_RE.match(head))
        if not credible:
            continue  # 脏标题不参与配对（批B 闸门）
        code = _extract_stock_code(parsed.get("code") or "")
        if not code:
            continue
        code2names.setdefault(code, set())
        if not _CODE_HEAD_RE.match(head):
            name2code.setdefault(head, code)
            code2names[code].add(head)
    return {"name2code": name2code, "code2names": code2names}


def _stock_group_keys(arguments: str, index: dict) -> tuple:
    """查询 → (规范代码, 匹配 key 列表)。

    同一标的的所有写法（规范代码 / 去后缀代码 / 历史上出现过的公司名）
    全部进 key —— 这是"打名字和打代码命中同一组"的实现。
    """
    name = _extract_stock_name(arguments)
    code = _extract_stock_code(arguments)
    if not code and name:
        code = (index.get("name2code") or {}).get(name, "")
    keys: list[str] = []
    if code:
        keys.extend(_code_variants(code))
        keys.extend(sorted(index.get("code2names", {}).get(code) or ()))
    if name:
        keys.append(name)
    if not keys:
        keys.extend(_stock_match_keys(arguments))
    return code, [k for k in dict.fromkeys(k for k in keys if k)]


def _resolve_match_keys(log_path: Path, stock: str, code: str) -> list[str]:
    """销账用的匹配 key：本条新决策的写法 + 同代码分组里的历史别名写法。

    批C(2026-09-11): 旧版只给 [名称, 代码, 去后缀代码]，于是"英维克"这条新
    决策销不掉历史上用 002837.SZ 写的旧条目 —— 注入分组与销账口径必须一致。
    """
    keys = [stock] + _code_variants(code)
    try:
        text = log_path.read_text(encoding="utf-8")
        sections = [s for s in re.split(r"\n(?=## )", text) if s.strip().startswith("## ")]
        index = _alias_index(sections)
        canon = _extract_stock_code(code) or (index.get("name2code") or {}).get(stock, "")
        if canon:
            keys.extend(sorted(index.get("code2names", {}).get(canon) or ()))
    except Exception as e:
        logger.debug(f"_resolve_match_keys: alias index skipped: {e}")
    return [k for k in dict.fromkeys(k for k in keys if k)]


# ── 批B(2026-09-11) 标的归一 / 名称可信度 / 失败留痕 ──────────────
# 交易所后缀映射：只用于把用户输入的 6 位代码补成规范写法。
_A_SHARE_SUFFIX = {"6": "SH", "0": "SZ", "3": "SZ", "8": "BJ", "4": "BJ"}

# 出现即说明"这不是公司名，是一句话/一个问题"的词。
_NAME_STOPWORDS = (
    "你", "我", "他", "她", "的", "吗", "呢", "吧", "了", "怎么", "怎样", "什么", "多少",
    "能不能", "可以", "是否", "值得", "看一下", "看看", "分析", "研究", "持有", "买", "卖",
    "如何", "现在", "未来", "半年", "一年", "一直", "最近", "这", "那", "请", "帮",
)


def _extract_stock_code(text: str) -> str:
    """从文本里抽股票代码并规范化：`300308` → `300308.SZ`，`0700.HK` 原样保留。

    返回 '' 表示没有明确代码。判定只认 4-6 位纯数字（A股）或 `\\d{4,6}.XX` 形式。
    """
    t = " ".join(str(text or "").split()).upper()
    if not t:
        return ""
    m = re.search(r"(\d{6})\.(SH|SZ|BJ)", t)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.search(r"(\d{4,5})\.HK", t)
    if m:
        return m.group(0)
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", t)
    if m:
        digits = m.group(1)
        suffix = _A_SHARE_SUFFIX.get(digits[0], "")
        return f"{digits}.{suffix}" if suffix else digits
    return ""


def _is_credible_name(name: str) -> bool:
    """公司名可信度闸门：拦住"你看一下还""能不能买300308"这类句子残片。

    刻意保守：宁可拒掉可疑的，也不要让脏标的进决策池（脏 key 会让
    同股历史匹配和后续去重全部失效）。
    """
    s = _one_line(name)
    if len(s) < 2 or len(s) > 12:
        return False
    # 规范代码形态优先放行 —— 否则 "300308.SZ" 会被下面的标点规则按 '.' 误杀
    if re.match(r"^\d{4,6}(\.[A-Za-z]{2,3})?$", s):
        return True
    if re.search(r"[，。；！？,.;!?、\s「」【】（）()]", s):
        return False
    # 带数字的只接受规范代码形态，避免"工商银行17.5%"这类混排
    if re.search(r"[0-9]", s) and not re.match(r"^\d{4,6}(\.[A-Za-z]{2,3})?$", s):
        return False
    for w in _NAME_STOPWORDS:
        if w in s:
            return False
    return True


def _clean_company_hint(hint: str) -> str:
    """清洗 goal parser 给的 company_hint —— 旧版直接信任它，导致脏标的入库。"""
    if not hint:
        return ""
    cleaned = _extract_stock_name(hint)
    return cleaned if _is_credible_name(cleaned) else ""


def _stock_match_keys(arguments: str) -> list[str]:
    """同一标的的多种写法：代码优先，附带公司名，用于历史匹配与销账。"""
    keys: list[str] = []
    code = _extract_stock_code(arguments)
    if code:
        keys.append(code)
        keys.append(code.split(".")[0])       # 300308.SZ → 也认 300308
    name = _extract_stock_name(arguments)
    if name and _is_credible_name(name):
        keys.append(name)
    return keys


def _section_matches(section: str, keys: list[str]) -> bool:
    """决策条目是否属于该标的：标题按名称/代码匹配，正文只认「**代码**」字段。

    批B 补充(2026-09-11)：标题必须**可信**才参与名称匹配 —— 历史条目里存在
    '你看一下还'、'能不能买300308' 这类句子残片标题，它们的子串会命中
    合法 key（如 '300308'），导致无关条目被错误注入/销账。
    代码 key 走「**代码**」字段精确匹配，正文里提到别的公司不会误伤。
    """
    head = section.split("\n")[0]
    head_title = head[3:].split(" — ")[0].strip() if head.startswith("## ") else ""
    head_low = head_title.lower()
    credible_head = _is_credible_name(head_title) or bool(
        re.match(r"^\d{4,6}(\.[A-Za-z]{2,3})?$", head_title)
    )
    for k in keys:
        if not k:
            continue
        kl = k.lower()
        is_code_key = bool(re.match(r"^\d{4,6}(\.|$)", kl))
        if is_code_key:
            if re.search(rf"\*\*代码\*\*: *{re.escape(k)}\b", section, re.I):
                return True
            if credible_head and kl in head_low:
                return True
        else:
            if credible_head and kl in head_low:
                return True
    return False


def _log_skip(reason: str, arguments: str, skill_name: str = "", task_id: str = "",
              extra: Optional[dict] = None) -> None:
    """结论未能入库时留痕：logger.warning + 追加 data/decision-skip-log.jsonl。"""
    logger.warning(f"Decision NOT saved ({reason}) for '{_one_line(arguments)[:60]}' "
                   f"skill={skill_name} task={task_id}")
    try:
        DECISION_SKIP_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "reason": reason,
            "arguments": _one_line(arguments)[:200],
            "skill": _one_line(skill_name),
            "task_id": _one_line(task_id),
        }
        if extra:
            rec.update({k: _one_line(v)[:200] for k, v in extra.items()})
        with open(DECISION_SKIP_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # 留痕失败不能反过来阻断主流程
        logger.warning(f"decision skip-log write failed: {e}")


def _strip_conclusion(raw: str) -> str:
    """结论文本清理：去掉尾部加粗符号/句号与多余空白。"""
    s = _one_line(raw)
    s = s.strip("*· \t")
    s = s.rstrip("。;；")
    return s.strip("*· \t")


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

    # 批B: 代码形态优先返回 —— 必须早于下面的标点截断，
    # 否则 "0700.HK" 会被按 '.' 切成 "0700"（历史回归用例）。
    first = arg.split()[0].rstrip("，,。.;；")
    if re.match(r"^\d{4,6}(\.[A-Za-z]{2,3})?$", first):
        return first

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

    # 批B(2026-09-11): 先按标点截断 —— 公司名里几乎不会出现标点，
    # "工商银行17.5%，长江电力1" 这类多标的/残句在此被切断。
    arg = re.split(r"[，。；！？,.;!?、：:〜~…]", arg)[0].strip()

    # 剥离尾部疑问/分析短语,取最早出现的后缀截断。
    # 覆盖 "的投资价值/基本面/现在还能买吗/怎么样" 等带"的"与不带"的"形态,
    # 避免 "贵州茅台的投资价值"、"拼多多的基本面" 整句被当作股票名。
    _SUFFIXES = [
        "的投资价值", "投资价值", "的基本面", "基本面", "现在还能买吗", "还能买吗",
        "值得买吗", "可以买吗", "能不能买", "值得入手吗", "你觉得怎么样", "怎么样", "怎么看",
        "的走势", "走势", "的前景", "前景", "的估值", "估值", "的股价", "股价",
        "的目标价", "目标价", "如何", "看下",
        "的分析", "分析", "研究", "财报", "2026", "2025", "Q1", "Q2", "Q3", "Q4",
        # 批B 追加：口语化提问尾巴
        # （"英维克你看一下在半年之内能不能一直持有" → "英维克"）
        "你看一下", "你看", "看一下", "能不能", "能一直持有", "一直持有", "能持有",
        "半年之内", "半年内", "之内", "该不该", "要不要", "是不是", "适不适合", "持有",
    ]
    cut = len(arg)
    for suffix in _SUFFIXES:
        idx = arg.find(suffix)
        if 0 < idx < cut:
            cut = idx
    if cut < len(arg):
        arg = arg[:cut].strip()

    # 纯数字股票代码: "601318你觉得怎么样" → "601318"(数字开头且后接中文问句/动词)
    m = re.match(r"^(\d{4,6})", arg)
    if m and len(arg) > len(m.group(1)) and re.search(r"[买卖看看评议走势估]", arg[len(m.group(1)):]):
        return m.group(1)

    # If it's a stock code (digits + exchange suffix), keep as-is
    if re.match(r"^\d{4,6}\.[A-Z]{2,3}$", arg):
        return arg

    # 空格截断: "中际旭创 300308" → "中际旭创"; "Tencent 2025Q4" → "Tencent"
    arg = re.split(r"\s+", arg)[0].strip()

    # Otherwise take first 15 chars as company name
    return arg[:15].rstrip("，,。. ")


def _extract_decision(report: str, arguments: str) -> Optional[dict]:
    """Extract investment decision from a research report.

    Returns dict with conclusion/confidence/assumptions/target or None.
    """
    if not report or len(report) < 100:
        return None

    # 结论标记模式（批B 2026-09-11 扩展）。真实报告里的写法比旧版 5 条多得多：
    # 旧版只认 「**结论**：/ **投资建议**：/ **最终结论**：/ 投资建议：/ 操作建议：」，
    # 而投资团队报告实际写的是「**综合结论：…**」「**综合评级：⚡持有（附条件）**」
    # —— 一个都不匹配，于是结论静默不入库（英维克两篇即如此）。
    # 冒号前的 `**` 用 `(?:\*\*)?` 兼容两种加粗写法：闭合在冒号前（**结论**：）
    # 与"加粗在句首、闭合成对在句尾"（**综合结论：…**）。
    patterns = [
        ("综合结论", r"\*\*综合结论(?:\*\*)?\s*[：:]\s*([^\n]{2,140})"),
        ("综合结论", r"综合结论[：:]\s*([^\n]{2,140})"),
        ("综合评级", r"\*\*综合评级(?:\*\*)?\s*[：:]\s*([^\n]{2,140})"),
        ("综合评级", r"综合评级[：:]\s*([^\n]{2,140})"),
        ("最终评级", r"\*\*最终评级(?:\*\*)?\s*[：:]\s*([^\n]{2,140})"),
        ("投资评级", r"\*\*投资评级(?:\*\*)?\s*[：:]\s*([^\n]{2,140})"),
        ("最终结论", r"\*\*最终结论(?:\*\*)?\s*[：:]\s*([^\n]{2,140})"),
        ("结论", r"\*\*结论(?:\*\*)?\s*[：:]\s*([^\n]{2,140})"),
        ("投资建议", r"\*\*投资建议(?:\*\*)?\s*[：:]\s*([^\n]{2,140})"),
        ("投资建议", r"投资建议[：:]\s*([^\n]{2,140})"),
        ("操作建议", r"操作建议[：:]\s*([^\n]{2,140})"),
    ]

    conclusion = ""
    matched = ""
    for label, pat in patterns:
        m = re.search(pat, report)
        if m:
            conclusion = _strip_conclusion(m.group(1))
            matched = label
            break

    if not conclusion:
        return None

    # 置信度：兼容 "**置信度**：高" 与 "**置信度：高**"
    confidence = "中"
    conf_pat = r"\*\*置信度(?:\*\*)?\s*[：:]\s*([^\n]{1,20})"
    m = re.search(conf_pat, report)
    if m:
        confidence = _strip_conclusion(m.group(1))

    # Extract key assumptions (first few bullet points after "关键假设")
    assumptions = "未指定"
    asm_section = re.search(
        r"(?:关键假设|核心假设|主要假设).*?\n((?:\s*[-*]\s*.+\n){1,5})", report, re.DOTALL
    )
    if asm_section:
        lines = re.findall(r"[-*]\s*(.+)", asm_section.group(1))
        if lines:
            assumptions = "; ".join(lines[:3])

    # Extract target price（同样容忍 "**目标价：…**" 的句尾闭合写法）
    target = "未指定"
    target_pat = r"(?:\*\*)?(?:目标价|合理估值|合理价格)(?:\*\*)?[：:]\s*([^\n*]{1,40})"
    m = re.search(target_pat, report)
    if m:
        target = _strip_conclusion(m.group(1))

    return {
        "conclusion": conclusion[:140],
        "confidence": confidence[:20],
        "assumptions": assumptions[:200],
        "target": target[:60],
        "matched": matched,
    }
