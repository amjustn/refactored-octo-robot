#!/usr/bin/env python3
"""单Agent工具循环上限自检（2026-09-13 批2 收尾）。

背景：给 management-deep-dive / investment-research 开 tools_enabled 之后，
端到端报告里出现"网络检索工具本次调用已达上限，部分治理细节以已获取的公开
数据为限"——每工具 3 次的上限是给多Agent成员设计的（并行分工，每个角色只
需要几次调用），单Agent是一个人干完整篇深度研究，3 次不够。

本自检覆盖：
- 常量语义（多Agent 保持 3，单Agent 给 8）
- 接线（单Agent 路径传大额度；多Agent 走默认）
- 行为（真实执行器：超额度返回 TOOL_LOOP_LIMIT；大额度下连续调用不被拒）

用法: venv/bin/python scripts/verify_tool_loop_caps.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
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


async def _call(executor, name: str, args: dict) -> str:
    return await executor(name, args)


def part_a_constants() -> None:
    import app.harness.runner as R

    print("\n=== A. 常量与接线 ===")
    check("A1 多Agent 每工具上限仍为 3（行为不变）", R.MAX_SAME_TOOL_CALLS == 3,
          str(R.MAX_SAME_TOOL_CALLS))
    check("A2 单Agent 上限为 8", R.MAX_SAME_TOOL_CALLS_SINGLE == 8,
          str(R.MAX_SAME_TOOL_CALLS_SINGLE))
    check("A3 单Agent 上限严格大于多Agent（确实是放宽）",
          R.MAX_SAME_TOOL_CALLS_SINGLE > R.MAX_SAME_TOOL_CALLS)

    src = (ROOT / "app" / "harness" / "runner.py").read_text(encoding="utf-8")
    check("A4 单Agent 路径按 skill_tools_enabled 选择上限",
          "_cap = MAX_SAME_TOOL_CALLS_SINGLE if skill_tools_enabled else MAX_SAME_TOOL_CALLS" in src)
    check("A5 上限作为参数传入执行器（不再硬编码引用全局常量）",
          "max_same_tool_calls: int = MAX_SAME_TOOL_CALLS" in src
          and "max_same_tool_calls=_cap" in src)
    check("A6 上限提示文案使用实际额度（便于留痕排查）",
          '达到上限 {max_same_tool_calls} 次' in src)
    # 多Agent 的两个调用点（成员Agent / 长文分篇）未传 _cap，仍走默认
    check("A7 多Agent/长文分支未传 _cap（保持 3）",
          src.count("max_same_tool_calls=_cap") == 1)


def part_b_behavior() -> None:
    import app.harness.runner as R

    print("\n=== B. 真实执行器行为（calc 为离线确定性工具） ===")
    runner = R.AgentRunner(task_id="__verify_caps__")

    # 小额度：第 3 次应当被拒
    ex_small = runner._tool_executor("cap_small", max_same_tool_calls=2)
    r1 = asyncio.run(_call(ex_small, "calc", {"expression": "1+1"}))
    r2 = asyncio.run(_call(ex_small, "calc", {"expression": "2+2"}))
    r3 = asyncio.run(_call(ex_small, "calc", {"expression": "3+3"}))
    check("B1 前 2 次真实执行成功", "2" in r1 and "4" in r2, f"{r1[:60]} / {r2[:60]}")
    try:
        d3 = json.loads(r3)
    except Exception:
        d3 = {}
    check("B2 第 3 次返回 TOOL_LOOP_LIMIT", d3.get("error") == "TOOL_LOOP_LIMIT", r3[:120])
    check("B3 上限提示含实际额度(2)", "上限 2 次" in (d3.get("message") or ""), d3.get("message", "")[:80])

    # 大额度（单Agent 配置 8）：连打 5 次同一工具都不应被拒
    ex_big = runner._tool_executor("cap_big", max_same_tool_calls=R.MAX_SAME_TOOL_CALLS_SINGLE)
    results = [asyncio.run(_call(ex_big, "calc", {"expression": f"{i}+{i}"})) for i in range(1, 6)]
    rejected = [r for r in results if "TOOL_LOOP_LIMIT" in r]
    check("B4 单Agent 额度下连续 5 次同工具调用全不被拒", not rejected,
          f"{len(rejected)} 次被拒")
    ex_big2 = runner._tool_executor("cap_big3", max_same_tool_calls=8)
    outs = [asyncio.run(_call(ex_big2, "calc", {"expression": f"{i}*1"})) for i in range(9)]
    check("B6 8 次额度：第 9 次被拒（有界，不会无限循环）",
          "TOOL_LOOP_LIMIT" not in outs[7] and "TOOL_LOOP_LIMIT" in outs[8],
          f"第8次={outs[7][:40]} 第9次={outs[8][:60]}")
    check("B7 调用计数按 agent_key 隔离且只统计真实执行（被拒的那次不计入）",
          runner._tool_call_counts.get("cap_small", {}).get("calc") == 2
          and runner._tool_call_counts.get("cap_big", {}).get("calc") == 5
          and runner._tool_call_counts.get("cap_big3", {}).get("calc") == 8,
          str({k: v.get("calc") for k, v in runner._tool_call_counts.items()}))


def main() -> int:
    part_a_constants()
    part_b_behavior()
    print("\n" + "=" * 56)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
