"""FCFE 输入集取数 —— 装配"股东自由现金流"各分量，喂给 three_scenario(anchor="FCFE")。

公式（权益口径，硬性口径，报告里必须按此列明细）:

    FCFE = 归母净利润 + 折旧摊销(D&A) - 资本开支(capex) - 营运资本增加(ΔNWC) + 净借款

    ΔNWC  = NWC(期末) - NWC(上年同期期末)
    NWC   = (流动资产合计 - 非经营性流动资产) - (流动负债合计 - 短期借款 - 一年内到期的非流动负债)
            非经营性流动资产 = 货币资金 + 交易性金融资产 + 买入返售 + 拆出资金 + 衍生金融资产
    净借款 = 取得借款收到的现金 - 偿还债务支付的现金

为什么这样切（口径说明，便于报告引用）：
- 用**归母**净利润（FCFE 是普通股股东口径，不含少数股东）；
- NWC 按 Damodaran 的非现金营运资本口径：剔除货币资金与各类金融性资产（理财/拆出资金/
  买入返售/衍生），有息短期负债加回（它们属于融资口径，其变动已由"净借款"单独反映，
  避免重复扣减）。实测依据：贵州茅台 2026H1 拆出资金 1410.84 亿，若不剔除会把财务公司
  吸存的波动算成营运资本占用，FCFE 直接失真；
- 净借款用现金流量表实际发生额（取得借款 - 偿还债务），不用资产负债表有息负债差额
  ——前者反映当期真实融资动作，后者含汇率折算与重分类噪声。

期间口径（basis）:
  "ttm"    = 最近年报 + 最近半年报 - 上年同期半年报（滚动 12 个月，缺任一期则回退 annual）
  "annual" = 最近年报（单期 12 个月）
  "h1"     = 最近半年报（单期 6 个月，**不是 12 个月**，仅作参考，输出会带 warning）

字段来源（东财三张表，英文列名 —— 2026-09-13 实测确认存在）:
  利润表: PARENT_NETPROFIT(归母净利润)
  现金流: FA_IR_DEPR/IA_AMORTIZE/LPE_AMORTIZE/USERIGHT_ASSET_AMORTIZE(补充资料段 D&A，
          另见 app/tools/cash_flow_items.py)、CONSTRUCT_LONG_ASSET(capex)、
          RECEIVE_LOAN_CASH(取得借款)、PAY_DEBT_CASH(偿还债务)
  资产负债: TOTAL_CURRENT_ASSETS/TOTAL_CURRENT_LIAB/MONETARYFUNDS/SHORT_LOAN/
          NONCURRENT_LIAB_1YEAR/SHARE_CAPITAL

只取数与装配，不做估值折现（折现在 financial_rigor.reverse_fcfe）。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..core.data_cache import DataCache

logger = logging.getLogger("ai_berkshire.fcfe_inputs")

CACHE_CATEGORY = "fcfe_inputs"
CACHE_TTL = 6 * 3600        # 6 小时：单日多次报告复用，新披露当天内可刷新
CACHE_SCHEMA = "v5"         # 输出结构/口径/警告项变更时必须改，否则旧缓存会返回旧结果（两次踩过）
                            # v5 = NWC 非经营性资产补 TRADE_FINASSET_NOTFVTPL（2026-09-13）

DA_FIELDS = ("FA_IR_DEPR", "IA_AMORTIZE", "LPE_AMORTIZE", "USERIGHT_ASSET_AMORTIZE")

_cache: Optional[DataCache] = None


def _get_cache() -> DataCache:
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))[:8]


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _index(rows: list) -> dict:
    """报表行按 8 位报告期索引（后出现的覆盖先出现的）。"""
    out = {}
    for r in rows or []:
        if isinstance(r, dict):
            d = _digits(r.get("报告日") or r.get("REPORT_DATE"))
            if len(d) == 8:
                out[d] = r
    return out


def _sum_fields(row: dict, fields) -> Optional[float]:
    if not row:
        return None
    vals = [_num(row.get(f)) for f in fields]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _latest_year_index(idx: dict, mmdd: str) -> Optional[str]:
    cands = sorted([d for d in idx if d[4:] == mmdd], reverse=True)
    return cands[0] if cands else None


def _prev_year(digits: str) -> str:
    return str(int(digits[:4]) - 1) + digits[4:]


# 非经营性流动资产（从营运资本里剔除）：货币资金 + 各类金融性资产
#   交易性金融资产、买入返售(BUY_RESALE_FINASSET)、
#   拆出资金(LEND_FUND)、衍生金融资产(DERIVE_FINASSET)
# 依据：Damodaran 的 non-cash working capital = (流动资产 - 现金及有价证券) -
#       (流动负债 - 短期借款)。不剔的话，理财/拆出资金/结构性存款的进出会被当成
#       营运资本占用 —— 实测贵州茅台 2026H1 拆出资金 1410.84 亿（财务公司吸存），
#       混进 ΔNWC 会让 FCFE 严重失真。
# ⚠️ 2026-09-13 修正（口径缺陷，实测踩到）：东财资产负债表对"交易性金融资产"有**两个
#    模板字段名**，视公司模板二选一，历史上从未同时非零（20 只样本实测：15 只走
#    TRADE_FINASSET_NOTFVTPL、1 只银行模板走 FVTPL_FINASSET、4 只两边都是 0）：
#      * 一般企业模板 → TRADE_FINASSET_NOTFVTPL
#      * 金融业模板   → FVTPL_FINASSET
#    原实现只列了 FVTPL_FINASSET ⇒ 一般企业（澜起/宁德/神华/比亚迪/美的…）的**理财
#    被当成经营性营运资本**。澜起 2026H1 交易性金融资产 29.17 亿（上年同期 6.67 亿），
#    22.50 亿的理财净买入被算成营运资本占用 → TTM FCFE 被低估到 4.16 亿（真实 26.66 亿）。
#    两个字段名都列，实测不重叠所以直接求和不会双计；未观测到取值的 TRADE_FINASSET
#    别名不纳入（宁可少剔也不冒双计风险）。
NONOP_CURRENT_ASSETS = (
    "MONETARYFUNDS", "FVTPL_FINASSET", "TRADE_FINASSET_NOTFVTPL",
    "BUY_RESALE_FINASSET", "LEND_FUND", "DERIVE_FINASSET",
)


def nwc_swing_warning(delta_nwc: float, net_profit_parent: Optional[float]) -> Optional[str]:
    """单期 FCFE 被营运资本摆动主导时的提示（纯函数，便于单测）。

    ΔNWC 绝对值超过归母净利润的一半 → 说明这一期的 FCFE 主要不是经营赚来的，
    而是被应收/存货/其他流动资产的进出推着走，单期折现没有意义。
    """
    if not net_profit_parent:
        return None
    if abs(delta_nwc) > abs(net_profit_parent) * 0.5:
        return (
            f"营运资本变动 {delta_nwc / 1e8:.2f} 亿，超过归母净利润的一半 —— FCFE 单期被"
            "营运资本摆动主导（应收/存货/其他流动资产扩张或收缩），建议结合 3 年趋势判断，"
            "或对 FCFE 做正常化后再折现"
        )
    return None


def nwc_from_row(row: dict) -> Optional[float]:
    """营运资本（非现金口径）：
    (流动资产 - 非经营性流动资产) - (流动负债 - 短期借款 - 一年内到期非流动负债)。
    """
    if not row:
        return None
    ca, cl = _num(row.get("TOTAL_CURRENT_ASSETS")), _num(row.get("TOTAL_CURRENT_LIAB"))
    if ca is None or cl is None:
        return None
    nonop = sum((_num(row.get(f)) or 0.0) for f in NONOP_CURRENT_ASSETS)
    short_loan = _num(row.get("SHORT_LOAN")) or 0.0
    cur_1y = _num(row.get("NONCURRENT_LIAB_1YEAR")) or 0.0
    return (ca - nonop) - (cl - short_loan - cur_1y)


def resolve_periods(cf_idx: dict, basis: str = "ttm") -> tuple:
    """确定要用的期间组合。

    返回 (periods, used_basis, note)，periods = [(期次, 权重), ...]；
    无法满足请求的 basis 时回退并说明（绝不静默降级）。
    """
    latest_annual = _latest_year_index(cf_idx, "1231")
    latest_h1 = _latest_year_index(cf_idx, "0630")
    want = (basis or "ttm").lower()
    if want == "h1":
        if not latest_h1:
            return [], "none", "无半年报数据"
        return [(latest_h1, 1)], "h1", "半年报单期口径（6 个月，非 12 个月）"
    if want == "annual":
        if not latest_annual:
            return [], "none", "无年报数据"
        return [(latest_annual, 1)], "annual", ""
    # ttm 优先
    if latest_annual and latest_h1 and latest_h1 > latest_annual:
        prev_h1 = _prev_year(latest_h1)
        if prev_h1 in cf_idx:
            return [(latest_annual, 1), (latest_h1, 1), (prev_h1, -1)], "ttm", ""
        note = f"缺上年同期半年报({prev_h1})，TTM 不可用，回退年报口径"
    else:
        note = "最近半年报早于最近年报，TTM 无增量，回退年报口径"
    if latest_annual:
        return [(latest_annual, 1)], "annual", note
    if latest_h1:
        return [(latest_h1, 1)], "h1", note + "；退到半年报单期口径（6 个月）"
    return [], "none", "三张表均无可用期间"


def _weighted(idx: dict, periods: list, fields) -> Optional[float]:
    """严格加权（任一期间缺该科目即失败）—— 用于利润表这类必须齐全的科目。"""
    total = 0.0
    got = False
    for period, w in periods:
        v = _sum_fields(idx.get(period), fields)
        if v is None:
            return None
        total += w * v
        got = True
    return total if got else None


def _weighted_flow(idx: dict, periods: list, fields, zero_if_all_missing: bool = False) -> tuple:
    """现金流量表"流量类"科目的加权求和：某期间缺该科目按 0 计并留痕。

    为什么不能按缺失失败（实测）：无融资动作的期间，东财现金流表里
    `RECEIVE_LOAN_CASH` / `PAY_DEBT_CASH` 就是 nan（澜起 2025H1 即如此），
    若按缺失处理，TTM 永远拼不出来。按 0 计入在含 -1 权重的期间同样成立：
    TTM = 年报 + 半年报 − 上年同期半年报，上年同期本来就没有该业务时，减 0 正确。

    zero_if_all_missing：三个期间**全部**无该科目时是否按 0 处理。
      * 净借款（取得借款/偿还债务）用 True —— 不融资的公司（实测贵州茅台三个期间
        全无该科目）净借款就是 0，整表判缺失会误杀；
      * 折旧摊销/资本开支用 False —— 这两个科目全缺说明补充资料或投资数据没取到，
        不能假设为 0（宁可失败也不编造）。

    返回 (值 或 None, 缺失期间列表)。
    """
    total = 0.0
    any_value = False
    missing = []
    for period, w in periods:
        v = _sum_fields(idx.get(period), fields)
        if v is None:
            missing.append(period)
            continue
        any_value = True
        total += w * v
    if not any_value:
        return (0.0 if zero_if_all_missing else None), missing
    return total, missing


def market_cap_shares(symbol: str) -> Optional[float]:
    """行情口径总股本 = 总市值 / 最新价（与平台其它地方用的"总市值"同口径）。

    用途：每股 FCFE 必须和折现比较的市值同口径。实测澜起科技两者一致
    （2026-06-30 资产负债表实收资本 12.2222 亿股＝东财总市值/价格），但 A+H 两地上市
    或刚完成增发的公司会出现差异（资产负债表实收资本是工商口径、可能滞后或只算 A 股），
    所以这里取行情口径优先，并在两者差异 >2% 时告警 + 同时回传两个数（不静默取一个）。

    数据源与 app/tools/market_data.py 的 _fetch_a_share_price 一致（东财现货表）；
    本函数走同步调用，只从同步路径（get_fcfe_inputs / 其 to_thread 包装）进入。
    取不到时返回 None（调用方退回资产负债表股本并留痕，不编造）。
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or len(df) == 0:
            return None
        code = re.sub(r"\D", "", str(symbol))[:6]
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        if code_col is None:
            return None
        matched = df[df[code_col].astype(str).str.contains(code, na=False)]
        if len(matched) == 0:
            return None
        row = matched.iloc[0]
        cap = _num(row.get("总市值"))
        px = _num(row.get("最新价"))
        if cap and px and cap > 0 and px > 0:
            return cap / px
    except Exception as e:  # noqa: BLE001
        logger.debug(f"行情口径股本取数失败（{symbol}）: {type(e).__name__}")
    return None


def get_fcfe_inputs(symbol: str, basis: str = "ttm", price: Optional[float] = None,
                    shares: Optional[float] = None) -> dict:
    """装配 FCFE 输入集（分量 + 每股 FCFE）。

    shares: 可选，显式指定总股本（股）。优先级：显式入参 > 行情口径(总市值/价格) >
            资产负债表实收资本。输出 `shares_source` 标明用了哪个；两种口径差异
            >2% 时给 warning 并同时回传两个数（A+H/增发会导致差异，不静默取一个）。

    返回 {ok, symbol, basis, periods, components, fcfe, shares, shares_source,
          shares_balance_sheet, shares_market_cap, fcfe_per_share, fcfe_yield_pct,
          source, note, warnings}
    ok=False 时 note 写明原因；任何分量缺失都不编造（返回 ok=False + 缺失项清单）。
    """
    from .cash_flow_items import to_em_symbol

    out = {
        "ok": False, "symbol": symbol or "", "basis": "", "periods": [],
        "components": {}, "fcfe": None, "shares": None, "fcfe_per_share": None,
        "fcfe_yield_pct": None, "source": "", "note": "", "warnings": [],
    }
    em = to_em_symbol(symbol)
    if not em:
        out["note"] = "仅支持 A 股 6 位代码（沪深京）"
        return out
    out["symbol"] = em

    cache = _get_cache()
    key = f"{CACHE_SCHEMA}|{em}|{(basis or 'ttm').lower()}|{'p' if price is not None else '-'}|{shares or '-'}"
    try:
        hit = cache.get(CACHE_CATEGORY, key, max_age_seconds=CACHE_TTL)
        if isinstance(hit, dict) and hit.get("ok"):
            return hit
    except Exception as e:  # noqa: BLE001
        logger.debug(f"FCFE 输入缓存读取失败: {e}")

    try:
        import akshare as ak
        cf = ak.stock_cash_flow_sheet_by_report_em(symbol=em)
        ps = ak.stock_profit_sheet_by_report_em(symbol=em)
        bs = ak.stock_balance_sheet_by_report_em(symbol=em)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"FCFE 取数失败（{em}）: {type(e).__name__}: {str(e)[:120]}")
        out["note"] = f"东财三表取数失败：{type(e).__name__}"
        return out
    if cf is None or ps is None or bs is None or len(cf) == 0 or len(ps) == 0 or len(bs) == 0:
        out["note"] = "东财三表返回空表"
        return out

    cf_idx, ps_idx, bs_idx = _index(cf.to_dict("records")), _index(ps.to_dict("records")), _index(bs.to_dict("records"))
    periods, used_basis, note = resolve_periods(cf_idx, basis)
    if not periods:
        out["note"] = note or "无可用期间"
        return out
    out["basis"] = used_basis
    out["periods"] = [p for p, _ in periods]
    if note:
        out["warnings"].append(note)
    if used_basis == "h1":
        out["warnings"].append(
            "当前为半年报单期口径（6 个月），不是 12 个月的 FCFE1；"
            "用于折现前须年化或改用 TTM/年报口径"
        )

    np_ = _weighted(ps_idx, periods, ("PARENT_NETPROFIT",))
    da, da_missing = _weighted_flow(cf_idx, periods, DA_FIELDS)
    capex, capex_missing = _weighted_flow(cf_idx, periods, ("CONSTRUCT_LONG_ASSET",))
    borrow, borrow_missing = _weighted_flow(cf_idx, periods, ("RECEIVE_LOAN_CASH",),
                                            zero_if_all_missing=True)
    repay, repay_missing = _weighted_flow(cf_idx, periods, ("PAY_DEBT_CASH",),
                                          zero_if_all_missing=True)
    for label, miss in (("折旧摊销", da_missing), ("资本开支", capex_missing),
                        ("取得借款", borrow_missing), ("偿还债务", repay_missing)):
        if miss:
            out["warnings"].append(
                f"{label}：{'、'.join(miss)} 该科目无数据，按 0 计入（无该业务期间属正常）"
            )
    # ΔNWC 用"最近一期"期末 vs 上年同期期末（periods 首项是年报行，不能当期末！）
    end_p = max(p for p, _ in periods)
    nwc_end = nwc_from_row(bs_idx.get(end_p))
    nwc_prev = nwc_from_row(bs_idx.get(_prev_year(end_p)))
    shares_bs = _num((bs_idx.get(end_p) or {}).get("SHARE_CAPITAL"))
    shares_mc = None if shares is not None else market_cap_shares(em)
    if shares is not None and float(shares) > 0:
        shares_used, shares_source = float(shares), "override"
    elif shares_mc:
        shares_used, shares_source = shares_mc, "market_cap"
    elif shares_bs:
        shares_used, shares_source = shares_bs, "balance_sheet"
    else:
        shares_used, shares_source = None, ""
    if shares_bs and shares_mc and abs(shares_mc - shares_bs) / shares_bs > 0.02:
        out["warnings"].append(
            f"股本口径差异 {abs(shares_mc - shares_bs) / shares_bs * 100:.1f}%：行情口径 "
            f"{shares_mc / 1e8:.4f} 亿股（总市值/价格）vs 资产负债表实收资本 "
            f"{shares_bs / 1e8:.4f} 亿股；已用{shares_source}（与总市值同口径，A+H/增发会造成差异）"
        )
    if shares is None and shares_mc is None and shares_bs is not None:
        out["warnings"].append("行情口径股本取数失败，每股 FCFE 用的是资产负债表实收资本口径")

    missing = [name for name, v in (
        ("归母净利润", np_), ("折旧摊销", da), ("资本开支", capex),
        ("取得借款", borrow), ("偿还债务", repay), ("营运资本(期末)", nwc_end),
        ("营运资本(上年同期)", nwc_prev), ("总股本", shares_used),
    ) if v is None]
    if missing:
        out["note"] = "以下分量缺失，不做任何估算：" + "、".join(missing)
        out["components"] = {"missing": missing}
        return out

    net_borrow = borrow - repay
    delta_nwc = nwc_end - nwc_prev
    fcfe = np_ + da - capex - delta_nwc + net_borrow

    out["components"] = {
        "net_profit_parent": np_,
        "depreciation_amortization": da,
        "capex": capex,
        "delta_nwc": delta_nwc,
        "net_borrowing": net_borrow,
        "nwc_end": nwc_end,
        "nwc_prev_same_period": nwc_prev,
        "periods_used": [p for p, _ in periods],
    }
    out["fcfe"] = fcfe
    out["shares"] = shares_used
    out["shares_source"] = shares_source
    out["shares_balance_sheet"] = shares_bs
    out["shares_market_cap"] = shares_mc
    out["fcfe_per_share"] = fcfe / shares_used if shares_used else None
    if price is not None and price > 0 and out["fcfe_per_share"]:
        out["fcfe_yield_pct"] = round(out["fcfe_per_share"] / price * 100, 2)
    out["source"] = "eastmoney_three_statements"
    out["ok"] = True
    if abs(delta_nwc) > abs(np_) * 0.5:
        warn = nwc_swing_warning(delta_nwc, np_)
        if warn:
            out["warnings"].append(warn)
    if fcfe <= 0:
        out["warnings"].append(
            f"FCFE 为负（{fcfe:,.0f} 元）：扩产期/周期底常见，FCFE 折现锚对其无意义"
            "（reverse_fcfe 会直接拒绝），应改用 PB/PE/PS 锚或先做正常化"
        )
    try:
        cache.set(CACHE_CATEGORY, key, out)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"FCFE 输入缓存写入失败: {e}")
    return out


async def aget_fcfe_inputs(symbol: str, basis: str = "ttm", price: Optional[float] = None,
                           shares: Optional[float] = None) -> dict:
    import asyncio
    return await asyncio.to_thread(get_fcfe_inputs, symbol, basis, price, shares)
