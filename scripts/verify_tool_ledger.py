#!/usr/bin/env python3
"""第2层自检：工具调用明细账本 + 报告 meta 自动对账（2026-10-08）。

背景（静态审计风险项）：
  126 份 meta.json 中只有 22 份含 tool_calls，且全部集中在 2026-09-13 之后
  的多智能体报告；单智能体类报告从来没有该字段。save_report 只在
  tool_calls.total 为真时才写字段，于是「零次工具调用」与「当时还没有这个
  字段」在产物上无法区分。逐次调用的时间/参数/返回明细（原 scratchpad）
  也随时可被清理，summary/total>0 的报告没有任何明细可复核。

本自检覆盖：
  A. 明细账本写入：每次调用落一条含 时间戳/task_id/agent/工具/参数摘要/
     返回长度/结果短摘要/任务内序号 的记录；结果摘要截断；序号连续。
  B. 自动对账：账本明细条数 == record_tool_counts 汇总的 ledger_count ==
     落盘 meta.tool_calls.total；零调用任务显式写 total=0 + ledger="none"。
  C. 存在性核对：账本里的每次调用能在同一 code path 写出的服务侧
     scratchpad 留痕里找到对应记录。
  D. 存量报告巡检：逐份 meta 对账；没有账本的老报告显式统计为
     「无明细（历史）」，不静默跳过、不算通过。

用法:
    venv/bin/python scripts/verify_tool_ledger.py
退出码：0 = 无 FAIL；1 = 存在 FAIL。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILED: list = []

# 存量报告分类计数（显式统计，绝不静默）
HIST_NO_FIELD = 0       # meta 里根本没有 tool_calls 字段
HIST_NO_LEDGER = 0      # 有汇总但没有可对账的明细账本（9 月那批）


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def _ledger_lines(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                rows.append({"_unparsable": line[:200]})
    return rows


# ==================== A. 明细账本写入 ====================

def part_a_ledger_write(tmp: Path) -> Path:
    from app.harness import runner as runner_mod
    from app.harness.runner import (
        LEDGER_PREVIEW_CHARS,
        AgentRunner,
        count_tool_ledger,
        ledger_relpath,
    )

    print("\n=== A. 明细账本写入 ===")
    ledger_dir = tmp / "tool_ledger"
    scratch_dir = tmp / "scratchpad"
    orig_ledger, orig_scratch = runner_mod.TOOL_LEDGER_DIR, runner_mod.SCRATCHPAD_DIR
    runner_mod.TOOL_LEDGER_DIR = ledger_dir
    runner_mod.SCRATCHPAD_DIR = scratch_dir
    task_id = "__ledger_unit__"
    try:
        runner = AgentRunner(task_id=task_id)
        calls = [
            ("financial-analyst", "get_financial_data", {"symbol": "600519"}, '{"ok": 1}' * 400),
            ("financial-analyst", "calc", {"expression": "1+1"}, "2"),
            ("industry-researcher", "web_search", {"query": "新能源" * 100}, "x" * 50),
        ]
        for agent, tool, args, result in calls:
            asyncio.run(runner._log_tool_call(agent, tool, args, result))

        path = ledger_dir / f"{task_id}.jsonl"
        rows = _ledger_lines(path)
        check("A1 账本文件落盘且条数正确", path.exists() and len(rows) == 3, f"rows={len(rows)}")
        required = {"seq", "ts", "task_id", "agent", "tool", "args_summary",
                    "result_len", "result_preview"}
        check("A2 每条含全部必需字段",
              bool(rows) and all(required <= set(r) for r in rows),
              str([sorted(set(r)) for r in rows[:1]]))
        check("A3 序号在任务内连续（1..N）",
              [r.get("seq") for r in rows] == [1, 2, 3],
              str([r.get("seq") for r in rows]))
        check("A4 task_id / agent / 工具名正确",
              bool(rows)
              and all(r.get("task_id") == task_id for r in rows)
              and rows[0]["agent"] == "financial-analyst"
              and rows[1]["tool"] == "calc",
              str(rows[:1]))
        check("A5 返回长度被记录（len(result)）",
              len(rows) == 3
              and rows[0]["result_len"] == len('{"ok": 1}' * 400) and rows[1]["result_len"] == 1,
              str([r.get("result_len") for r in rows]))
        check(f"A6 结果短摘要截断到 {LEDGER_PREVIEW_CHARS} 字符",
              len(rows) == 3
              and all(len(r["result_preview"]) <= LEDGER_PREVIEW_CHARS for r in rows)
              and len(rows[2]["result_preview"]) == 50,
              str([len(r.get("result_preview") or "") for r in rows]))
        check("A7 参数摘要单行且有上限",
              all("\n" not in r["args_summary"] for r in rows)
              and all(len(r["args_summary"]) <= 300 for r in rows))
        check("A8 count_tool_ledger 与实际行数一致", count_tool_ledger(task_id) == 3,
              str(count_tool_ledger(task_id)))
        rel = ledger_relpath(task_id)
        check("A9 账本位于独立的 tool_ledger 目录（非 scratchpad/reports）",
              "tool_ledger" in rel and rel.endswith(f"{task_id}.jsonl"), rel)

        # 被拒调用（ledger=False）不得进账本
        asyncio.run(runner._log_tool_call(
            "financial-analyst", "calc__LIMIT_HIT", {}, "limit", ledger=False))
        check("A10 被拒调用（ledger=False）不写入明细账本",
              count_tool_ledger(task_id) == 3, str(count_tool_ledger(task_id)))
        return path
    finally:
        runner_mod.TOOL_LEDGER_DIR = orig_ledger
        runner_mod.SCRATCHPAD_DIR = orig_scratch


# ==================== B. 自动对账 ====================

def part_b_reconcile(tmp: Path) -> None:
    from app.harness import persist as P
    from app.harness import runner as runner_mod
    from app.harness.runner import (
        AgentRunner,
        get_tool_counts,
        record_tool_counts,
    )

    print("\n=== B. 自动对账（明细条数 == 汇总计数 == meta.total）===")
    ledger_dir = tmp / "tool_ledger_b"
    scratch_dir = tmp / "scratchpad_b"
    reports_dir = tmp / "reports_b"
    reports_dir.mkdir(parents=True, exist_ok=True)
    orig_ledger, orig_scratch, orig_reports = (
        runner_mod.TOOL_LEDGER_DIR, runner_mod.SCRATCHPAD_DIR, P.REPORTS_DIR)
    runner_mod.TOOL_LEDGER_DIR = ledger_dir
    runner_mod.SCRATCHPAD_DIR = scratch_dir
    P.REPORTS_DIR = reports_dir
    task_id = "__ledger_reconcile__"
    zero_id = "__ledger_zero__"
    try:
        runner = AgentRunner(task_id=task_id)
        for agent, tool in (("a", "web_search"), ("a", "web_search"),
                            ("a", "calc"), ("b", "get_stock_price")):
            asyncio.run(runner._log_tool_call(agent, tool, {"k": tool}, "r" * 10))

        counts = {"a": {"web_search": 2, "calc": 1}, "b": {"get_stock_price": 1}}
        summary = record_tool_counts(task_id, counts)

        check("B1 汇总 total == 账本明细条数",
              summary["total"] == 4 and summary["ledger_count"] == 4,
              f"total={summary['total']} ledger_count={summary['ledger_count']}")
        got = get_tool_counts(task_id)
        check("B2 get_tool_counts 带 ledger_count 且 == total",
              got.get("total") == 4 and got.get("ledger_count") == 4 and got.get("ledger", "").endswith(".jsonl"),
              str({k: got.get(k) for k in ("total", "ledger_count", "ledger")}))

        path = P.get_repository().save_report(
            "management-deep-dive", "对账测试", "# 报告", 5.0, {"total_tokens": 9},
            task_id=task_id, tool_calls=get_tool_counts(task_id),
        )
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        tc = meta.get("tool_calls") or {}
        check("B3 落盘 meta 三方一致（total == ledger_count == 实际行数）",
              tc.get("total") == 4 and tc.get("ledger_count") == 4
              and len(_ledger_lines(ledger_dir / f"{task_id}.jsonl")) == 4,
              str(tc))

        # 零调用任务：显式 total=0 + ledger="none"
        record_tool_counts(zero_id, {})
        zpath = P.get_repository().save_report(
            "industry-research", "零调用", "# 报告2", 1.0, {"total_tokens": 3},
            task_id=zero_id, tool_calls=get_tool_counts(zero_id),
        )
        zmeta = json.loads(zpath.with_suffix(".meta.json").read_text(encoding="utf-8"))
        ztc = zmeta.get("tool_calls") or {}
        check("B4 零调用任务显式写 total=0 + ledger=none",
              ztc.get("total") == 0 and ztc.get("ledger") == "none"
              and ztc.get("ledger_count") == 0,
              str(ztc))
    finally:
        runner_mod.TOOL_LEDGER_DIR = orig_ledger
        runner_mod.SCRATCHPAD_DIR = orig_scratch
        P.REPORTS_DIR = orig_reports


# ==================== C. 存在性核对 ====================

def part_c_existence(tmp: Path) -> None:
    from app.harness import runner as runner_mod
    from app.harness.runner import AgentRunner

    print("\n=== C. 存在性核对（账本调用 ↔ 服务侧留痕）===")
    ledger_dir = tmp / "tool_ledger_c"
    scratch_dir = tmp / "scratchpad_c"
    orig_ledger, orig_scratch = runner_mod.TOOL_LEDGER_DIR, runner_mod.SCRATCHPAD_DIR
    runner_mod.TOOL_LEDGER_DIR = ledger_dir
    runner_mod.SCRATCHPAD_DIR = scratch_dir
    task_id = "__ledger_exists__"
    try:
        runner = AgentRunner(task_id=task_id)
        for tool in ("get_financial_data", "calc", "web_search"):
            asyncio.run(runner._log_tool_call("agent-x", tool, {"q": tool}, f"result-{tool}"))

        led = _ledger_lines(ledger_dir / f"{task_id}.jsonl")
        scratch = _ledger_lines(scratch_dir / f"{task_id}.jsonl")
        # 同一 code path 写出的服务侧留痕：按 (agent, tool) 多重集核对
        def _key(row):
            return (row.get("agent"), row.get("tool"))
        from collections import Counter
        led_keys = Counter(_key(r) for r in led)
        scratch_keys = Counter(_key(r) for r in scratch)
        missing = led_keys - scratch_keys
        check("C1 每条账本记录都能在服务侧 scratchpad 留痕中找到",
              not missing and len(scratch) == len(led),
              f"missing={dict(missing)} ledger={len(led)} scratch={len(scratch)}")
        check("C2 服务侧留痕条数 == 账本条数", len(scratch) == len(led),
              f"{len(scratch)} vs {len(led)}")
    finally:
        runner_mod.TOOL_LEDGER_DIR = orig_ledger
        runner_mod.SCRATCHPAD_DIR = orig_scratch


# ==================== D. 存量报告巡检 ====================

def part_d_scan_reports() -> None:
    global HIST_NO_FIELD, HIST_NO_LEDGER
    from app.core.config import REPORTS_DIR

    print("\n=== D. 存量报告 meta 对账 ===")
    metas = sorted(REPORTS_DIR.glob("*.meta.json")) if REPORTS_DIR.exists() else []
    print(f"  扫描 {len(metas)} 份报告 meta（只读）")
    new_total = new_ok = new_fail = 0
    for mp in metas:
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            check(f"D 可解析 {mp.name}", False, str(e))
            continue
        tc = meta.get("tool_calls")
        if not isinstance(tc, dict):
            HIST_NO_FIELD += 1
            continue
        total = int(tc.get("total") or 0)
        ledger = str(tc.get("ledger") or "")
        ledger_count = tc.get("ledger_count")
        if total == 0:
            check(f"D 零调用记录 {mp.name} 显式标记",
                  ledger == "none" and int(ledger_count or 0) == 0,
                  str(tc))
            continue
        # 有实际调用：分两类，绝不静默跳过。
        #   历史：meta 里还没有 ledger 字段（2026-10-08 之前的产物）——允许无明细，
        #         显式计入 HIST_NO_LEDGER，不算通过也不算失败；
        #   新式：meta 里已带 ledger 字段 —— 必须能定位到明细账本且三方一致，
        #         定位不到 / 对不上 = FAIL（否则新链路的失败会被伪装成「历史」）。
        if "ledger" not in tc:
            HIST_NO_LEDGER += 1
            continue
        new_total += 1
        ledger_path = ROOT / ledger if ledger not in ("", "none", "unknown") else None
        if ledger_path is None or not ledger_path.exists():
            new_fail += 1
            check(f"D 新式报告 {mp.name} 能定位明细账本", False,
                  f"ledger={ledger!r} total={total}")
            continue
        rows = _ledger_lines(ledger_path)
        try:
            lc = int(ledger_count if ledger_count is not None else -1)
        except (TypeError, ValueError):
            lc = -1
        good = total == lc == len(rows)
        if good:
            new_ok += 1
        else:
            new_fail += 1
        check(f"D 对账 {mp.name}（total==ledger_count==行数）", good,
              f"total={total} ledger_count={ledger_count} rows={len(rows)}")
    print(f"  新式报告（meta 带 ledger 字段）：{new_total} 份"
          f"，可对账 {new_ok} 份，对不上 {new_fail} 份")


def main() -> int:
    print("=" * 64)
    print("工具调用明细账本 + 自动对账自检")
    print("=" * 64)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        part_a_ledger_write(tmp)
        part_b_reconcile(tmp)
        part_c_existence(tmp)
    part_d_scan_reports()

    print("\n" + "=" * 64)
    print(f"PASS {PASS} / FAIL {FAIL}")
    print(f"无明细（历史·meta 无 tool_calls 字段）：{HIST_NO_FIELD} 份")
    print(f"无明细（历史·有汇总但无账本）：{HIST_NO_LEDGER} 份")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
