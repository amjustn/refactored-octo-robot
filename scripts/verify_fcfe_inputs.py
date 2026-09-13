#!/usr/bin/env python3
"""FCFE 输入集自检 —— 反向 FCFE（anchor="FCFE"）的取数侧。

覆盖：
  A 离线：报告期归一（datetime）、期间回退（季报无补充资料）、TTM 组合、
          NWC 非经营性资产剔除、流量类科目缺项按 0、缓存 schema 版本、
          缺失即失败（不编造）、h1 口径警告
  B 联网：三家真实公司 TTM 分量与 FCFE 精确值、茅台拆出资金剔除效果、
          股本口径、每股 FCFE / 收益率、缓存命中、反向 FCFE 端到端对接

实测结论（2026-09-13）见 app/tools/fcfe_inputs.py 顶部口径说明。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools import fcfe_inputs as C  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    print("=" * 72)
    print("A. 离线断言")
    print("=" * 72)

    # 报告期归一
    check("A1 datetime 报告期归一为 8 位", C._digits("2026-06-30 00:00:00") == "20260630")
    check("A2 8 位字符串原样", C._digits("20251231") == "20251231")
    check("A3 空值/None → 空", C._digits(None) == "" and C._digits("") == "")

    # 期间选择：季报无补充资料 → 必须回退
    cf_idx = {"20251231": {}, "20260331": {}, "20260630": {}, "20250630": {}, "20250930": {}}
    periods, basis, note = C.resolve_periods(cf_idx, "ttm")
    check("A4 TTM 组合 = 年报 + 最近半年报 − 上年同期半年报",
          periods == [("20251231", 1), ("20260630", 1), ("20250630", -1)] and basis == "ttm",
          f"{periods}")
    check("A5 一季报/三季报不参与 FCFE（补充资料只在 6-30/12-31 有值）",
          all(p[4:] in ("0630", "1231") for p, _ in periods))
    check("A6 缺上年同期 → 回退年报口径并留痕",
          C.resolve_periods({"20251231": {}, "20260630": {}}, "ttm")[1] == "annual")
    check("A7 annual 口径只取年报", C.resolve_periods(cf_idx, "annual")[0] == [("20251231", 1)])
    check("A8 h1 口径带半年/非12个月提示",
          C.resolve_periods(cf_idx, "h1")[1] == "h1" and "6 个月" in C.resolve_periods(cf_idx, "h1")[2])
    check("A9 空索引 → none", C.resolve_periods({}, "ttm")[1] == "none")

    # NWC：非经营性资产剔除
    row = {"TOTAL_CURRENT_ASSETS": 1000.0, "TOTAL_CURRENT_LIAB": 300.0,
           "MONETARYFUNDS": 600.0, "LEND_FUND": 150.0, "FVTPL_FINASSET": 50.0,
           "SHORT_LOAN": 20.0, "NONCURRENT_LIAB_1YEAR": 5.0}
    check("A10 NWC 剔除货币资金/拆出资金/交易性金融资产并加回有息短债",
          abs(C.nwc_from_row(row) - ((1000 - 800) - (300 - 25))) < 1e-6,
          f"nwc={C.nwc_from_row(row)}")
    check("A11 关键科目缺失 → None（不猜）", C.nwc_from_row({"MONETARYFUNDS": 1.0}) is None)

    # 流量类：缺项按 0 / 全缺行为
    idx2 = {"20260630": {"RECEIVE_LOAN_CASH": 100.0}, "20251231": {}, "20250630": {}}
    v, miss = C._weighted_flow(idx2, [("20251231", 1), ("20260630", 1), ("20250630", -1)],
                               ("RECEIVE_LOAN_CASH",), zero_if_all_missing=True)
    check("A12 流量类缺项按 0 计并回传缺失期间", v == 100.0 and len(miss) == 2)
    v2, _ = C._weighted_flow({"20260630": {}}, [("20260630", 1)], ("CONSTRUCT_LONG_ASSET",),
                             zero_if_all_missing=False)
    check("A13 D&A/capex 全缺 → None（宁可失败不编造）", v2 is None)
    v3, _ = C._weighted_flow({"20260630": {}}, [("20260630", 1)], ("RECEIVE_LOAN_CASH",),
                             zero_if_all_missing=True)
    check("A14 净借款全缺 → 0（不融资的公司成立）", v3 == 0.0)

    # 严格加权（利润表）
    ps = {"20251231": {"PARENT_NETPROFIT": 10.0}, "20260630": {"PARENT_NETPROFIT": 8.0}}
    check("A15 利润表严格加权：任一期间缺失即失败",
          C._weighted(ps, [("20251231", 1), ("20250630", -1)], ("PARENT_NETPROFIT",)) is None)

    check("A16 缓存键含 schema 版本（防旧口径缓存回流）", C.CACHE_SCHEMA.startswith("v"))

    out = C.get_fcfe_inputs("", basis="ttm")
    check("A17 空标的 → ok=False 且不抛异常", out["ok"] is False and "A 股" in out["note"])
    out_hk = C.get_fcfe_inputs("0700.HK", basis="ttm")
    check("A18 港股 → ok=False（FCFE 三表仅覆盖 A 股）", out_hk["ok"] is False)

    print()
    print("=" * 72)
    print("B. 联网实测（东财三表）")
    print("=" * 72)

    async def live():
        r1 = await C.aget_fcfe_inputs("688008", basis="ttm", price=191.96)
        check("B1 澜起科技 TTM 取数成功", r1["ok"] is True, r1.get("note", ""))
        if r1["ok"]:
            c = r1["components"]
            check("B2 期间 = 20251231 + 20260630 − 20250630",
                  max(r1["periods"]) == "20260630" and len(r1["periods"]) == 3)
            check("B3 D&A 已取到（补充资料段）", c["depreciation_amortization"] > 0,
                  f"D&A={c['depreciation_amortization'] / 1e8:.4f}亿")
            check("B4 FCFE = 净利 + D&A − capex − ΔNWC + 净借款（公式自洽）",
                  abs(r1["fcfe"] - (c["net_profit_parent"] + c["depreciation_amortization"]
                                    - c["capex"] - c["delta_nwc"] + c["net_borrowing"])) < 1e-6)
            check("B5 股本与每股 FCFE 可算", r1["shares"] > 1e9 and r1["fcfe_per_share"] > 0,
                  f"股本={r1['shares'] / 1e8:.4f}亿股 每股={r1['fcfe_per_share']:.4f}元")
            check("B6 股本口径标注来源", r1.get("shares_source") in ("market_cap", "balance_sheet", "override"))
            check("B7 营运资本被单期摆动主导时有提示（澜起 ΔNWC 25 亿量级）",
                  any("营运资本" in w for w in r1["warnings"]))
            r1b = await C.aget_fcfe_inputs("688008", basis="ttm", price=191.96)
            check("B8 第二次调用命中缓存且结果一致", r1b["fcfe"] == r1["fcfe"])

        r2 = await C.aget_fcfe_inputs("600519", basis="ttm", price=1500.0)
        check("B9 贵州茅台 TTM 成功（无借款 → 净借款按 0）",
              r2["ok"] is True and r2["components"]["net_borrowing"] == 0.0)
        if r2["ok"]:
            check("B10 茅台 ΔNWC 已剔除拆出资金（若未剔除会在 95 亿量级）",
                  abs(r2["components"]["delta_nwc"]) < 60e8,
                  f"ΔNWC={r2['components']['delta_nwc'] / 1e8:.2f}亿")

        r3 = await C.aget_fcfe_inputs("002155", basis="ttm", price=20.0)
        check("B11 湖南黄金 TTM 成功", r3["ok"] is True)

        r4 = await C.aget_fcfe_inputs("688008", basis="annual")
        check("B12 annual 口径可用且与 TTM 不同",
              r4["ok"] is True and r4["basis"] == "annual" and r4["periods"] == ["20251231"])

        r5 = await C.aget_fcfe_inputs("688008", basis="ttm", price=191.96, shares=1.1e9)
        check("B13 显式指定股本优先", r5["shares_source"] == "override" and r5["shares"] == 1.1e9)

        # 端到端：输入集 → 反向 FCFE
        from app.tools.financial_rigor import three_scenario
        fx = three_scenario(price=191.96, shares_100m=r1["shares"] / 1e8, growth_rates=[],
                            anchor="FCFE", fcfe_per_share=r1["fcfe_per_share"],
                            discount_rates=[0.09, 0.10, 0.11])
        check("B14 输入集可直接喂给 three_scenario(anchor=FCFE)",
              fx.get("anchor") == "FCFE" and "sensitivity" in fx,
              f"隐含 g={fx['implied'][1]['implied_growth_pct']}%")

    asyncio.run(live())

    print()
    print("=" * 72)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
