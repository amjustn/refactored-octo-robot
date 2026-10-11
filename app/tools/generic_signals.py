"""通用财务与估值信号（本机可核算口径，2026-10-11）。

背景：板块知识库（`data/knowledge/sectors/*.json`）给的是「该盯哪些信号」的观察清单，
而清单上的信号本机大多没有数据源 —— 实测电子板块 4 个视角里只有「风险逆向派」落地
（它的信号恰好与本机已有的减持/公告/新闻数据重合），另三个视角（买方趋势派/深度价值派/
卖方研究派）在报告里 0 命中，因为模型手上没有清单上那些数字，又禁止凭空编造。

本模块负责把清单里「本机能核算」的通用指标算成真实数值注入，让可核算的那部分落地。
刻意不新增外部数据源，全部复用本机已有取数：

- 估值分位：PB / PE(TTM) 近 5 年历史分位            ← 百度股市通估值序列（akshare）
- 股息与利差：近 12 个月已实施现金分红 ÷ 现价 − 10 年国债收益率
                                                   ← 东财分红送配 + 中债（akshare）
- 现金流质量：经营活动现金流净额 ÷ 净利润            ← market_data.get_financial_data
- 资本开支强度：购建固定资产等支付的现金 ÷ 折旧摊销   ← 同上 + cash_flow_items（补充资料段）
- 营运资金：应收票据及应收账款 + 存货 相对营业成本的周转天数 ← 同上
- 净现金占市值：货币资金 + 交易性金融资产 − 有息负债，除以总市值 ← 同上

不覆盖（本机无数据源，禁止凭空填数）：产能利用率、行业协会销售额（SIA/WSTS/SEMI）、
渠道库存与交期、渠道调研类数据。缺失项在注入文本里显式列出，不静默。

口径提醒：
- 财报键名实测（2026-10-11，澜起 688008.SH）：现金流键是「经营活动产生的现金流量净额」，
  「经营活动产生的现金流量」恒为 nan；折旧摊销只能从现金流量表补充资料段取（cash_flow_items）。
- D&A 只有中报/年报披露 → 期间与现金流表不一致时该指标整项跳过并记 missing，不混用期间。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

from ..core.data_cache import DataCache
from . import cash_flow_items as CFI
from . import market_data as M

logger = logging.getLogger("ai_berkshire.generic_signals")

CACHE_CATEGORY = "generic_signals"
CACHE_TTL = 6 * 3600            # 6 小时：估值序列/国债/分红均为日频数据
OVERALL_TIMEOUT = 25.0          # 主链路保护：整块超时即放弃注入（不影响其他分支）
BLOCK_TITLE = "估值与现金流信号（本机实测口径）"

VALIDATION_MARK = "【验证用例】"

_cache: Optional[DataCache] = None


def _get_cache() -> DataCache:
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache


def _cached(key: str, producer):
    """读缓存；未命中则调用 producer() 并落盘。producer 必须是同步函数，且只返回
    JSON 可序列化的纯 dict/list（DataCache 落盘不做特殊编码）。"""
    cache = _get_cache()
    try:
        hit = cache.get(CACHE_CATEGORY, key, max_age_seconds=CACHE_TTL)
        if hit:
            return hit
    except Exception as e:  # noqa: BLE001 - 缓存异常绝不影响取数
        logger.debug(f"信号缓存读取失败({key}): {e}")
    val = producer()
    try:
        cache.set(CACHE_CATEGORY, key, val)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"信号缓存写入失败({key}): {e}")
    return val


# ── 单源取数（同步，交由 asyncio.to_thread 执行）──────────────────────────

def _fetch_valuation(plain_code: str, indicator: str) -> dict:
    """百度股市通估值序列 → 当前值、近 5 年分位、样本数、数据日期。"""
    def _do() -> dict:
        import akshare as ak

        df = ak.stock_zh_valuation_baidu(symbol=plain_code, indicator=indicator, period="近五年")
        rows = []
        for _, r in df.iterrows():
            v = r.get("value")
            if v is None or v != v:
                continue
            rows.append((str(r.get("date")), float(v)))
        return {"rows": rows}

    data = _cached(f"valuation:{plain_code}:{indicator}", _do)
    rows = data.get("rows") or []
    if not rows:
        return {}
    cur_date, cur = rows[-1]
    vals = [v for _, v in rows]
    below = sum(1 for v in vals if v < cur)
    return {
        "value": cur, "date": cur_date, "samples": len(vals),
        "percentile": round(below / len(vals) * 100, 1),
    }


def _fetch_market_cap(plain_code: str) -> dict:
    """百度股市通总市值序列（亿元）→ 最新值与日期。"""
    def _do() -> dict:
        import akshare as ak

        df = ak.stock_zh_valuation_baidu(symbol=plain_code, indicator="总市值", period="近一年")
        rows = []
        for _, r in df.iterrows():
            v = r.get("value")
            if v is None or v != v:
                continue
            rows.append((str(r.get("date")), float(v)))
        return {"rows": rows}

    data = _cached(f"market_cap:{plain_code}", _do)
    rows = data.get("rows") or []
    if not rows:
        return {}
    cur_date, cur = rows[-1]
    return {"value_100m": cur, "date": cur_date}


def _fetch_bond_10y() -> dict:
    """中债 10 年期国债收益率最新值（%）。"""
    def _do() -> dict:
        import akshare as ak

        start = (dt.date.today() - dt.timedelta(days=45)).strftime("%Y%m%d")
        df = ak.bond_zh_us_rate(start_date=start)
        col = "中国国债收益率10年"
        s = df[col].dropna()
        if len(s) == 0:
            return {}
        idx = s.index[-1]
        return {"value": float(s.loc[idx]), "date": str(df.loc[idx, "日期"])[:10]}

    return _cached("bond_10y", _do)


def _fetch_dividend(plain_code: str, months: int = 12) -> dict:
    """近 N 个月已实施（按除权除息日）现金分红合计，元/股。"""
    def _do() -> dict:
        import akshare as ak
        import pandas as pd

        df = ak.stock_fhps_detail_em(symbol=plain_code)
        if df is None or len(df) == 0:
            return {"items": [], "dps": 0.0}
        cut = dt.date.today() - dt.timedelta(days=int(months * 30.44))
        items = []
        for _, r in df.iterrows():
            ex = r.get("除权除息日")
            ratio = r.get("现金分红-现金分红比例")
            if ex is None or ratio is None or pd.isna(ex) or pd.isna(ratio):
                continue
            ex_date = pd.to_datetime(ex, errors="coerce")
            if pd.isna(ex_date) or ex_date.date() < cut:
                continue
            items.append({
                "period": str(r.get("报告期", ""))[:10],
                "ex_date": ex_date.strftime("%Y-%m-%d"),
                "per_10": float(ratio),
            })
        items.sort(key=lambda x: x["ex_date"])
        dps = sum(i["per_10"] for i in items) / 10.0
        return {"items": items, "dps": round(dps, 6)}

    return _cached(f"dividend:{plain_code}:{months}", _do)


# ── 指标计算（纯函数，便于自检）─────────────────────────────────────────

_PERIOD_DAYS = {"0331": 90, "0630": 181, "0930": 273, "1231": 365}


def _num(v) -> Optional[float]:
    """nan/None 统一成 None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _pick(row: dict, *keys) -> Optional[float]:
    for k in keys:
        v = _num(row.get(k))
        if v is not None:
            return v
    return None


def period_days(period: str) -> int:
    return _PERIOD_DAYS.get(str(period)[4:8], 365)


def compute_metrics(fin: dict, da: dict, price: Optional[float], market_cap_100m: Optional[float]) -> dict:
    """由财报三表 + D&A + 现价 + 市值算出通用指标。缺项即为 None，不猜。"""
    inc = (fin.get("income_statement") or [{}])[0]
    bal = (fin.get("balance_sheet") or [{}])[0]
    cf = (fin.get("cash_flow") or [{}])[0]
    period = str(inc.get("报告日", ""))
    out: dict = {"period": period, "missing": []}

    # 现金流质量
    ocf = _pick(cf, "经营活动产生的现金流量净额", "经营活动产生的现金流量")
    np_parent = _num(inc.get("归属于母公司所有者的净利润"))
    net_profit = np_parent if np_parent is not None else _pick(inc, "净利润")
    out["ocf_yi"] = None if ocf is None else round(ocf / 1e8, 2)
    out["net_profit_parent_yi"] = None if np_parent is None else round(np_parent / 1e8, 2)
    if ocf is not None and net_profit:
        out["ocf_over_np"] = round(ocf / net_profit, 2)
    else:
        out["ocf_over_np"] = None
        out["missing"].append("经营现金流/净利润")

    # 资本开支 / 折旧摊销（期间必须一致，否则整项跳过）
    capex = _pick(cf, "购建固定资产、无形资产和其他长期资产所支付的现金")
    da_total = _num(da.get("total")) if da.get("ok") else None
    da_period = str(da.get("period", "")).replace("-", "")
    out["da_period"] = da_period
    out["da_yi"] = None if da_total is None else round(da_total / 1e8, 2)
    out["capex_yi"] = None if capex is None else round(capex / 1e8, 2)
    if capex is not None and da_total and da_period == period:
        out["capex_over_da"] = round(capex / da_total, 2)
    else:
        out["capex_over_da"] = None
        if capex is not None and da_total is None:
            out["missing"].append("折旧摊销（补充资料段不可用）")
        elif capex is not None and da_period != period:
            out["missing"].append(f"资本开支/折旧（期间不一致：财报 {period} / 折旧 {da_period or '无'}）")

    # 营运资金周转天数（期末余额口径）
    cost = _pick(inc, "营业成本")
    ar = _pick(bal, "应收票据及应收账款", "应收账款")
    inv = _pick(bal, "存货")
    if cost and ar is not None and inv is not None:
        days = (ar + inv) / cost * period_days(period)
        out["wc_days"] = round(days, 1)
        out["ar_yi"] = round(ar / 1e8, 2)
        out["inv_yi"] = round(inv / 1e8, 2)
        out["cost_yi"] = round(cost / 1e8, 2)
    else:
        out["wc_days"] = None
        out["missing"].append("应收+存货周转天数")

    # 净现金占市值
    cash_like = 0.0
    for k in ("货币资金", "交易性金融资产"):
        v = _pick(bal, k)
        if v is not None:
            cash_like += v
    debt = 0.0
    for k in ("短期借款", "长期借款", "应付债券", "一年内到期的非流动负债", "租赁负债"):
        v = _pick(bal, k)
        if v is not None:
            debt += v
    out["cash_yi"] = round(cash_like / 1e8, 2)
    out["debt_yi"] = round(debt / 1e8, 2)
    out["net_cash_yi"] = round((cash_like - debt) / 1e8, 2)
    if market_cap_100m:
        out["net_cash_over_mcap_pct"] = round((cash_like - debt) / (market_cap_100m * 1e8) * 100, 2)
    else:
        out["net_cash_over_mcap_pct"] = None
        out["missing"].append("净现金/市值（市值不可得）")

    out["price"] = price
    out["market_cap_yi"] = market_cap_100m
    return out


def _fmt_yi(v: Optional[float]) -> str:
    return "数据不可得" if v is None else f"{v:,.2f} 亿元"


def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    return "数据不可得" if v is None else f"{v:.{digits}f}%"


def _fmt_num(v: Optional[float], digits: int = 2) -> str:
    return "数据不可得" if v is None else f"{v:.{digits}f}"


# ── 组装 ────────────────────────────────────────────────────────────────

async def collect(symbol_or_text: str) -> dict:
    """取数 + 计算，返回结构化结果；任何单源失败只记 missing，不抛。"""
    code, source = M.resolve_symbol(symbol_or_text or "")
    if not code:
        return {}
    plain = code.split(".")[0]
    out: dict = {"code": code, "symbol_source": source, "missing": []}

    price = name = None
    try:
        q = M.get_stock_price(code)
        if asyncio.iscoroutine(q):
            q = await q
        if isinstance(q, dict) and not q.get("error"):
            price = _num(q.get("price"))
            name = q.get("name")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用信号：现价取数失败（{code}）: {type(e).__name__}")
        out["missing"].append("现价")

    fin = {}
    try:
        fin = await M.get_financial_data(code) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用信号：财报取数失败（{code}）: {type(e).__name__}")
        out["missing"].append("财报三表")

    async def _thread(fn, *a):
        return await asyncio.to_thread(fn, *a)

    valuation: dict = {}
    for indicator, key in (("市净率", "pb"), ("市盈率(TTM)", "pe_ttm")):
        try:
            valuation[key] = await _thread(_fetch_valuation, plain, indicator)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"通用信号：{indicator} 序列取数失败（{plain}）: {type(e).__name__}")
            valuation[key] = {}
    out["valuation"] = valuation

    try:
        out["market_cap"] = await _thread(_fetch_market_cap, plain)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用信号：市值取数失败（{plain}）: {type(e).__name__}")
        out["market_cap"] = {}
    try:
        out["bond_10y"] = await _thread(_fetch_bond_10y)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用信号：10 年国债取数失败: {type(e).__name__}")
        out["bond_10y"] = {}
    try:
        out["dividend"] = await _thread(_fetch_dividend, plain)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用信号：分红取数失败（{plain}）: {type(e).__name__}")
        out["dividend"] = {"items": [], "dps": 0.0}
    try:
        da = await _thread(CFI.get_depreciation_amortization, code)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用信号：折旧摊销取数失败（{code}）: {type(e).__name__}")
        da = {}
    out["da"] = da or {}

    mcap = (out.get("market_cap") or {}).get("value_100m")
    metrics = compute_metrics(fin, out["da"], price, mcap)
    out.update({"name": name, **metrics})
    for item in metrics.get("missing", []):
        if item not in out["missing"]:
            out["missing"].append(item)
    return out


def format_block(data: dict) -> str:
    """把结构化信号格式化为注入文本；数据不足时返回空串（宁可不注入，也不给空架子）。"""
    if not data or not data.get("code"):
        return ""
    name = data.get("name") or data["code"]
    valuation = data.get("valuation") or {}
    pb, pe = valuation.get("pb") or {}, valuation.get("pe_ttm") or {}
    bond = data.get("bond_10y") or {}
    div = data.get("dividend") or {}
    dps = div.get("dps")
    price = data.get("price")

    lines = [f"## {BLOCK_TITLE}", ""]
    head = f"标的：{name}（{data['code']}）"
    if price:
        head += f"｜现价 {price:.2f} 元"
    if data.get("market_cap_yi"):
        head += f"｜总市值 {data['market_cap_yi']:,.2f} 亿元"
    if (data.get("market_cap") or {}).get("date"):
        head += f"（{data['market_cap']['date']}）"
    lines.append(head)

    if pb or pe:
        seg = []
        if pb:
            seg.append(f"PB {pb['value']:.2f} → 分位 {pb['percentile']:.1f}%")
        if pe:
            seg.append(f"PE(TTM) {pe['value']:.2f} → 分位 {pe['percentile']:.1f}%")
        sample = (pb or pe).get("samples", 0)
        date = (pb or pe).get("date", "")
        lines.append(
            f"- 估值分位（近 5 年 {sample} 个交易日，截至 {date}，来源：百度股市通估值序列）：" + "；".join(seg)
        )
    else:
        lines.append("- 估值分位：数据不可得（估值序列源不可用）")

    if dps is not None and price and bond.get("value") is not None:
        dy = dps / price * 100
        spread = dy - bond["value"]
        ex_dates = "、".join(i["ex_date"] for i in (div.get("items") or [])) or "无"
        lines.append(
            f"- 股息与利差：近 12 个月已实施现金分红 {dps:.4f} 元/股（除权除息日 {ex_dates}，"
            f"来源：东方财富分红送配）→ 股息率 {dy:.3f}%；10 年期国债收益率 {bond['value']:.4f}%"
            f"（{bond.get('date', '')}，来源：中债）→ 利差 {spread:+.3f} 个百分点"
        )
    else:
        lines.append("- 股息与利差：数据不可得（分红或国债收益率源不可用）")

    lines.append(
        f"- 现金流质量（报告期 {data.get('period', '')}，未年化）：经营活动现金流净额 "
        f"{_fmt_yi(data.get('ocf_yi'))} ÷ 净利润 {_fmt_yi(data.get('net_profit_parent_yi'))}"
        f"（归母口径）→ {_fmt_num(data.get('ocf_over_np'))}"
    )
    lines.append(
        f"- 资本开支强度（报告期 {data.get('period', '')}）：购建固定资产等支付的现金 "
        f"{_fmt_yi(data.get('capex_yi'))} ÷ 折旧摊销 {_fmt_yi(data.get('da_yi'))}"
        f"（{data.get('da_period', '') or '无'}，来源：现金流量表补充资料段）→ {_fmt_num(data.get('capex_over_da'))}"
    )
    if data.get("wc_days") is not None:
        lines.append(
            f"- 营运资金（{data.get('period', '')}，期末余额口径）：(应收票据及应收账款 {data.get('ar_yi')} 亿 + "
            f"存货 {data.get('inv_yi')} 亿) ÷ 营业成本 {data.get('cost_yi')} 亿 × "
            f"{period_days(data.get('period', ''))} 天 = {data['wc_days']:.1f} 天"
        )
    else:
        lines.append("- 营运资金周转天数：数据不可得（营业成本或应收/存货科目缺失）")

    lines.append(
        f"- 净现金占市值：货币资金与交易性金融资产合计 {_fmt_yi(data.get('cash_yi'))} − 有息负债 "
        f"{_fmt_yi(data.get('debt_yi'))} = 净现金 {_fmt_yi(data.get('net_cash_yi'))} → 占市值 "
        f"{_fmt_pct(data.get('net_cash_over_mcap_pct'))}"
    )

    if data.get("missing"):
        lines.append(f"- 本次未取到：{'、'.join(data['missing'])}（源不可用，请如实写\"数据不可得\"）")
    lines.append(
        "口径说明：以上均为本机可核算指标，可直接引用并注明来源；行业景气类信号"
        "（产能利用率、协会销售额、渠道库存与交期等）本机无数据源，不得凭空填数。"
    )
    return "\n".join(lines) + "\n"


async def build_signal_context(symbol_or_text: str) -> str:
    """注入入口：解析标的 → 取数 → 格式化。任何异常都返回空串且不抛。"""
    try:
        data = await asyncio.wait_for(collect(symbol_or_text), timeout=OVERALL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"通用信号：整体超时（>{OVERALL_TIMEOUT}s），本次不注入")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用信号：构建失败，本次不注入: {type(e).__name__}: {str(e)[:120]}")
        return ""
    block = format_block(data)
    if block:
        logger.info(f"通用信号注入: 标的='{symbol_or_text}' 代码={data.get('code')} 注入 {len(block)} 字符")
    else:
        logger.info(f"通用信号注入: 标的='{symbol_or_text}' 未识别到 A 股代码或数据不足，零注入")
    return block
