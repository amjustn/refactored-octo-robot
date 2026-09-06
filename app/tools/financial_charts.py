"""Financial chart series builder for research reports.

Fetches multi-period financial statement history (income / balance sheet /
cash flow) plus valuation history (PE/PB), and computes the indicator
series used by the report charts:

- revenue:      营业总收入 (accumulated, CNY 亿)
- net_profit:   TTM 净利润 (CNY 亿) — rolling four quarters
- roe:          TTM 净利润 / 归母股东权益 (percent)
- gross_margin: (营业总收入 - 营业成本) / 营业总收入 (percent)
- ocf:          经营活动现金流量净额 (accumulated, CNY 亿)
- pe / pb:      historical valuation series (for percentile bands)

A-share data comes from akshare (sina statements + baidu valuation),
US/HK from yfinance.  All heavy imports happen inside functions so the
module loads instantly.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ai_berkshire.charts")

# Max periods kept in the financial series (quarterly statements → ~5y).
MAX_FIN_PERIODS = 24
# Valuation history keeps the full series (used for percentile bands).
MAX_VAL_PERIODS = 1200


# ==================== A-share (akshare) ====================

def _parse_report_date(value) -> Optional[str]:
    """Normalize a report date like 20260630 -> '2026-06-30'."""
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _to_num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _column(df, *names):
    """First existing column from a candidate list."""
    for n in names:
        if n in df.columns:
            return n
    return None


def _build_ttm_profit(cum_rows):
    """Convert accumulated net profit rows into TTM net profit.

    cum_rows: list of (date 'YYYY-MM-DD', cum_profit) sorted ascending.
    Annual reports (12-31) are full-year; interim reports are TTM =
    cum - same period last year + last year full year.
    """
    annual = {}  # year -> full-year profit
    for d, v in cum_rows:
        if d and d.endswith("-12-31"):
            annual[d[:4]] = v
    out = []
    for i, (d, v) in enumerate(cum_rows):
        if not d or v is None:
            out.append((d, None))
            continue
        if d.endswith("-12-31"):
            out.append((d, v))
            continue
        year = d[:4]
        prev_same = None
        for dj, vj in cum_rows:
            if dj and dj.startswith(str(int(year) - 1)) and dj[5:] == d[5:]:
                prev_same = vj
                break
        prev_annual = annual.get(str(int(year) - 1))
        if prev_same is not None and prev_annual is not None:
            out.append((d, v - prev_same + prev_annual))
        else:
            out.append((d, None))
    return out


def _fetch_a_share_series(code: str) -> dict:
    """Fetch multi-period financial + valuation series for an A-share code."""
    import akshare as ak

    stock_prefix = f"sh{code}" if code.startswith("6") else f"sz{code}"

    def _fetch():
        result = {
            "revenue": [], "net_profit": [], "roe": [],
            "gross_margin": [], "ocf": [], "pe": [], "pb": [],
            "equity": [], "dates": [],
        }
        # --- income statement ---
        try:
            df = ak.stock_financial_report_sina(stock=stock_prefix, symbol="利润表")
            if df is not None and len(df):
                df = df.head(MAX_FIN_PERIODS)
                rev_col = _column(df, "营业总收入", "营业收入")
                np_col = _column(df, "净利润", "归属于母公司所有者的净利润")
                cost_col = _column(df, "营业成本")
                dates, rev, np_ = [], [], []
                for _, r in df.iterrows():
                    d = _parse_report_date(r.get("报告日"))
                    dates.append(d)
                    rev.append(_to_num(r.get(rev_col)) if rev_col else None)
                    np_.append(_to_num(r.get(np_col)) if np_col else None)
                result["dates"] = dates
                result["revenue"] = rev
                # TTM net profit
                cum = [(d, v) for d, v in zip(dates, np_) if d and v is not None]
                ttm = _build_ttm_profit(cum)
                ttm_map = {d: v for d, v in ttm}
                result["net_profit"] = [ttm_map.get(d) for d in dates]
                # gross margin
                if cost_col:
                    result["gross_margin"] = [
                        round((r - c) / r * 100, 2)
                        if r and c is not None and r else None
                        for r, c in zip(rev, [_to_num(x.get(cost_col)) for _, x in df.iterrows()])
                    ]
        except Exception as e:
            logger.debug(f"A-share income series failed for {code}: {e}")

        # --- balance sheet: equity per period ---
        try:
            dfb = ak.stock_financial_report_sina(stock=stock_prefix, symbol="资产负债表")
            if dfb is not None and len(dfb):
                dfb = dfb.head(MAX_FIN_PERIODS)
                eq_col = _column(dfb, "归属于母公司股东权益合计", "所有者权益合计")
                if eq_col:
                    eq_map = {
                        _parse_report_date(r.get("报告日")): _to_num(r.get(eq_col))
                        for _, r in dfb.iterrows()
                    }
                    result["equity"] = [eq_map.get(d) for d in result["dates"]]
                    roe_list = []
                    for np_, eq in zip(result["net_profit"], result["equity"]):
                        if np_ is not None and eq:
                            roe_list.append(round(np_ * 100 / eq, 2))
                        else:
                            roe_list.append(None)
                    result["roe"] = roe_list
        except Exception as e:
            logger.debug(f"A-share balance sheet series failed for {code}: {e}")

        # --- cash flow ---
        try:
            dfc = ak.stock_financial_report_sina(stock=stock_prefix, symbol="现金流量表")
            if dfc is not None and len(dfc):
                dfc = dfc.head(MAX_FIN_PERIODS)
                ocf_col = _column(dfc, "经营活动产生的现金流量净额", "经营活动产生的现金流量")
                if ocf_col:
                    ocf_map = {
                        _parse_report_date(r.get("报告日")): _to_num(r.get(ocf_col))
                        for _, r in dfc.iterrows()
                    }
                    result["ocf"] = [ocf_map.get(d) for d in result["dates"]]
        except Exception as e:
            logger.debug(f"A-share cash flow series failed for {code}: {e}")

        # --- valuation history (baidu) ---
        try:
            for key, indicator in (("pe", "市盈率(TTM)"), ("pb", "市净率")):
                dfv = ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period="全部")
                if dfv is not None and len(dfv):
                    dfv = dfv.tail(MAX_VAL_PERIODS)
                    result[key] = [
                        {"date": str(r.get("date")), "value": _to_num(r.get("value"))}
                        for _, r in dfv.iterrows()
                    ]
        except Exception as e:
            logger.debug(f"A-share valuation series failed for {code}: {e}")

        return result

    return _fetch()


# ==================== US/HK (yfinance) ====================

def _fetch_yf_series(symbol: str) -> dict:
    """Fetch annual financial series for US/HK via yfinance (best effort)."""
    import yfinance as yf

    def _fetch():
        result = {
            "revenue": [], "net_profit": [], "roe": [],
            "gross_margin": [], "ocf": [], "pe": [], "pb": [],
            "equity": [], "dates": [],
        }
        ticker = yf.Ticker(symbol)
        try:
            fs = ticker.financials  # rows: metrics, columns: years
            bs = ticker.balance_sheet
            cfs = ticker.cashflow
            if fs is not None and len(fs):
                dates = [d.strftime("%Y-%m-%d") for d in fs.columns]
                result["dates"] = dates[::-1]
                for key, row in (
                    ("revenue", "Total Revenue"),
                    ("net_profit", "Net Income"),
                    ("gross_margin", "Gross Profit"),
                ):
                    series = []
                    for d in dates[::-1]:
                        try:
                            v = _to_num(fs.loc[row, d])
                        except (KeyError, TypeError):
                            v = None
                        series.append(v)
                    if key == "gross_margin" and series:
                        result["gross_margin"] = [
                            round(g / r * 100, 2) if g is not None and r else None
                            for g, r in zip(series, result["revenue"])
                        ]
                    else:
                        result[key] = series
                if bs is not None and len(bs):
                    eq_series = []
                    for d in dates[::-1]:
                        try:
                            v = _to_num(bs.loc["Stockholders Equity", d])
                        except (KeyError, TypeError):
                            v = None
                        eq_series.append(v)
                    result["equity"] = eq_series
                    result["roe"] = [
                        round(np_ * 100 / eq, 2) if np_ is not None and eq else None
                        for np_, eq in zip(result["net_profit"], eq_series)
                    ]
                if cfs is not None and len(cfs):
                    result["ocf"] = []
                    for d in dates[::-1]:
                        try:
                            v = _to_num(cfs.loc["Operating Cash Flow", d])
                        except (KeyError, TypeError):
                            v = None
                        result["ocf"].append(v)
        except Exception as e:
            logger.debug(f"yf financial series failed for {symbol}: {e}")

        try:
            info = ticker.info or {}
            hist = ticker.history(period="1d")
            px = float(hist["Close"].iloc[-1]) if hist is not None and len(hist) else None
            result["pe"] = [{"date": datetime.now().strftime("%Y-%m-%d"),
                             "value": _to_num(info.get("trailingPE"))}]
            result["pb"] = [{"date": datetime.now().strftime("%Y-%m-%d"),
                             "value": _to_num(info.get("priceToBook"))}]
            result["current_price"] = px
        except Exception as e:
            logger.debug(f"yf valuation failed for {symbol}: {e}")
        return result

    return _fetch()


# ==================== Public API ====================

def _percentile_rank(values, current):
    """Percentile of `current` within historical `values` (0-100)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    below = sum(1 for v in vals if v <= current)
    return round(below / len(vals) * 100, 1)


async def build_financial_series(query: str) -> Optional[dict]:
    """Build the chart series payload for a company query (code or Chinese name).

    Returns None when the query cannot be resolved to a tradable symbol.
    """
    from .market_data import _detect_market, _resolve_company_name, search_company

    resolved = _resolve_company_name(query)
    if resolved:
        query = resolved
    market = _detect_market(query)

    if market == "A_SHARE":
        code = query.split(".")[0]
        raw = await asyncio.to_thread(_fetch_a_share_series, code)
        series = _clean_a_share(raw)
        name = _lookup_name(code)
    elif market in ("US", "HK"):
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_fetch_yf_series, query), timeout=15.0
            )
        except (asyncio.TimeoutError, Exception):
            logger.warning(f"yf series timeout for {query}")
            raw = {}
        series = _clean_yf(raw)
        name = None
    else:
        # Try name search fallback for A-share Chinese names
        results = await search_company(query)
        if results:
            sym = results[0]["symbol"]
            return await build_financial_series(sym)
        return None

    if not series or not any(series.get("dates")):
        return None

    # Percentile bands from valuation history
    pe_now = next((p["value"] for p in reversed(series.get("pe", [])) if p.get("value") is not None), None)
    pb_now = next((p["value"] for p in reversed(series.get("pb", [])) if p.get("value") is not None), None)
    percentiles = {
        "pe_now": pe_now,
        "pe_pct": _percentile_rank([p["value"] for p in series.get("pe", [])], pe_now) if pe_now else None,
        "pb_now": pb_now,
        "pb_pct": _percentile_rank([p["value"] for p in series.get("pb", [])], pb_now) if pb_now else None,
    }

    return {
        "symbol": query,
        "name": name or query,
        "market": market,
        "currency": "CNY" if market == "A_SHARE" else ("HKD" if market == "HK" else "USD"),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "series": series,
        "percentiles": percentiles,
        "units": "亿元" if market == "A_SHARE" else "原始币种",
    }


def _clean_a_share(raw: dict) -> dict:
    """Scale to 亿 and drop empty periods."""
    dates, revenue, net_profit, roe, gm, ocf = (
        raw["dates"], raw["revenue"], raw["net_profit"],
        raw["roe"], raw["gross_margin"], raw["ocf"],
    )
    out_dates, out_rev, out_np, out_roe, out_gm, out_ocf = [], [], [], [], [], []
    for i, d in enumerate(dates):
        if not d:
            continue
        out_dates.append(d)
        out_rev.append(round(revenue[i] / 1e8, 2) if revenue[i] is not None else None)
        out_np.append(round(net_profit[i] / 1e8, 2) if net_profit[i] is not None else None)
        out_roe.append(roe[i])
        out_gm.append(gm[i] if i < len(gm) else None)
        out_ocf.append(round(ocf[i] / 1e8, 2) if ocf[i] is not None else None)
    return {
        "dates": out_dates,
        "revenue": out_rev, "net_profit": out_np, "roe": out_roe,
        "gross_margin": out_gm, "ocf": out_ocf,
        "pe": raw.get("pe", []), "pb": raw.get("pb", []),
    }


def _clean_yf(raw: dict) -> dict:
    return {
        "dates": raw.get("dates", []),
        "revenue": raw.get("revenue", []),
        "net_profit": raw.get("net_profit", []),
        "roe": raw.get("roe", []),
        "gross_margin": raw.get("gross_margin", []),
        "ocf": raw.get("ocf", []),
        "pe": raw.get("pe", []), "pb": raw.get("pb", []),
    }


def _lookup_name(code: str) -> Optional[str]:
    """Best-effort A-share name lookup via akshare spot table."""
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == code]
        if len(row):
            return str(row.iloc[0]["名称"])
    except Exception:
        pass
    return None
