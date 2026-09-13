#!/usr/bin/env python3
"""D&A（折旧摊销）取数自检 —— 第 A 步：只补取数，不动估值口径。

实测结论（2026-09-13）：
- 唯一可用源 = 东财现金流接口补充资料段（英文列名 FA_IR_DEPR / IA_AMORTIZE /
  LPE_AMORTIZE / USERIGHT_ASSET_AMORTIZE）；新浪现金流量表 71 列无折旧摊销；巨潮无接口。
- ⚠️ 只有中报(06-30)/年报(12-31)有值，季报该段恒为 nan → 必须挑"最近的有值期间"。

用法: venv/bin/python scripts/verify_cash_flow_items.py
"""
from __future__ import annotations

import asyncio
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


def part_a_offline() -> None:
    from app.tools import cash_flow_items as C

    print("\n=== A. 离线断言 ===")
    check("A1 代码归一化（6位→东财前缀）", C.to_em_symbol("688008") == "SH688008")
    check("A2 后缀式→前缀式", C.to_em_symbol("688008.SH") == "SH688008" and C.to_em_symbol("002155.SZ") == "SZ002155")
    check("A3 已带前缀原样返回", C.to_em_symbol("SH600519") == "SH600519")
    check("A4 北交所代码（8 开头）", C.to_em_symbol("836826") == "BJ836826")
    check("A5 非 A 股/非法输入返回空", all(C.to_em_symbol(x) == "" for x in ("0700.HK", "AAPL", "68800", "", None)))

    check("A6 字段表覆盖 4 个可用字段", set(C.DA_FIELDS) == {
        "FA_IR_DEPR", "IA_AMORTIZE", "LPE_AMORTIZE", "USERIGHT_ASSET_AMORTIZE"})
    check("A7 披露期限制为中报/年报", C.DISCLOSED_MMDD == ("06-30", "12-31"))

    # 期间选择：季报（全 nan）必须被跳过
    rows = [
        {"报告日": "20260930", "FA_IR_DEPR": float("nan")},           # 三季报：无值
        {"报告日": "20260630", "FA_IR_DEPR": 47_391_994.43, "IA_AMORTIZE": 9_090_381.52,
         "LPE_AMORTIZE": 17_237_741.98, "USERIGHT_ASSET_AMORTIZE": 7_153_751.89},
        {"报告日": "20260331", "FA_IR_DEPR": float("nan")},           # 一季报：无值
        {"报告日": "20251231", "FA_IR_DEPR": 89_457_064.37},
    ]
    picked = C.pick_period(rows)
    check("A8 跳过全 nan 的季报，选中中报", picked is not None and picked["报告日"] == "20260630",
          str(picked and picked.get("报告日")))
    check("A9 disclosed_only=False 时允许选季报",
          (C.pick_period([{"报告日": "20260930", "FA_IR_DEPR": 1.0}], disclosed_only=False) or {}).get("报告日") == "20260930")
    check("A10 全无值 → None", C.pick_period([{"报告日": "20260930", "FA_IR_DEPR": float("nan")}]) is None)
    check("A11 空输入 → None（不抛异常）", C.pick_period([]) is None and C.pick_period(None) is None)
    # 防回归：东财返回的是 datetime 字符串（'2026-06-30 00:00:00'），
    # 早期实现只截前 8 位 → '2026-06-' 导致整表被跳过（实测踩过）
    dt_rows = [{"REPORT_DATE": "2026-06-30 00:00:00", "FA_IR_DEPR": 1.0},
               {"REPORT_DATE": "2026-09-30 00:00:00", "FA_IR_DEPR": float("nan")}]
    dt_pick = C.pick_period(dt_rows)
    check("A11b 认 datetime 形式的报告期", dt_pick is not None and C.summarize_da(dt_pick)["period"] == "2026-06-30",
          str(dt_pick and C.summarize_da(dt_pick).get("period")))

    s = C.summarize_da(picked)
    check("A12 合计 = 四项之和", abs(s["total"] - 80_873_869.82) < 0.01, str(s["total"]))
    check("A13 报告期格式化 YYYY-MM-DD", s["period"] == "2026-06-30", s["period"])
    check("A14 缺字段不计入（nan 被忽略）",
          abs(C.summarize_da({"报告日": "20251231", "FA_IR_DEPR": 10.0, "IA_AMORTIZE": float("nan")})["total"] - 10.0) < 1e-9)
    check("A15 全 nan 行 total 为 None",
          C.summarize_da({"报告日": "20251231", "FA_IR_DEPR": float("nan")})["total"] is None)

    out = C.get_depreciation_amortization("0700.HK")
    check("A16 港股返回 ok=False + 说明", out["ok"] is False and "仅支持 A 股" in out["note"], out["note"])

    src = (ROOT / "app" / "tools" / "cash_flow_items.py").read_text(encoding="utf-8")
    check("A17 模块不含估值计算（不导入 financial_rigor/valuation_guard）",
          "financial_rigor" not in src and "valuation_guard" not in src and "reverse_gordon" not in src)
    check("A18 缓存 TTL 为 30 天且 category 独立",
          "CACHE_TTL = 30 * 86400" in src and 'CACHE_CATEGORY = "cash_flow_items"' in src)


async def part_b_live() -> None:
    from app.tools import cash_flow_items as C

    print("\n=== B. 联网实测（三家 × 两期）===")
    expect = {
        "688008": ("2026-06-30", 80_873_869.82),
        "600519": ("2026-06-30", 1_207_390_616.22),
        "002155": ("2026-06-30", 208_268_253.45),
    }
    for sym, (period, total) in expect.items():
        r = C.get_depreciation_amortization(sym)
        print(f"      {sym}: ok={r['ok']} period={r['period']} total={r['total']} cached={r['cached']}")
        check(f"B-{sym} 取到 {period} 且合计相符（{total/1e8:.4f} 亿）",
              r["ok"] and r["period"] == period and abs((r["total"] or 0) - total) < 1.0,
              f"got={r.get('period')} / {r.get('total')}")
        check(f"B-{sym} 字段齐全（4 项）", len(r.get("items") or {}) == 4, str(list((r.get('items') or {}).keys())))

    r1 = C.get_depreciation_amortization("688008")
    r2 = C.get_depreciation_amortization("688008")
    check("B-缓存：第二次命中（cached=True）", r2["cached"] is True and r2["total"] == r1["total"])

    rr = await C.aget_depreciation_amortization("688008")
    check("B-异步包装可用", rr["ok"] and rr["total"] == r1["total"])

    # 折旧率合理性（固定资产折旧 ÷ 固定资产平均余额），与资产负债表口径自洽
    import akshare as ak
    bs = ak.stock_balance_sheet_by_report_em(symbol="SH688008")
    ra = {str(x["REPORT_DATE"])[:10]: x for x in bs.to_dict("records")}
    b25, b24 = ra.get("2025-12-31"), ra.get("2024-12-31")
    cf = ak.stock_cash_flow_sheet_by_report_em(symbol="SH688008")
    c25 = {str(x["REPORT_DATE"])[:10]: x for x in cf.to_dict("records")}.get("2025-12-31")
    if b25 and b24 and c25:
        avg_fa = (b25.get("FIXED_ASSET") + b24.get("FIXED_ASSET")) / 2
        rate = c25.get("FA_IR_DEPR") / avg_fa * 100 if avg_fa else 0
        print(f"      澜起 2025 固定资产折旧率 = {rate:.1f}%（平均固定资产 {avg_fa/1e8:.2f} 亿）")
        check("B-合理性：固定资产折旧率落在 5%~35%（数据自洽）", 5 <= rate <= 35, f"{rate:.1f}%")


def main() -> int:
    part_a_offline()
    asyncio.run(part_b_live())
    print("\n" + "=" * 56)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
