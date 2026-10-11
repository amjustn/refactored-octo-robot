#!/usr/bin/env python
"""自检：通用财务与估值信号模块（app/tools/generic_signals.py）。

分三段：
  纯函数层（离线夹具）—— 计算与格式化口径，不联网
  接线层（离线）      —— 预算档 / 分类器 / 白名单常量
  真实取数层（联网）  —— 用真实标的复算并交叉验证（独立重算，不信模块自报）

用法：
  cd /home/q/ai_berkshire_web && venv/bin/python scripts/verify_generic_signals.py
  ... --base http://127.0.0.1:8001     # 追加真实链路 API 段（需服务已重启加载新代码）

判据：全部 PASS 且退出码 0。断言只允许"按真实关系收紧"，不许为了过检放宽或删除。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


# ── 夹具：澜起科技 2026 半年报真实值（2026-10-11 取自 get_financial_data）──
FIX_FIN = {
    "income_statement": [{
        "报告日": "20260630", "营业收入": 3335497521.81, "营业成本": 1156994202.69,
        "净利润": 1941098275.16, "归属于母公司所有者的净利润": 1997374517.87,
    }],
    "balance_sheet": [{
        "货币资金": 15109529544.43, "交易性金融资产": 2917042139.08,
        "应收票据及应收账款": 974611423.98, "存货": 996353075.98,
        "长期借款": 107079875.18, "一年内到期的非流动负债": 20788994.92,
        "租赁负债": 14506393.08,
    }],
    "cash_flow": [{
        "经营活动产生的现金流量净额": 1328028043.56,
        "购建固定资产、无形资产和其他长期资产所支付的现金": 255293531.99,
    }],
}
FIX_DA = {"ok": True, "period": "2026-06-30", "total": 80873869.82}


def section_pure() -> None:
    from app.tools import generic_signals as G

    print("\n[1] 纯函数层（离线夹具）")
    check("period_days 四期映射", [G.period_days(p) for p in ("20260331", "20260630", "20260930", "20261231")] == [90, 181, 273, 365])

    check("_num 处理 nan/None/字符串/非法", (
        G._num(float("nan")) is None and G._num(None) is None
        and G._num("1.5") == 1.5 and G._num("abc") is None
    ))

    m = G.compute_metrics(FIX_FIN, FIX_DA, price=197.60, market_cap_100m=2411.78)
    # 期望值全部由夹具数字手工推算，不用模块内部结果当期望
    exp_ocf = round(1328028043.56 / 1997374517.87, 2)                    # 归母口径
    exp_capex_da = round(255293531.99 / 80873869.82, 2)
    exp_wc = round((974611423.98 + 996353075.98) / 1156994202.69 * 181, 1)
    exp_net_cash = round((15109529544.43 + 2917042139.08 - (107079875.18 + 20788994.92 + 14506393.08)) / 1e8, 2)
    exp_nc_mcap = round((15109529544.43 + 2917042139.08 - (107079875.18 + 20788994.92 + 14506393.08)) / (2411.78e8) * 100, 2)
    check("经营现金流/归母净利润", m["ocf_over_np"] == exp_ocf, f"{m['ocf_over_np']} vs {exp_ocf}")
    check("资本开支/折旧摊销", m["capex_over_da"] == exp_capex_da, f"{m['capex_over_da']} vs {exp_capex_da}")
    check("应收+存货周转天数", m["wc_days"] == exp_wc, f"{m['wc_days']} vs {exp_wc}")
    check("净现金(亿元)", m["net_cash_yi"] == exp_net_cash, f"{m['net_cash_yi']} vs {exp_net_cash}")
    check("净现金/市值", m["net_cash_over_mcap_pct"] == exp_nc_mcap, f"{m['net_cash_over_mcap_pct']} vs {exp_nc_mcap}")
    check("口径取归母而非合并净利润", m["net_profit_parent_yi"] == 19.97 and m["ocf_over_np"] == exp_ocf)

    # D&A 期间与财报不一致 → 整项跳过并留痕（不混用期间）
    m_bad = G.compute_metrics(FIX_FIN, {"ok": True, "period": "2025-12-31", "total": 1e8}, 100.0, 1000.0)
    check("期间不一致时跳过资本开支/折旧", m_bad["capex_over_da"] is None)
    check("期间不一致时记入 missing", any("期间不一致" in x for x in m_bad["missing"]))

    # 缺科目 → 指标为 None 且 missing 有记录，不填 0
    fin_empty = {"income_statement": [{"报告日": "20260630"}], "balance_sheet": [{}], "cash_flow": [{}]}
    m_none = G.compute_metrics(fin_empty, {}, None, None)
    check("缺科目时三项均为 None", (
        m_none["ocf_over_np"] is None and m_none["wc_days"] is None and m_none["net_cash_over_mcap_pct"] is None
    ))
    check("缺科目时 missing 逐条留痕", len(m_none["missing"]) >= 3, str(m_none["missing"]))

    check("空数据 format_block 返回空串", G.format_block({}) == "" and G.format_block({"code": ""}) == "")

    blk = G.format_block({
        "code": "688008.SH", "name": "澜起科技", "period": "20260630", **m,
        "valuation": {"pb": {"value": 11.21, "percentile": 80.9, "samples": 913, "date": "2026-10-10"},
                      "pe_ttm": {"value": 78.46, "percentile": 53.6, "samples": 913, "date": "2026-10-10"}},
        "bond_10y": {"value": 1.6849, "date": "2026-10-10"},
        "dividend": {"dps": 0.59, "items": [{"ex_date": "2026-07-29"}]},
        "market_cap": {"value_100m": 2411.78, "date": "2026-10-10"},
    })
    for key, label in (("估值分位", "估值分位行"), ("股息与利差", "股息利差行"), ("现金流质量", "现金流质量行"),
                       ("资本开支强度", "资本开支行"), ("营运资金", "周转天数行"), ("净现金占市值", "净现金行")):
        check(f"输出含{label}", key in blk)
    check("输出含来源标注（百度/东财/中债/补充资料）", all(s in blk for s in ("百度股市通", "东方财富", "中债", "补充资料")))
    check("输出含禁编造口径说明", "不得凭空填数" in blk)
    check("输出字符数受控（<2000）", len(blk) < 2000, str(len(blk)))

    blk_missing = G.format_block({"code": "000001.SZ", "name": "测试", "period": "20260630", **m_none,
                                 "valuation": {}, "bond_10y": {}, "dividend": {"dps": 0.0}, "market_cap": {}})
    check("缺数据写「数据不可得」而非 0", "数据不可得" in blk_missing)
    check("未取到项在块内显式列出", "本次未取到" in blk_missing)


def section_wiring() -> None:
    from app.core.context import SECTOR_KB_SKILLS
    from app.harness.context import BUDGET, _categorize

    print("\n[2] 接线层（离线）")
    check("预算档含 signals 且 >= 1000", BUDGET.get("signals", 0) >= 1000, str(BUDGET.get("signals")))
    check("信号块归为 signals 类（不被 financials 吞）",
          _categorize("估值与现金流信号（本机实测口径）", "PB 11.21 → 分位 80.9%；PE(TTM) 78.46") == "signals")
    check("真财报块仍归 financials", _categorize("📊 机构研报观点（东方财富研报中心）", "PE 相关") == "financials")
    check("白名单含投资团队/行业研究", {"investment-team", "industry-research"} <= SECTOR_KB_SKILLS)
    check("白名单仍排除日报/组合审视", not ({"daily-briefing", "portfolio-review"} & SECTOR_KB_SKILLS))


async def section_live() -> None:
    from app.harness.context import _categorize, count_tokens
    from app.tools import generic_signals as G

    print("\n[3] 真实取数层（联网）")
    t0 = time.time()
    data = await G.collect("澜起科技")
    dt1 = time.time() - t0
    check("标的解析到规范代码", data.get("code") == "688008.SH", str(data.get("code")))

    pb = (data.get("valuation") or {}).get("pb") or {}
    check("PB 分位在 0-100 且样本充足", 0 <= pb.get("percentile", -1) <= 100 and pb.get("samples", 0) >= 200,
          f"分位 {pb.get('percentile')} 样本 {pb.get('samples')}")

    # 独立复算（脚本自己直连 akshare，不与模块共享结果）
    import akshare as ak
    s = ak.stock_zh_valuation_baidu(symbol="688008", indicator="市净率", period="近五年")["value"].astype(float)
    ind_pct = round((s < s.iloc[-1]).mean() * 100, 1)
    check("PB 分位与独立复算一致", abs(ind_pct - pb.get("percentile", -99)) <= 0.1, f"模块 {pb.get('percentile')} / 独立 {ind_pct}")

    bond = ak.bond_zh_us_rate(start_date="20260901")["中国国债收益率10年"].dropna().iloc[-1]
    dps = (data.get("dividend") or {}).get("dps")
    price = data.get("price")
    if dps is not None and price and data.get("bond_10y", {}).get("value"):
        mod_spread = dps / price * 100 - data["bond_10y"]["value"]
        ind_spread = float(dps) / float(price) * 100 - float(bond)
        check("股息率-国债利差与独立复算一致", abs(mod_spread - ind_spread) <= 0.01,
              f"模块 {round(mod_spread, 3)} / 独立 {round(ind_spread, 3)}")
    else:
        check("股息率-国债利差与独立复算一致", False, "分红/现价/国债有一项缺失")

    ocf, npp = data.get("ocf_yi"), data.get("net_profit_parent_yi")
    ok_ocf = bool(
        ocf is not None and npp
        and data.get("ocf_over_np") is not None
        and abs(data["ocf_over_np"] - round(ocf / npp, 2)) <= 0.01
    )
    check("经营现金流/归母净利润可复算", ok_ocf, f"{ocf} 亿 / {npp} 亿 → {data.get('ocf_over_np')}")

    check("净现金与市值口径自洽", data.get("net_cash_over_mcap_pct") is not None
          and abs(data["net_cash_over_mcap_pct"] - data["net_cash_yi"] / data["market_cap_yi"] * 100) <= 0.05,
          f"{data.get('net_cash_yi')} 亿 / {data.get('market_cap_yi')} 亿")

    blk = G.format_block(data)
    check("真实块非空且含 6 类指标", len(blk) > 400 and all(
        k in blk for k in ("估值分位", "股息与利差", "现金流质量", "资本开支强度", "营运资金", "净现金占市值")))
    check("真实块 token 在预算内（<1200）", count_tokens(blk) < 1200, f"{count_tokens(blk)} token")
    check("真实块归类为 signals", blk.startswith("## ") and _categorize(blk.split("\n")[0].lstrip("# ").strip(), blk) == "signals")

    check("非标的文本零注入", await G.build_signal_context("最近行业趋势如何") == "")

    # 缓存行为用确定性探针验证（不靠耗时对比：缓存命中时首轮也可能极快，那会变成恒真断言）
    calls = {"n": 0}

    def _probe() -> dict:
        calls["n"] += 1
        return {"rows": [["2026-10-10", 1.0]]}

    probe_key = f"probe:verify:{int(time.time() * 1000)}"
    G._cached(probe_key, _probe)
    G._cached(probe_key, _probe)
    check("缓存命中后不再调用取数函数", calls["n"] == 1, f"producer 调用 {calls['n']} 次")
    check("缓存首轮确实调用过一次取数", calls["n"] >= 1)
    check("取数耗时可控（整块远低于主链路超时）", dt1 < G.OVERALL_TIMEOUT, f"{dt1:.2f}s < {G.OVERALL_TIMEOUT}s")


async def section_api(base: str) -> None:
    import urllib.parse
    import urllib.request

    print(f"\n[4] 真实链路 API 段（{base}）")
    env = {}
    for line in Path(__file__).resolve().parents[1].joinpath(".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    token = env.get("BERKSHIRE_API_TOKEN", "")
    req = urllib.request.Request(
        f"{base}/api/auth/login", data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        jwt = json.load(resp)["access_token"]

    def ctx(skill: str, args: str) -> str:
        q = urllib.parse.urlencode({"arguments": args, "skill_name": skill})
        r = urllib.request.Request(f"{base}/api/data/context?{q}", headers={"Authorization": f"Bearer {jwt}"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.load(resp)["context"]

    c = ctx("investment-team", "澜起科技")
    check("白名单技能上下文含信号块", "估值与现金流信号（本机实测口径）" in c)
    check("信号块与板块知识库块同现", "板块观察知识库" in c)
    c2 = ctx("daily-briefing", "澜起科技")
    check("白名单外技能零注入", "估值与现金流信号" not in c2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="运行中服务地址，给出则追加 API 段")
    args = ap.parse_args()

    section_pure()
    section_wiring()
    asyncio.run(section_live())
    if args.base:
        asyncio.run(section_api(args.base.rstrip("/")))

    print(f"\n结果：PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
