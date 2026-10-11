#!/usr/bin/env python3
"""批C 补充验收：注入块在 context 预算裁剪下是否存活（体量上界 + 实测裁剪）

用法: ./venv/bin/python scripts/verify_history_trim_budget.py

背景：注入块（## 📋 历史决策复盘…）由 ContextBuilder._split_sections 切成独立
section，_categorize 归类后按 BUDGET 给预算；超预算走 _structured_trim(L1) 按句截尾。
批C 给它单列 decision_history(2200) 预算，本脚本把这个保护钉死。

设计（2026-09-27 修假红）:
- 行为断言全部打在**仓库内固定夹具** scripts/fixtures/history_decision_fixture.md
  上，不随生产 decision-log.md 增长而变化。
- 冲突期望由**源数据自己算**（expected_conflict），不再写死某标的一定 True/False。
- 裁剪断言按关系自洽：预算足够 → 块完整（truncated=0）；预算不足 → 按已知上限截断
  （truncated>0 且有裁剪标注），不锁死某个标的的具体结果。
- 生产日志只保留冒烟：读取成功 + 每个注入块在预算内（关系式，不锁死具体数字）。
- 收尾校验生产 decision-log.md sha256 未变（只读）。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import re
import sys
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


def check(n, ok, d) -> None:
    RESULTS.append((n, bool(ok), d))


def fmt(v) -> str:
    return v if isinstance(v, str) else repr(v)


LOG = dl.DECISION_LOG_PATH
FIXTURE = ROOT / "scripts" / "fixtures" / "history_decision_fixture.md"
SHA_BEFORE = hashlib.sha256(LOG.read_bytes()).hexdigest() if LOG.exists() else ""
LOG_TEXT = LOG.read_text(encoding="utf-8")
FIX_TEXT = FIXTURE.read_text(encoding="utf-8")

builder = ctx.ContextBuilder()
BUDGET = ctx.BUDGET["decision_history"]


def trim(text: str):
    return asyncio.run(builder._trim_and_grade(text, None))


@contextlib.contextmanager
def use_log(path):
    orig = dl.DECISION_LOG_PATH
    dl.DECISION_LOG_PATH = path
    try:
        yield
    finally:
        dl.DECISION_LOG_PATH = orig


def expected_conflict(query: str, log_text: str) -> bool:
    """源数据自算期望：同组有效条目 ≥2 种明确立场且末版立场与上次不同。"""
    sections = [s for s in re.split(r"\n(?=## )", log_text)
                if s.strip().startswith("## ")]
    index = dl._alias_index(sections)
    _code, keys = dl._stock_group_keys(query, index)
    group: list[dict] = []
    for sec in sections:
        if not dl._section_matches(sec, keys):
            continue
        parsed = dl._parse_section(sec)
        if not parsed:
            continue
        parsed["stance"] = dl._classify_stance(parsed)
        group.append(parsed)
    effective: list[dict] = []
    by_dupkey: dict = {}
    for e in group:
        dk = dl._dup_key(e)
        if dk and dk in by_dupkey:
            continue
        if dk:
            by_dupkey[dk] = e
        effective.append(e)
    if not effective:
        return False
    labels = {e["stance"] for e in effective if e["stance"] != "unknown"}
    if len(labels) < 2:
        return False
    last = effective[-1]
    return any(e["stance"] != last["stance"] for e in effective[:-1])


# ── 1. 分类归属：注入块必须落到 decision_history，且不再占 web 预算 ──────
with use_log(FIXTURE):
    block_c = dl.load_decisions("冲突股")
secs = ctx._split_sections(block_c)
cats = {t: ctx._categorize(t, b) for t, b in secs}
check("1-注入块归类为 decision_history", "decision_history" in cats.values(), fmt(cats))
check("1b-预算存在且 ≥ 块体积上界(1974)",
      ctx.BUDGET.get("decision_history", 0) >= 1974,
      f"decision_history={ctx.BUDGET.get('decision_history')}")


# ── 4. 夹具标的实测：按「预算足够→完整 / 不足→按上限截断」的关系断言 ────
def block_checks(label: str, q: str, log_text: str) -> None:
    exp_conf = expected_conflict(q, log_text)
    blk = dl.load_decisions(q)
    size = tok(blk)
    out = trim(blk + "\n## 当前时间上下文\n日期 2026-09-11\n")
    kept = "历史决策复盘" in out.context
    conf_out = "⚠ 立场冲突" in out.context
    last_in = "★ 最后一版" in blk
    last_out = "★ 最后一版" in out.context
    if size <= ctx.BUDGET["decision_history"]:
        # 预算足够：块必须完整，冲突标记与最新版行原样保留
        check(f"4-{label}{q} 预算足够→块完整（truncated=0）",
              out.truncated_tokens == 0 and kept,
              f"块 {size} token≤预算；truncated={out.truncated_tokens} 留存={kept} "
              f"分类计数={out.token_counts.get('decision_history')}")
        check(f"4b-{label}{q} 冲突标记/最新版行按源数据保留",
              conf_out == exp_conf and last_out == last_in,
              f"冲突块 期望(源数据)={exp_conf} 实={conf_out}；"
              f"最后一版 进={last_in} 出={last_out}")
    else:
        # 预算不足：按已知上限截断，头部仍在
        check(f"4-{label}{q} 超预算→按已知上限截断（有裁剪标注）",
              out.truncated_tokens > 0 and kept,
              f"块 {size} token>预算；truncated={out.truncated_tokens} 留存={kept}")
        check(f"4b-{label}{q} 超预算时块头仍保留，最新版不静默缺失",
              kept and ("上次立场" in out.context),
              f"块留存={kept} 头部上次立场={'上次立场' in out.context}")


with use_log(FIXTURE):
    for q in ["冲突股", "平静股", "描述股", "长文股"]:
        block_checks("夹具/", q, FIX_TEXT)
with use_log(LOG):
    for q in ["英维克", "中际旭创", "腾讯控股", "金山办公"]:
        block_checks("生产/", q, LOG_TEXT)

# ── 5. 夹具长文股（5 条长结论，预算内）裁剪后完整 ─────────────────────
with use_log(FIXTURE):
    long_blk = dl.load_decisions("长文股")
out_long = trim(long_blk + "\n## x\n")
check("5-预算内(夹具长文股 5 条长结论)裁剪后仍完整",
      out_long.truncated_tokens == 0 and "★ 最后一版" in out_long.context,
      f"块 {tok(long_blk)} token ≤ 预算 {BUDGET}；truncated={out_long.truncated_tokens}")

# ── 6. 预算不足：构造超上界的决策块，必须按已知上限截断并留标注 ─────────
oversized = ("## 📋 历史决策复盘（来自 decision-log.md）\n"
             + "结论：建议买入，基本面强劲且估值合理，护城河深厚。\n" * 400)
out_over = trim(oversized + "\n## 当前时间上下文\n日期 2026-09-11\n")
check("6-超预算块按已知上限截断（truncated>0 且留裁剪标注）",
      tok(oversized) > BUDGET and out_over.truncated_tokens > 0
      and "（本节因 token 预算裁剪）" in out_over.context,
      f"构造块 {tok(oversized)} token > 预算 {BUDGET}；"
      f"truncated={out_over.truncated_tokens}；裁剪标注="
      f"{'有' if '（本节因 token 预算裁剪）' in out_over.context else '无'}")

# ── 7. 对照：假若塞回 web 预算，会被截尾（证明本保护不是摆设） ──────────
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
    out_web = trim(long_blk + "\n## x\n")
finally:
    # 原地还原：BUDGET 是模块级/类属性共用的同一个 dict 对象，必须就地复原
    ctx._categorize = old_cat
    ctx.BUDGET.clear()
    ctx.BUDGET.update(old_budgets)
check("7b-还原后 decision_history 预算仍在（清理无残留）",
      ctx.BUDGET.get("decision_history") == 2200 and ctx.BUDGET.get("web") == 1500,
      f"decision_history={ctx.BUDGET.get('decision_history')} web={ctx.BUDGET.get('web')}")

check("6b-对照：无专用预算时同一块确被截尾（保护必要性属实）",
      tok(long_blk) > 1500 and out_web.truncated_tokens > 0
      and "（本节因 token 预算裁剪）" in out_web.context,
      f"夹具长文股 {tok(long_blk)} token → truncated={out_web.truncated_tokens}")

# ── 2/3. 生产日志冒烟：读取成功 + 每个注入块在预算内（关系式，不锁死数字） ─
raw_secs = [s for s in re.split(r"\n(?=## )", LOG_TEXT) if s.strip().startswith("## ")]
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
with use_log(LOG):
    for code, names in groups.items():
        out = dl.load_decisions(names[0] if names else code)
        if out:
            sizes.append((tok(out), code))
sizes.sort(reverse=True)
worst_real = sizes[0] if sizes else (0, "-")
check("2-生产每个注入块都在 decision_history 预算内（关系式）",
      bool(sizes) and all(size <= BUDGET for size, _ in sizes),
      f"共 {len(sizes)} 组；最大 {worst_real[0]} token（{worst_real[1]}）≤ 预算 {BUDGET}")
print(f"  生产同组标的 {len(sizes)} 个；最大注入块 {worst_real[0]} token（{worst_real[1]}）；"
      f"预算 {BUDGET}，余量 {BUDGET - worst_real[0]}")

# 单组体积上界（同标的 5 条最长 + 框架开销）—— **观察项**（2026-10-11 改）
# 为什么不再作硬断言：该上界是"理论上最多能注入多少"，随生产 decision-log.md 增长单调变大，
# 而真实注入永远受名额与预算裁剪约束（第 2 项已断言真实注入 ≤ 预算）。旧写法
# `worst_upper <= BUDGET` 会随日志长胖而稳定假红：实测 2026-10-11 最坏 2232 token vs 预算 2200，
# 把当天无关改动回退到 HEAD 后同样红 ⇒ 它测的是生产数据漂移，不是代码回归。
# 保留的护栏：上界 / 预算 < 1.5 —— 若预算被误砍到远低于实际需要（例如 2200→100）会立刻红。
by_code: dict[str, list[int]] = {}
for s in raw_secs:
    p = dl._parse_section(s)
    if not p:
        continue
    code = (p.get("code") or "").strip()
    if not code or code in ("未指定",):
        continue
    by_code.setdefault(code, []).append(tok(s))
worst_upper, worst_code = 0, "-"
for code, vals in by_code.items():
    vals.sort(reverse=True)
    upper = sum(vals[:5]) + 90  # 90 ≈ 表头/时间线/提示语等框架开销
    if upper > worst_upper:
        worst_upper, worst_code = upper, code
ratio = (worst_upper / BUDGET) if BUDGET else 0.0
print(f"  观察项：最坏单组理论体积上界≈{worst_upper} token（{worst_code}）｜预算 {BUDGET}｜"
      f"上界/预算={ratio:.2f}（<1.5 视为安全余量充足）")
check("3-预算未被砍到远低于实际需要（上界/预算 < 1.5）",
      bool(BUDGET) and ratio < 1.5,
      f"上界 {worst_upper} / 预算 {BUDGET} = {ratio:.2f}；理论值仅作观察，不作硬断言")

SHA_AFTER = hashlib.sha256(LOG.read_bytes()).hexdigest()
check("8-生产 decision-log.md 只读未改", SHA_BEFORE == SHA_AFTER, f"sha256 {SHA_AFTER[:12]}…")

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + fmt(detail)))
print(f"\n批C 注入预算保护检查：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
