#!/usr/bin/env python3
"""标的记忆召回一致性自检（2026-09-20）。

背景：data/reports/ 的历史记忆按标的检索，旧实现只有"字符串双向包含" ——
同一只票换个写法就落到不同结果，实测基线：
    '中际旭创'        -> 1 篇（arguments='300308 中际旭创'）
    '300308'          -> 3 篇（8 月那批 arguments 只写代码，打名字召不回）
    '中际旭创未来三年' -> 0 篇（带修饰词整句，两边谁也不含谁 -> 静默 0 注入，无提示）
修法：两边都用项目统一入口 resolve_symbol 解析成规范代码，代码相同即同组。

本脚本核对三件事（用户口径）：
  1. 同一标的的各种写法召回到同一组；
  2. 不减少任何一条旧规则命中（只增不减，逐条对账）；
  3. 稳健性：解析器不可用/抛异常时退回旧规则，不拖慢、不带崩。

跑法（必须从仓库根、用 venv 的 python）：
  cd /home/q/ai_berkshire_web && venv/bin/python scripts/verify_stock_memory_recall.py
无副作用：只读 data/reports/，不写任何生产文件。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.tools.market_data as md  # noqa: E402
from app.tools import stock_memory as sm  # noqa: E402

PASS = 0
FAIL = 0
NOTES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------------------
# 旧规则复刻（对账基准："不减少任何一条现有命中"）
# ---------------------------------------------------------------------------
def legacy_find(query: str, limit: int = 1000) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    is_code = len(q) == 6 and q.isdigit()
    out: list[dict] = []
    for p in sm._iter_reports():
        info = sm._report_info(p)
        args = info["arguments"]
        if not args and not is_code:
            continue
        if is_code:
            hit = q in args or q in p.name
        else:
            hit = bool(args) and (q in args or args in q)
        if not hit:
            continue
        info["excerpt"] = sm._read_excerpt(p)
        out.append(info)
    out.sort(key=sm._ts_sort_key, reverse=True)
    return out[:limit]


def files_of(rows: list[dict]) -> set[str]:
    return {Path(r["file"]).name for r in rows}


def main() -> int:
    print("标的记忆召回一致性自检")
    print(f"报告库：{len(sm._iter_reports())} 篇\n")

    # ── 一、写法一致性 ──────────────────────────────────────────────────
    print("一、同一标的的各种写法必须落到同一组")
    groups = [
        ("中际旭创", ["中际旭创", "300308", "300308 中际旭创"]),
        ("澜起科技", ["澜起科技", "688008", "澜起科技688008"]),
        ("贵州茅台", ["贵州茅台", "600519", "600519 贵州茅台"]),
        ("腾讯", ["腾讯", "腾讯控股"]),
    ]
    for label, variants in groups:
        res = {v: files_of(sm.find_stock_reports(v, limit=1000)) for v in variants}
        base = res[variants[0]]
        same = all(res[v] == base for v in variants)
        check(
            f"{label}：{' / '.join(variants)} 同组",
            same,
            "; ".join(f"{v}={len(res[v])}篇" for v in variants),
        )

    print("\n二、带修饰词的整句（旧实现静默 0 注入）")
    mod_cases = [
        ("中际旭创未来三年", "300308"),
        ("分析一下腾讯控股", "腾讯"),
    ]
    for a, b in mod_cases:
        ra, rb = files_of(sm.find_stock_reports(a, limit=1000)), files_of(sm.find_stock_reports(b, limit=1000))
        check(f"{a!r} 与 {b!r} 同组", ra == rb and len(ra) > 0, f"{len(ra)} 篇 / {len(rb)} 篇")

    print("\n三、不该被归组的输入不误召")
    for q in ["AI算力", "2026房地产", "半导体设备产业链", "x"]:
        new, old = files_of(sm.find_stock_reports(q, limit=1000)), files_of(legacy_find(q))
        check(f"{q!r} 与旧规则一致（无归一误召）", new == old, f"新 {len(new)} 篇 / 旧 {len(old)} 篇")

    # ── 二、只增不减：全库穷举对账 ──────────────────────────────────────
    print("\n四、只增不减（逐条对账，旧命中必须全部保留）")
    queries = sorted({sm._report_info(p)["arguments"].strip() for p in sm._iter_reports()} - {""})
    queries += [
        "中际旭创", "300308", "澜起科技", "688008", "腾讯", "腾讯控股", "贵州茅台", "600519",
        "美光", "中际讯创2026年怎么样", "调试", "x", "", " ",
    ]
    lost: list[str] = []
    gained = 0
    for q in queries:
        new, old = files_of(sm.find_stock_reports(q, limit=1000)), files_of(legacy_find(q))
        missing = old - new
        if missing:
            lost.append(f"{q!r} 丢了 {sorted(missing)}")
        gained += len(new - old)
    check(f"{len(queries)} 个查询：无任何旧命中丢失", not lost, f"新增命中合计 {gained} 条（含重复计入）")
    for line in lost[:5]:
        NOTES.append("丢失：" + line)

    print("\n五、limit / 排序语义未变")
    r3 = sm.find_stock_reports("300308", limit=3)
    rall = sm.find_stock_reports("300308", limit=1000)
    check("limit=3 生效", len(r3) == 3, f"{len(r3)} 篇")
    check(
        "limit=3 = 全集按时间倒序的前 3",
        [r["file"] for r in r3] == [r["file"] for r in rall[:3]],
        f"全集 {len(rall)} 篇",
    )
    ts = [r["created_at"] for r in rall]
    check("仍按时间倒序", ts == sorted(ts, reverse=True), str(ts)[:80])

    # ── 三、稳健性 ──────────────────────────────────────────────────────
    print("\n六、解析器不可用/抛异常时退回旧规则")
    orig = md.resolve_symbol
    try:
        def boom(*_a, **_k):
            raise RuntimeError("模拟解析器崩了")

        md.resolve_symbol = boom  # type: ignore[assignment]
        sm.reset_symbol_memo()
        try:
            new, old = files_of(sm.find_stock_reports("中际旭创", limit=1000)), files_of(legacy_find("中际旭创"))
            check("解析器抛异常：不抛、退回旧规则", new == old, f"新 {len(new)} 篇 / 旧 {len(old)} 篇")
        except Exception as e:  # noqa: BLE001
            check("解析器抛异常：不抛、退回旧规则", False, f"{type(e).__name__}: {e}")

        sm.reset_symbol_memo()
        try:
            r = sm.build_memory_context("中际旭创", limit=3)
            check("解析器抛异常：注入路径不崩", isinstance(r, str), f"{len(r)} 字符")
        except Exception as e:  # noqa: BLE001
            check("解析器抛异常：注入路径不崩", False, f"{type(e).__name__}: {e}")
    finally:
        md.resolve_symbol = orig  # type: ignore[assignment]
        sm.reset_symbol_memo()

    print("\n七、记忆化生效（同一 arguments 不重复解析）")
    calls = {"n": 0}
    orig = md.resolve_symbol
    try:
        def counted(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)  # type: ignore[misc]

        md.resolve_symbol = counted  # type: ignore[assignment]
        sm.reset_symbol_memo()
        sm.find_stock_reports("中际旭创", limit=1000)
        first = calls["n"]
        sm.find_stock_reports("中际旭创", limit=1000)
        second = calls["n"] - first
        check("首次解析后再次检索 0 次解析", second == 0, f"首轮 {first} 次 / 次轮 {second} 次")
        check("解析次数不超过库篇数", first <= len(sm._iter_reports()), f"{first} 次")
    finally:
        md.resolve_symbol = orig  # type: ignore[assignment]
        sm.reset_symbol_memo()

    print("\n八、空 arguments 行为与旧版一致")
    check("空查询返回空", sm.find_stock_reports("") == [] and sm.find_stock_reports("   ") == [])
    empty_files = {Path(p).name for p in sm._iter_reports() if not sm._report_info(p)["arguments"].strip()}
    hit_empty = files_of(sm.find_stock_reports("中际旭创", limit=1000)) & empty_files
    check("空 arguments 报告不因归组被召入", not hit_empty, f"库内空 arguments {len(empty_files)} 篇")

    # ── 四、注入层 ──────────────────────────────────────────────────────
    print("\n九、注入产物（build_memory_context）")
    # 口径：'标的: <查询原文>' 一行本就随写法不同（记录用户怎么问的），
    # 一致性只对报告列表部分（篇数 + 每条文件名/时间/参数/摘要）要求逐字相同。
    def body_of(text: str) -> str:
        return "\n".join(line for line in text.split("\n") if not line.startswith("标的: "))

    texts = {v: sm.build_memory_context(v, limit=3) for v in ["中际旭创", "300308", "300308 中际旭创"]}
    bodies = {v: body_of(t) for v, t in texts.items()}
    check(
        "各写法注入的报告列表逐字一致",
        len(set(bodies.values())) == 1 and len(bodies["300308"]) > 0,
        "; ".join(f"{v}={len(t)}字符" for v, t in texts.items()),
    )
    def _count(text: str) -> str:
        m = re.search(r"相关历史报告 (\d+) 篇", text)
        return m.group(1) if m else "?"

    counts = {v: _count(t) for v, t in texts.items()}
    check("注入篇数一致", len(set(counts.values())) == 1, "; ".join(f"{v}={c}篇" for v, c in counts.items()))
    t = texts["300308"]
    check("节标题格式未变", t.startswith("=== 历史研究记忆 ===") and t.rstrip().endswith("=== 记忆结束 ==="))

    print("\n十、真实链路 query（_extract_stock_name 之后）")
    from app.harness.decision_log import _extract_stock_name

    a = _extract_stock_name("中际旭创未来三年")
    b = _extract_stock_name("300308")
    ta, tb = sm.build_memory_context(a, limit=3), sm.build_memory_context(b, limit=3)
    check(
        "带修饰词的输入与代码注入同一组报告",
        body_of(ta) == body_of(tb) and len(ta) > 0,
        f"{a!r} {len(ta)}字符 / {b!r} {len(tb)}字符",
    )

    print("\n十一、性能（不得引入网络/慢路径）")
    sm.reset_symbol_memo()
    t0 = time.time()
    sm.find_stock_reports("中际旭创", limit=3)
    dt = time.time() - t0
    check("单次检索耗时 < 1.0s", dt < 1.0, f"{dt*1000:.1f} ms（记忆化后）")
    t0 = time.time()
    for q in ["中际旭创", "澜起科技", "贵州茅台", "腾讯", "AI算力"]:
        sm.find_stock_reports(q, limit=3)
    dt = time.time() - t0
    check("5 次检索总耗时 < 2.0s", dt < 2.0, f"{dt*1000:.1f} ms")

    print(f"\n结果：PASS {PASS} / FAIL {FAIL}")
    for n in NOTES:
        print("  ⚠ " + n)
    print("ALL PASSED" if FAIL == 0 else "FAILED")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
