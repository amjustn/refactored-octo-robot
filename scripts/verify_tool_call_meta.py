#!/usr/bin/env python3
"""第 3 步自检：报告 meta 的工具调用汇总（可观测性）。

起因（静态审计风险项，已实测复现）：91 份 meta.json 里没有任何工具字段，
工具调用只落在 data/scratchpad/<task>.jsonl，"每任务工具调用数 / 是否撞上限"
在报告侧完全看不到。

本自检覆盖：
- 汇总函数（计数、排序、非法值、空输入、撞上限明细）
- 留痕（有界字典、读写）
- 落盘（save_report 写入 meta["tool_calls"]，且不污染无工具任务）
- 接线（runner 在 run() 末尾留痕；harness run() 把它传给 save_report）

用法: venv/bin/python scripts/verify_tool_call_meta.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILED: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def part_a_summary() -> None:
    from app.harness.runner import get_tool_counts, record_tool_counts, summarize_tool_calls

    print("\n=== A. 汇总函数 ===")
    counts = {"researcher": {"web_search": 3, "calc": 2}, "financial-analyst": {"web_search": 1, "get_stock_price": 4}}
    s = summarize_tool_calls(counts)
    check("A1 总数正确（3+2+1+4=10）", s["total"] == 10, str(s))
    check("A2 by_tool 按次数倒序", list(s["by_tool"].items())[0] == ("get_stock_price", 4), str(s["by_tool"]))
    check("A3 by_tool 合并同名工具（web_search 3+1=4）", s["by_tool"]["web_search"] == 4)
    check("A4 by_agent 汇总正确", s["by_agent"]["researcher"] == 5 and s["by_agent"]["financial-analyst"] == 5)
    check("A5 统计有工具使用的 Agent 数", s["agents_with_tools"] == 2)
    check("A6 无 limit_hits 时该字段不出现", "limit_hits" not in s)

    s2 = summarize_tool_calls(counts, {"web_search": 2, "calc": 1})
    check("A7 撞上限明细进入 limit_hits", s2["limit_hits"] == {"web_search": 2, "calc": 1}, str(s2.get("limit_hits")))

    check("A8 空输入 → total=0 且不抛异常",
          summarize_tool_calls({})["total"] == 0 and summarize_tool_calls(None)["total"] == 0)
    check("A9 非法计数被忽略（不抛异常）",
          summarize_tool_calls({"a": {"t": "x"}, "b": None})["total"] == 0)
    check("A10 零/负数计数不计入", summarize_tool_calls({"a": {"t": 0, "u": -3}})["total"] == 0)

    record_tool_counts("__verify_tc__", counts, {"web_search": 1})
    got = get_tool_counts("__verify_tc__")
    check("A11 留痕可读回且含 limit_hits", got.get("total") == 10 and got.get("limit_hits") == {"web_search": 1})
    check("A12 未留痕的 task_id 返回 {}", get_tool_counts("__nope__") == {})
    check("A13 留痕字典有界（<=64）",
          len(__import__("app.harness.runner", fromlist=["x"])._TOOL_COUNTS) <= 64)


def part_b_persist() -> None:
    from app.harness import persist as P

    print("\n=== B. 落盘到 meta.json ===")
    orig_dir = P.REPORTS_DIR
    with tempfile.TemporaryDirectory() as td:
        P.REPORTS_DIR = Path(td)
        try:
            tc = {"total": 7, "by_tool": {"web_search": 4, "calc": 3},
                  "by_agent": {"researcher": 7}, "agents_with_tools": 1,
                  "limit_hits": {"web_search": 1}}
            path = P.get_repository().save_report(
                "management-deep-dive", "测试查询", "# 报告", 12.5, {"total_tokens": 100},
                task_id="__verify_tc_meta__", tool_calls=tc,
            )
            meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            check("B1 meta 含 tool_calls", isinstance(meta.get("tool_calls"), dict), str(meta.keys()))
            check("B2 tool_calls.total 落盘", meta["tool_calls"]["total"] == 7)
            check("B3 by_tool 落盘且可读", meta["tool_calls"]["by_tool"]["web_search"] == 4)
            check("B4 limit_hits 落盘（观测撞上限）", meta["tool_calls"]["limit_hits"] == {"web_search": 1})

            # 无工具任务不应出现该字段（避免给所有报告塞空壳）
            path2 = P.get_repository().save_report(
                "industry-research", "行业查询", "# 报告2", 3.0, {"total_tokens": 10},
                task_id="__verify_tc_none__",
            )
            meta2 = json.loads(path2.with_suffix(".meta.json").read_text(encoding="utf-8"))
            check("B5 无工具任务不写 tool_calls 字段", "tool_calls" not in meta2, str(meta2.keys()))

            # total=0 的汇总同样不写
            path3 = P.get_repository().save_report(
                "industry-research", "行业查询2", "# 报告3", 3.0, {"total_tokens": 10},
                task_id="__verify_tc_zero__", tool_calls={"total": 0, "by_tool": {}},
            )
            meta3 = json.loads(path3.with_suffix(".meta.json").read_text(encoding="utf-8"))
            check("B6 total=0 时不写字段", "tool_calls" not in meta3)
        finally:
            P.REPORTS_DIR = orig_dir


def part_c_wiring() -> None:
    print("\n=== C. 接线（源码级）===")
    runner_src = (ROOT / "app" / "harness" / "runner.py").read_text(encoding="utf-8")
    check("C1 runner 在 run() 末尾留痕",
          "record_tool_counts(self.task_id, self._tool_call_counts, self._tool_limit_hits)" in runner_src)
    check("C2 撞上限时计数", "_tool_limit_hits[name] = runner._tool_limit_hits.get(name, 0) + 1" in runner_src)
    check("C3 每次 run 重置撞上限计数", "self._tool_limit_hits.clear()" in runner_src)
    init_src = (ROOT / "app" / "harness" / "__init__.py").read_text(encoding="utf-8")
    check("C4 harness 把汇总传给 save_report", "tool_calls=_get_tool_counts(spec.task_id)" in init_src)
    persist_src = (ROOT / "app" / "harness" / "persist.py").read_text(encoding="utf-8")
    check("C5 save_report 支持并写入 tool_calls",
          "tool_calls: Optional[dict] = None" in persist_src and 'meta["tool_calls"] = clean_tc' in persist_src)
    check("C6 字段长度有上限（by_tool/by_agent 各 20 条）",
          'items())[:20]' in persist_src)


def main() -> int:
    part_a_summary()
    part_b_persist()
    part_c_wiring()
    print("\n" + "=" * 56)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
