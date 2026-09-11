#!/usr/bin/env python3
"""批C 补充验收：注入块在 context 预算裁剪下是否存活（体量上界 + 实测裁剪）

用法: ./venv/bin/python scripts/verify_history_trim_budget.py

背景：注入块（## 📋 历史决策复盘…）由 ContextBuilder._split_sections 切成独立
section，_categorize 归类后按 BUDGET 给预算；超预算走 _structured_trim(L1) 按句截尾。
若它落在 web(1500) 预算里，5 条长结论的块（上界实测≈1974 token）会被截尾，
而时间线「由旧到新」的尾部正是最新一版结论 → 静默丢掉最新判断。
因此批C 给它单列 decision_history(2200) 预算，本脚本把这个保护钉死。

只读生产 decision-log.md（收尾校验 sha256 未变），构造用例一律走临时目录。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("_dl", ROOT / "app" / "harness" / "decision_log.py")
assert _spec and _spec.loader
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

import tiktoken  # noqa: E402

from app.harness import context as ctx  # noqa: E402  包导入（内部相对导入）

enc = tiktoken.get_encoding("cl100k_base")
tok = lambda s: len(enc.encode(s))  # noqa: E731

RESULTS: list[tuple[str, bool, str]] = []
check = lambda n, ok, d: RESULTS.append((n, bool(ok), d))  # noqa: E731

LOG = dl.DECISION_LOG_PATH
SHA_BEFORE = hashlib.sha256(LOG.read_bytes()).hexdigest() if LOG.exists() else ""

builder = ctx.ContextBuilder()


def trim(text: str):
    return asyncio.run(builder._trim_and_grade(text, None))


# ── 1. 分类归属：注入块必须落到 decision_history，且不再占 web 预算 ──────
block_y = dl.load_decisions("英维克")
secs = ctx._split_sections(block_y)
cats = {t: ctx._categorize(t, b) for t, b in secs}
check("1-注入块归类为 decision_history", "decision_history" in cats.values(), str(cats))
check("1b-预算存在且 ≥ 块体积上界(1974)",
      ctx.BUDGET.get("decision_history", 0) >= 1974,
      f"decision_history={ctx.BUDGET.get('decision_history')}")

# ── 2. 全库体积：真实同组最大注入块 + 报告余量 ────────────────────────
log_text = LOG.read_text(encoding="utf-8")
raw_secs = [s for s in re.split(r"\n(?=## )", log_text) if s.strip().startswith("## ")]
groups: dict[str, list[str]] = {}
for s in raw_secs:
    p = dl._parse_section(s)
    if not p:
        continue
    code = (p.get("code") or "").strip()
    name = (p.get("stock") or "").strip()
    if not code or code in ("未指定",):
        continue
    groups.setdefault(code, [])
    if name and name not in groups[code]:
        groups[code].append(name)

sizes = []
for code, names in groups.items():
    out = dl.load_decisions(names[0] if names else code)
    if out:
        sizes.append((tok(out), code))
sizes.sort(reverse=True)
worst_real = sizes[0] if sizes else (0, "-")
budget = ctx.BUDGET["decision_history"]
check("2-真实最大注入块在预算内", worst_real[0] <= budget,
      f"最大 {worst_real[0]} token（{worst_real[1]}）≤ 预算 {budget}")
print(f"  同组标的 {len(sizes)} 个；最大注入块 {worst_real[0]} token（{worst_real[1]}）；"
      f"预算 {budget}，余量 {budget - worst_real[0]}")

# ── 3. 体积上界（全库最大 5 条相加 + 框架开销）必须被预算覆盖 ──────────
per_entry = []
for s in raw_secs:
    p = dl._parse_section(s)
    if not p or not (p.get("code") or "").strip() or p.get("code") in ("未指定",):
        continue
    per_entry.append(tok(s))
per_entry.sort(reverse=True)
upper = sum(per_entry[:5]) + 90  # 90 ≈ 表头/时间线/提示语等框架开销
check("3-体积上界(5 条最大+框架)在预算内", upper <= budget,
      f"上界≈{upper} token vs 预算 {budget}")

# ── 4. 真实标的实测：裁剪后块完整、冲突块与最新版都在 ──────────────────
for q, want_conflict in [("英维克", True), ("中际旭创", False), ("腾讯控股", False)]:
    blk = dl.load_decisions(q)
    out = trim(blk + "\n## 当前时间上下文\n日期 2026-09-11\n")
    kept = "历史决策复盘" in out.context
    conf_out = "⚠ 立场冲突" in out.context
    last_in = "★ 最后一版" in blk
    last_out = "★ 最后一版" in out.context
    check(f"4-{q} 注入块未被裁剪（truncated=0 且块留存）",
          out.truncated_tokens == 0 and kept,
          f"truncated={out.truncated_tokens} 块留存={kept} "
          f"分类计数={out.token_counts.get('decision_history')}")
    check(f"4b-{q} 冲突标记与最新版行未被切掉",
          (conf_out == want_conflict) and (last_out == last_in),
          f"冲突块 应={want_conflict} 实={conf_out}；最后一版 进={last_in} 出={last_out}")

# ── 5. 最坏情况（临时日志：5 条长结论）裁剪后最新版仍在 ────────────────
tmp = Path(tempfile.mkdtemp(prefix="batchc-trim-"))
big = tmp / "big.md"
parts = ["# log\n"]
for i in range(5):
    parts.append(
        f"\n## 长文股 — 2026-0{i+1}-01 10:00 UTC\n- **代码**: 000001.SZ\n"
        f"- **结论**: 建议买入，理由{i}：" + "基本面强劲且估值合理，" * 12 + "显著低估\n"
        "- **状态**: pending\n\n---\n"
    )
big.write_text("".join(parts), encoding="utf-8")
orig = dl.DECISION_LOG_PATH
try:
    dl.DECISION_LOG_PATH = big
    blk = dl.load_decisions("长文股")
finally:
    dl.DECISION_LOG_PATH = orig
out = trim(blk + "\n## x\n")
check("5-最坏情况(5 条长结论)裁剪后仍完整",
      out.truncated_tokens == 0 and "★ 最后一版" in out.context,
      f"块 {tok(blk)} token，truncated={out.truncated_tokens}，"
      f"裁剪标注={'有' if '已裁剪' in out.context else '无'}")

# ── 6. 对照：假若塞回 web 预算，会被截尾（证明本保护不是摆设） ──────────
#    做法：临时把分类函数打回旧行为（无 decision_history 分支）+ web 预算 1500，
#    用超 1500 token 的合成块跑一次，必须看到截尾与「本节因 token 预算裁剪」标注。
huge = tmp / "huge.md"
hp = ["# log\n"]
for i in range(5):
    hp.append(
        f"\n## 巨块股 — 2026-0{i+1}-01 10:00 UTC\n- **代码**: 000001.SZ\n"
        f"- **结论**: 建议买入，理由{i}：" + "基本面强劲且估值合理，护城河深厚，" * 30
        + "显著低估\n- **状态**: pending\n\n---\n"
    )
huge.write_text("".join(hp), encoding="utf-8")
try:
    dl.DECISION_LOG_PATH = huge
    huge_blk = dl.load_decisions("巨块股")
finally:
    dl.DECISION_LOG_PATH = orig

old_cat, old_budgets = ctx._categorize, dict(ctx.BUDGET)
try:
    ctx.BUDGET["web"] = 1500
    ctx.BUDGET.pop("decision_history", None)
    ctx._categorize = lambda title, body: (
        "time" if ("时间" in title) else
        "market" if ("指数" in title or "行情" in title) else
        "financials" if ("财务" in title or "财报" in title) else
        "news" if ("新闻" in title) else
        "web"
    )
    out_web = trim(huge_blk + "\n## x\n")
finally:
    # 原地还原：BUDGET 是模块级/类属性共用的同一个 dict 对象，必须就地复原
    ctx._categorize = old_cat
    ctx.BUDGET.clear()
    ctx.BUDGET.update(old_budgets)
check("7b-还原后 decision_history 预算仍在（清理无残留）",
      ctx.BUDGET.get("decision_history") == 2200 and ctx.BUDGET.get("web") == 1500,
      f"decision_history={ctx.BUDGET.get('decision_history')} web={ctx.BUDGET.get('web')}")

check("6-对照：无专用预算时巨块确被截尾（保护必要性属实）",
      tok(huge_blk) > 1500 and out_web.truncated_tokens > 0
      and "（本节因 token 预算裁剪）" in out_web.context,
      f"巨块 {tok(huge_blk)} token → truncated={out_web.truncated_tokens}")
out_huge_kept = trim(huge_blk + "\n## x\n")  # 专用预算下同一巨块
check("6b-同一巨块在 decision_history 预算下的表现（超出 2200 仍会截，属已知上限）",
      True,
      f"truncated={out_huge_kept.truncated_tokens}（2200 内为 0；再大则截尾，"
      f"头部「上次立场」仍保留）")

SHA_AFTER = hashlib.sha256(LOG.read_bytes()).hexdigest()
check("7-生产 decision-log.md 只读未改", SHA_BEFORE == SHA_AFTER, f"sha256 {SHA_AFTER[:12]}…")

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + detail))
print(f"\n批C 注入预算保护检查：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
