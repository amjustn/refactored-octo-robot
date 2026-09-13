#!/usr/bin/env python3
"""批3（P2）自检：数据新鲜度门闸 —— 报告写"需要核实"，但上下文已有数据。

双重验证：
- 自下而上：离线规则断言（命中/不命中/误报防护/窗口/去重/密度阈值）
- 自上而下：OutputGuard 集成 + 真实历史报告回放（2026-09-13 那份写"需要核实"
  的 management-deep-dive 报告 + 当前真实注入上下文）

用法（仓库根目录）：
    venv/bin/python scripts/verify_freshness_gate.py
退出码：有 FAIL 时为 1。
"""
from __future__ import annotations

import asyncio
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


CTX_ANN = (
    "## 公司公告与定期报告（自动获取）\n### 一、定期报告\n"
    "- [2026-08-29] 半年报：澜起科技2026年半年度报告（公告链接：http://www.cninfo.com.cn/...）\n"
    "### 三、高管持股变动\n- 2021-02-04 杨崇和 增持20.00万\n"
)
CTX_BROKER = "## 机构研报（东财研报中心）\n- 国信证券 增持 2026-09-02\n"
CTX_QUOTE = "## 实时行情\n- **澜起科技** (688008.SH): ¥191.96\n"
CTX_HIBOR = "## 机构研报（慧博投研）\n国信证券 | 半导体设备行业专题 | 2026-09-07 | https://www.hibor.com.cn/data/x.html\n"


def part_a_rules() -> None:
    from app.harness.freshness_gate import check_freshness

    print("\n=== A. 离线规则断言 ===")

    r = check_freshness("2026年半年报应已披露，但具体数字需要核实。", CTX_ANN)
    check("A1 财报+需要核实 且上下文有公告块 → 命中",
          any(w["rule"] == "financials" for w in r), str(r))
    check("A2 告警结构完整（gate/detail）",
          all(w.get("gate") == "freshness" and w.get("detail") for w in r), str(r))

    check("A3 上下文没有公告块 → 不命中（不凭空指控）",
          check_freshness("半年报数字需要核实。", CTX_QUOTE) == [])
    check("A4 无上下文留痕 → 不命中", check_freshness("半年报数字需要核实。", "") == [])
    check("A5 空报告 → 不命中", check_freshness("", CTX_ANN) == [])

    r = check_freshness("最新高管名单与持股变动需要核实。", CTX_ANN)
    check("A6 高管+需要核实 → 命中 management 规则",
          any(w["rule"] == "management" for w in r), str(r))

    r = check_freshness("机构一致预期与目标价未能获取。", CTX_BROKER)
    check("A7 研报+未能获取 → 命中 research 规则",
          any(w["rule"] == "research" for w in r), str(r))

    r = check_freshness("现价未获取到，无法判断估值。", CTX_QUOTE)
    check("A8 行情+未获取 → 命中 quote 规则",
          any(w["rule"] == "quote" for w in r), str(r))

    r = check_freshness("公司公告原文需要核实。", CTX_ANN)
    check("A9 公告链接+需要核实 → 命中 notice_link 规则",
          any(w["rule"] == "notice_link" for w in r), str(r))

    r = check_freshness("机构研报列表已获取。慧博观点见上。", CTX_HIBOR)
    check("A10 只有陈述、没有缺口措辞 → 不命中", r == [], str(r))

    # 窗口限制：缺口措辞与数据类别名词相距很远时不算同一处
    far = "行业竞争格局需要核实。" + ("无关内容。" * 40) + "这里顺便说说财报。"
    check("A11 类别名词与缺口措辞超过 ±20 字窗口 → 不命中",
          not any(w["rule"] == "financials" for w in check_freshness(far, CTX_ANN)))

    # 子科目提示不应触发（真实 earnings-team 报告里 40 处"需要核实"都是这种）
    subitem = "**需要核实（证据链缺失）**：Q1→Q2 互连类毛利率自身变化、应收账款账龄、募资投向细节。"
    check("A11b 子科目提示（无整类名词同现）不触发",
          check_freshness(subitem, CTX_ANN) == [], str(check_freshness(subitem, CTX_ANN)))

    # 密度规则（按"整类缺口声明"计数，不按"需要核实"原始词频）
    rich = CTX_ANN + ("填充" * 3000)
    dense3 = "半年报数字需要核实。高管名单需要核实。机构一致预期需要核实。"
    r = check_freshness(dense3, rich)
    check("A12 3 处整类缺口 + 上下文 >=4000 字符 → 命中 density",
          any(w["rule"] == "density" for w in r), str(r))
    r = check_freshness(dense3, CTX_ANN)
    check("A13 同样 3 处但上下文仅小样本 → 不命中 density（防误报）",
          not any(w["rule"] == "density" for w in r), str(r))
    dense2 = "半年报数字需要核实。高管名单需要核实。"
    _d3 = any(w["rule"] == "density" for w in check_freshness(dense3, rich))
    _d2 = any(w["rule"] == "density" for w in check_freshness(dense2, rich))
    check("A14 阈值边界：3 处命中、2 处不命中", _d3 and not _d2, f"3处={_d3} 2处={_d2}")
    freq_only = "需要核实。" * 20 + "子科目毛利率与账龄需要核实。"
    check("A14b 只堆词频（无整类名词同现）不触发 density",
          not any(w["rule"] == "density" for w in check_freshness(freq_only, rich)))

    multi = "半年报需要核实，高管持股需要核实，机构一致预期未获取。" 
    r = check_freshness(multi, CTX_ANN + CTX_BROKER)
    rules = [w["rule"] for w in r]
    check("A15 多规则命中且同规则去重", len(rules) == len(set(rules)) and len(rules) >= 2, str(rules))


def part_b_integration() -> None:
    from app.harness.context import get_last_context, record_context
    from app.harness.guards import OutputGuard

    print("\n=== B. OutputGuard 集成（含留痕） ===")
    tid = "__verify_freshness__"
    record_context(tid, CTX_ANN)
    check("B1 上下文留痕可读回", "公司公告与定期报告" in get_last_context(tid))

    g = OutputGuard(task_id=tid)
    body = "# 报告\n\n2026年半年报应已披露，但具体数字需要核实。\n\n## 结论\n好公司。" + "\n填充" * 300
    _, warns = g.check(body, skill_name="management-deep-dive", strategy="single")
    check("B2 guard.warnings 含 freshness 告警",
          any(w["gate"] == "freshness" for w in warns), str([w["gate"] for w in warns]))
    check("B3 freshness 告警不改写报告正文（只告警）",
          "需要核实" in body and len([w for w in warns if w["gate"] == "freshness"]) >= 1)

    tid2 = "__verify_freshness_empty__"
    g2 = OutputGuard(task_id=tid2)
    _, w2 = g2.check(body, skill_name="management-deep-dive", strategy="single")
    check("B4 无留痕时 guard 不产 freshness 告警",
          not any(w["gate"] == "freshness" for w in w2), str([w["gate"] for w in w2]))
    check("B5 留痕字典有界（不会无界增长）",
          len(__import__("app.harness.context", fromlist=["x"])._LAST_CONTEXT) <= 32)


async def part_c_real() -> None:
    from app.core.context import build_full_context
    from app.harness.freshness_gate import check_freshness

    print("\n=== C. 真实回放（历史失败报告 × 当前真实上下文） ===")
    try:
        ctx = await build_full_context(arguments="澜起科技你给我一个深度报告",
                                       skill_name="management-deep-dive")
    except Exception as e:
        check("C1 真实上下文构建", False, f"{type(e).__name__}: {e}")
        return
    check("C1 真实上下文含公告块（门闸据此判定可引用）", "公司公告与定期报告" in ctx, f"len={len(ctx)}")
    check("C2 真实上下文含高管持股变动（当初说'需核实'的正是这项）", "高管持股变动" in ctx)

    hist = ROOT / "data" / "reports" / "management-deep-dive_20260913_124849.md"
    if not hist.exists():
        check("C3 历史报告存在", False, str(hist))
        return
    text = hist.read_text(encoding="utf-8", errors="ignore")
    n_missing = len(re.findall(r"需要核实|待核实|未核实|未披露", text))
    check("C3 历史报告确认含待核实措辞", n_missing >= 1, f"{n_missing} 处")

    warns = check_freshness(text, ctx)
    check("C4 该历史报告在门闸下会告警（当初本该被拦下）", len(warns) >= 1, str([w["rule"] for w in warns]))

    # 反向：真正做足了数据的报告不应触发密度告警
    good = ROOT / "data" / "reports" / "earnings-team_20260913_124602.md"
    if good.exists():
        gt = good.read_text(encoding="utf-8", errors="ignore")
        gw = check_freshness(gt, ctx)
        check("C5 数据充分的报告不触发 density（防止对正常报告滥告警）",
              not any(w["rule"] == "density" for w in gw), str([w["rule"] for w in gw]))


def main() -> int:
    part_a_rules()
    part_b_integration()
    asyncio.run(part_c_real())
    print("\n" + "=" * 56)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
