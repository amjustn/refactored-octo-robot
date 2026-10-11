#!/usr/bin/env python
"""自检：行业景气信号（电子试点，app/tools/industry_signals.py）。

三段：
  解析层（离线夹具）—— SIA 新闻稿与 TrendForce 表格的抽取口径，不联网
  注入与接线层     —— 零注入条件、预算档、分类归属、白名单
  真实抓取层（联网）—— 真抓两个源，并做**时点校验**（防抓到旧稿）

用法：
  cd /home/q/ai_berkshire_web && venv/bin/python scripts/verify_industry_signals.py
  ... --base http://127.0.0.1:8001    # 追加真实链路 API 段（需服务已重启加载新代码）

判据：全部 PASS 且退出码 0。断言只按真实关系收紧，不许为过检放宽或删除。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


FIX = ROOT / "scripts/fixtures"


def section_parse() -> None:
    from app.tools.industry_signals import _fmt_amount, parse_dram_spot, parse_sia

    print("\n[1] 解析层（离线夹具）")
    sia = parse_sia((FIX / "sia_news_fixture.html").read_text(encoding="utf-8"))
    check("SIA 抽到金额（$159.7B → 1,597 亿美元）", sia.get("amount_100m") == 1597.0, str(sia.get("amount_100m")))
    check("SIA 抽到月份（2026-08）", sia.get("month") == "2026-08", str(sia.get("month")))
    check("SIA 环比 +8.0%", sia.get("mom_pct") == 8.0)
    check("SIA 环比对照月带年份（2026-07）", sia.get("prev_month") == "2026-07", str(sia.get("prev_month")))
    check("SIA 同比 +144.3%", sia.get("yoy_pct") == 144.3)
    check("SIA 同比对照月带年份（2025-08）", sia.get("yoy_month") == "2025-08", str(sia.get("yoy_month")))
    check("SIA 发布日（Oct. 5, 2026）", sia.get("published") == "Oct. 5, 2026", str(sia.get("published")))
    check("SIA 空文本 → 不猜（空 dict）", parse_sia("") == {})
    check("SIA 摘要句不含年份 → 不误抽", parse_sia("sales were $1 billion during August") == {} or
          parse_sia("sales were $1 billion during August").get("month") is None)

    dram = parse_dram_spot((FIX / "trendforce_dram_fixture.html").read_text(encoding="utf-8"))
    check("DRAM 抽到盘平均（58.90）", dram.get("session_avg") == 58.9, str(dram.get("session_avg")))
    check("DRAM 抽到日高/日低（70.00 / 44.00）", dram.get("day_high") == 70.0 and dram.get("day_low") == 44.0)
    check("DRAM 抽到涨跌幅（0.40）", dram.get("change_pct") == 0.4)
    check("DRAM 方向=涨（认 &#9650; 实体，不认 fall-trend 类名）", dram.get("direction") == "涨", str(dram.get("direction")))
    check("DRAM 空文本 → 不猜（空 dict）", parse_dram_spot("<table></table>") == {})

    check("金额格式：整数不带小数", _fmt_amount(1597.0) == "1,597")
    check("金额格式：非整数保留一位", _fmt_amount(1597.25) == "1,597.2" or _fmt_amount(1597.25) == "1,597.3")
    check("金额格式：缺失返回破折号", _fmt_amount(None) == "—")


def section_wiring() -> None:
    from app.core.context import SECTOR_KB_SKILLS
    from app.harness.context import BUDGET, _categorize
    from app.tools.industry_signals import format_electronics_block

    print("\n[2] 注入与接线层")
    check("预算档含 industry 且 >= 1000", BUDGET.get("industry", 0) >= 1000, str(BUDGET.get("industry")))
    check("行业景气块归为 industry 类（不被 financials 吞）",
          _categorize("行业景气信号（电子·试点）", "1,597 亿美元；占比 8.0%") == "industry")
    check("估值信号块仍归 signals", _categorize("估值与现金流信号（本机实测口径）", "PE(TTM) 78.46") == "signals")

    ctx_src = (ROOT / "app/core/context.py").read_text(encoding="utf-8")
    check("core/context.py 已接线行业景气分支", "_fetch_industry_signal_ctx" in ctx_src
          and "build_industry_signal_context(sector_det.get" in ctx_src)
    check("板块识别提前到 gather 前（两分支共用一次识别）",
          ctx_src.find("sector_det: dict = {}") < ctx_src.find("await asyncio.gather("))
    check("结果已拼进上下文 parts", "if industry_ctx:" in ctx_src)
    check("白名单仍排除日报/组合审视", not ({"daily-briefing", "portfolio-review"} & SECTOR_KB_SKILLS))

    empty_block = format_electronics_block({}, {})
    check("两源都不可得 → 不注入空架子", empty_block == "")
    fallback = format_electronics_block({}, {"session_avg": 58.9, "change_pct": 0.4, "direction": "涨"})
    check("单源缺失 → 另一源照给 + 缺失写明「数据不可得」", "数据不可得" in fallback and "58.90" in fallback)
    check("块内含覆盖说明（不得凭空填数）", "不得凭空填数" in fallback)


def section_commodity() -> None:
    """商品价格（现货价）段：离线夹具断言，不联网。"""
    import json as _json

    from app.tools.industry_signals import (
        COMMODITY_EXTRA, COMMODITY_MAP, COMMODITY_UNITS, format_commodity_block,
    )

    print("\n[2b] 商品价格（离线夹具）")
    fix = _json.loads((FIX / "commodity_spot_fixture.json").read_text(encoding="utf-8"))
    check("现货价夹具含两个交易日", fix["date"] == "2026-10-09" and fix["prev_date"] == "2026-10-08")
    check("映射覆盖板块数 ≥ 10", len(COMMODITY_MAP) >= 10, f"{len(COMMODITY_MAP)} 个板块")
    check("黄金/白银单位单独标注（元/克、元/千克）",
          COMMODITY_UNITS.get("AU") == "元/克" and COMMODITY_UNITS.get("AG") == "元/千克")

    blk = format_commodity_block(["youse"], fix, {})
    check("单板块只输出该板块", "有色金属" in blk and "农林牧渔" not in blk and "钢铁" not in blk)
    check("数字带单位（元/吨）", "元/吨" in blk)
    cu_now, cu_prev = fix["prices"]["CU"], fix["prev_prices"]["CU"]
    exp_chg = f"{(cu_now / cu_prev - 1) * 100:+.2f}%"
    check("涨跌按两日现货价实算", exp_chg in blk, f"期望 {exp_chg}")
    check("块内含数据日期与来源", fix["date"] in blk and "akshare 现货价格接口" in blk)
    check("块内含覆盖说明（不得凭空填数）", "不得凭空填数" in blk)

    blk2 = format_commodity_block(["nonglin", "fangzhi"], fix, {})
    check("同一品种不重复（棉花在农业与纺织各映射一次，只出现一次）", blk2.count("棉花") == 1)

    blk3 = format_commodity_block(["shiyou"], fix, {"SC0": {"label": "原油", "price": 734.1, "prev": 758.5,
                                                          "chg_pct": -3.22, "date": "2026-10-09"}})
    check("原油走主力连续分支并标注口径不同", "原油" in blk3 and "主力合约收盘价" in blk3)
    check("原油涨跌标注", "-3.22%" in blk3)

    check("无匹配品种的板块 → 空串", format_commodity_block(["yinhang"], fix, {}) == "")
    check("夹具为空但原油可得 → 仍输出原油行", format_commodity_block(
        ["shiyou"], {}, {"SC0": {"label": "原油", "price": 734.1, "prev": 758.5, "chg_pct": -3.22, "date": "x"}}
    ) != "")
    check("期货品种不在映射内不会被带出（如 A 豆一、JD 鸡蛋）",
          "豆一" not in blk and "鸡蛋" not in blk)
    check("板块中文名取自注册表（非英文 key）",
          "钢铁" in format_commodity_block(["gangtie"], fix, {})
          and "gangtie" not in format_commodity_block(["gangtie"], fix, {}))
    check("商品映射不与电子重复（dianzi 不走商品分支）", "dianzi" not in COMMODITY_MAP and "dianzi" not in COMMODITY_EXTRA)


async def section_live() -> None:
    from app.harness.context import BUDGET, count_tokens
    from app.tools import industry_signals as I

    print("\n[3] 真实抓取层（联网）")
    sia, dram = await I._gather_signals()
    check("SIA 抓到金额", bool(sia.get("amount_100m")), f"{sia.get('amount_100m')} 亿美元")
    check("SIA 带来源 URL 与抓取时点", "semiconductors.org" in str(sia.get("url", "")) and bool(sia.get("fetched_at")))
    # 时点校验：月份必须落在近 4 个月内，否则视为抓到旧稿（宁可判失败也不写错数）
    month = str(sia.get("month") or "")
    ok_time = False
    if len(month) == 7:
        try:
            y, m = int(month[:4]), int(month[5:7])
            today = dt.date.today()
            delta = (today.year - y) * 12 + (today.month - m)
            ok_time = 0 <= delta <= 4
        except ValueError:
            ok_time = False
    check("SIA 数据时点在近 4 个月内（防抓到旧稿）", ok_time, f"月={month} 今天={dt.date.today()}")

    check("DRAM 抓到盘平均", dram.get("session_avg") is not None, str(dram.get("session_avg")))
    check("DRAM 均价落在合理区间（10~500 美元）", 10 <= float(dram.get("session_avg") or 0) <= 500)
    check("DRAM 方向已判定（涨/跌/—）", dram.get("direction") in {"涨", "跌", "—"}, str(dram.get("direction")))
    check("DRAM 带来源 URL", "trendforce" in str(dram.get("url", "")))

    block = await I.build_industry_signal_context(["dianzi"])
    check("电子板块 → 注入块非空", len(block) > 300, f"{len(block)} 字符")
    check("块内含 SIA 与 DRAM 两行", "全球半导体销售额" in block and "DRAM 现货价" in block)
    check("块 token 在预算内（industry 档）", count_tokens(block) < BUDGET.get("industry", 0),
          f"{count_tokens(block)} < {BUDGET.get('industry')}")
    check("无商品映射的板块 → 零注入（银行）", await I.build_industry_signal_context(["yinhang"]) == "")
    check("空板块列表 → 零注入", await I.build_industry_signal_context([]) == "")

    # ── 商品价格（现货价）真实抓取 ──
    snap, extra = await I._gather_commodity()
    prices = snap.get("prices") or {}
    check("现货价快照品种数 ≥ 40", len(prices) >= 40, f"{len(prices)} 个品种")
    ok_date = False
    try:
        ok_date = (dt.date.today() - dt.date.fromisoformat(str(snap.get("date")))).days <= 7
    except ValueError:
        ok_date = False
    check("现货价日期在近 7 天内", ok_date, str(snap.get("date")))
    check("原油（主力连续）可得", bool(extra.get("SC0")), str((extra.get("SC0") or {}).get("price")))
    blk_c = await I.build_industry_signal_context(["youse"])
    check("有色板块 → 商品块含金属价", "有色金属" in blk_c and "元/吨" in blk_c, f"{len(blk_c)} 字符")
    check("非商品板块（计算机）不产出商品块", "商品价格" not in await I.build_industry_signal_context(["jisuanji"]))
    both = await I.build_industry_signal_context(["dianzi", "youse"])
    check("电子 + 有色 → 两块同现", "SIA/WSTS" in both and "有色金属" in both, f"{len(both)} 字符")
    check("最大组合 token 在预算内", count_tokens(both) < BUDGET.get("industry", 0),
          f"{count_tokens(both)} < {BUDGET.get('industry')}")
    check("映射里每个板块都能取到中文名", all(I._sector_cn_name(k) != k for k in I.COMMODITY_MAP))


def section_api(base: str) -> None:
    print(f"\n[4] 真实链路 API 段（{base}）")
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    req = urllib.request.Request(
        f"{base}/api/auth/login", data=json.dumps({"token": env.get("BERKSHIRE_API_TOKEN", "")}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        jwt = json.load(resp)["access_token"]

    def ctx(skill: str, args: str) -> str:
        q = urllib.parse.urlencode({"arguments": args, "skill_name": skill})
        r = urllib.request.Request(f"{base}/api/data/context?{q}", headers={"Authorization": f"Bearer {jwt}"})
        with urllib.request.urlopen(r, timeout=90) as resp:
            return json.load(resp)["context"]

    c = ctx("investment-team", "澜起科技")
    check("真实链路：电子标的上下文含行业景气块", "行业景气信号（电子·试点）" in c)
    check("真实链路：与板块知识库/估值信号块同现",
          "板块观察知识库" in c and "估值与现金流信号" in c)
    c2 = ctx("investment-team", "贵州茅台")
    check("真实链路：非电子标的不注入（茅台=食品饮料）", "行业景气信号" not in c2)
    c3 = ctx("daily-briefing", "澜起科技")
    check("真实链路：白名单外技能零注入", "行业景气信号" not in c3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="")
    args = ap.parse_args()
    section_parse()
    section_wiring()
    section_commodity()
    asyncio.run(section_live())
    if args.base:
        section_api(args.base.rstrip("/"))
    print(f"\n结果：PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
