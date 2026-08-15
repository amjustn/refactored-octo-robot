"""P6: A-share factor screening engine.

Calculates key quantitative factors for value / quality / momentum / growth.
Uses baostock for data (same source as QT dashboard on port 8000).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger("ai_berkshire.tools.factor_screen")

# ── Factor definitions ──────────────────────────────────────────

@dataclass
class FactorResult:
    code: str
    name: str
    factors: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    rank: int = 0

# Core factors — inspired by 觀瀾's 440+ but focused on the most predictive ones.
# E4: every factor declared here IS computed in _calc_factors — declaring an
# unimplemented factor would silently skew the composite score.
FACTOR_DEFS = {
    # Value factors
    "pe_ratio": {"name": "市盈率", "category": "value", "lower_better": True},
    "pb_ratio": {"name": "市净率", "category": "value", "lower_better": True},
    "ps_ratio": {"name": "市销率", "category": "value", "lower_better": True},

    # Quality factors
    "roe": {"name": "ROE", "category": "quality", "lower_better": False},
    "gross_margin": {"name": "毛利率", "category": "quality", "lower_better": False},
    "net_margin": {"name": "净利率", "category": "quality", "lower_better": False},
    "debt_equity": {"name": "负债权益比", "category": "quality", "lower_better": True},
    "current_ratio": {"name": "流动比率", "category": "quality", "lower_better": False},

    # Momentum
    "ret_1m": {"name": "1月动量", "category": "momentum", "lower_better": False},
    "ret_3m": {"name": "3月动量", "category": "momentum", "lower_better": False},
    "ret_6m": {"name": "6月动量", "category": "momentum", "lower_better": False},
    "volatility": {"name": "波动率(低好)", "category": "momentum", "lower_better": True},

    # Growth
    "revenue_growth": {"name": "营收增速", "category": "growth", "lower_better": False},
    "earnings_growth": {"name": "盈利增速", "category": "growth", "lower_better": False},
}

FACTOR_CATEGORIES = ["value", "quality", "momentum", "growth"]
CATEGORY_LABELS = {
    "value": "价值因子",
    "quality": "质量因子",
    "momentum": "动量因子",
    "growth": "成长因子",
}


def _latest_completed_quarter():
    """Most recent COMPLETED fiscal quarter as (year, quarter).

    Quarterly reports land weeks after period end, so the still-open current
    quarter almost never has data — always query the last finished one
    (replaces the previously hardcoded 2025Q2).
    """
    today = date.today()
    q_current = (today.month - 1) // 3 + 1
    if q_current == 1:
        return today.year - 1, 4
    return today.year, q_current - 1


def _normalize_score(series: pd.Series, lower_better: bool) -> pd.Series:
    """Convert raw factor values to 0-100 percentile scores."""
    if series.dropna().empty:
        return pd.Series(50, index=series.index)  # neutral for missing data
    pct = series.rank(pct=True) * 100
    if lower_better:
        pct = 100 - pct
    return pct


def screen_stocks(
    stock_codes: Optional[list[str]] = None,
    categories: Optional[list[str]] = None,
    top_n: int = 20,
) -> list[FactorResult]:
    """Screen A-share stocks using factor scoring.

    If stock_codes is None, uses a predefined watchlist of major A-share stocks.
    Returns ranked list of FactorResult objects.
    """
    try:
        import baostock as bs
    except ImportError:
        logger.error("baostock not installed — cannot run factor screening")
        return _fallback_screen(stock_codes, top_n)

    if categories is None:
        categories = FACTOR_CATEGORIES

    if stock_codes is None:
        # Default watchlist: major A-share blue chips
        stock_codes = [
            "sh.600519", "sh.600036", "sh.601318", "sh.600276", "sh.600809",
            "sh.601012", "sh.600900", "sh.601888", "sh.603259", "sh.600031",
            "sz.000858", "sz.000333", "sz.002415", "sz.000651", "sz.002594",
            "sz.000001", "sz.002142", "sz.000568", "sz.000725", "sz.002475",
        ]

    results: list[FactorResult] = []

    try:
        bs.login()
        for code in stock_codes[:50]:  # Limit to prevent timeout
            try:
                factors = _calc_factors(bs, code)
                if factors:
                    name = _get_stock_name(bs, code)
                    results.append(FactorResult(code=code, name=name, factors=factors))
            except Exception as e:
                logger.debug(f"Factor calc failed for {code}: {e}")
    except Exception as e:
        logger.warning(f"baostock connection failed: {e}")
        return _fallback_screen(stock_codes, top_n)
    finally:
        try:
            bs.logout()
        except Exception:
            pass

    if not results:
        return _fallback_screen(stock_codes, top_n)

    # Compute composite score from selected categories
    for r in results:
        scores = []
        for fkey, fdef in FACTOR_DEFS.items():
            if fdef["category"] not in categories:
                continue
            if fkey in r.factors:
                scores.append(r.factors[fkey])
        r.score = sum(scores) / len(scores) if scores else 50.0

    # Rank and return top N
    results.sort(key=lambda x: x.score, reverse=True)
    for i, r in enumerate(results[:top_n]):
        r.rank = i + 1

    return results[:top_n]


def _calc_factors(bs, code: str) -> dict[str, float]:
    """Calculate all factors for one stock. Returns dict of normalized scores."""
    factors = {}
    year, quarter = _latest_completed_quarter()

    # Get financial data
    try:
        rs = bs.query_stock_basic(code=code)
        if rs.error_code != "0":
            return {}
        rs.next()
    except Exception:
        return {}

    # Profit data
    try:
        profit = bs.query_profit_data(code=code, year=year, quarter=quarter)
        if profit.error_code == "0":
            profit_df = profit.get_data()
            if not profit_df.empty:
                row = profit_df.iloc[0]
                eps = float(row.get("dilutedEPS", 0) or 0)
                roe = float(row.get("roeAvg", 0) or 0)
                factors["roe"] = roe
                factors["eps"] = eps
                factors["gross_margin"] = float(row.get("gpMargin", 0) or 0)
                factors["net_margin"] = float(row.get("npMargin", 0) or 0)
    except Exception:
        pass

    # Balance sheet
    try:
        bal = bs.query_balance_data(code=code, year=year, quarter=quarter)
        if bal.error_code == "0":
            bal_df = bal.get_data()
            if not bal_df.empty:
                row = bal_df.iloc[0]
                factors["debt_equity"] = float(row.get("debtToAssetsRatio", 50) or 50)
                factors["current_ratio"] = float(row.get("currentRatio", 1.5) or 1.5)
    except Exception:
        pass

    # Growth data
    try:
        grow = bs.query_growth_data(code=code, year=year, quarter=quarter)
        if grow.error_code == "0":
            grow_df = grow.get_data()
            if not grow_df.empty:
                row = grow_df.iloc[0]
                rg = float(row.get("YOYOperateIncome", 0) or 0)
                eg = float(row.get("YOYNetProfit", 0) or 0)
                factors["revenue_growth"] = rg
                factors["earnings_growth"] = eg
    except Exception:
        pass

    # Valuation + momentum from daily k-data (single query, ~7 months).
    # fields confirmed against baostock demo_A_stock_k_data.py.
    try:
        end_d = date.today()
        start_d = end_d - timedelta(days=220)
        rs_k = bs.query_history_k_data_plus(
            code, "date,close,peTTM,pbMRQ,psTTM",
            start_date=start_d.isoformat(), end_date=end_d.isoformat(),
            frequency="d", adjustflag="2",
        )
        rows = []
        while (rs_k.error_code == "0") and rs_k.next():
            rows.append(rs_k.get_row_data())
        if rows:
            kdf = pd.DataFrame(rows, columns=rs_k.fields)
            closes = pd.to_numeric(kdf["close"], errors="coerce").dropna()
            if not closes.empty:
                last_close = float(closes.iloc[-1])

                def _ret(calendar_days):
                    n = max(int(calendar_days * 5 / 7), 1)  # ≈ trading days
                    if len(closes) > n:
                        past = float(closes.iloc[-1 - n])
                        if past > 0:
                            return (last_close / past - 1) * 100
                    return None

                for key, days in (("ret_1m", 30), ("ret_3m", 90), ("ret_6m", 180)):
                    r = _ret(days)
                    if r is not None:
                        factors[key] = r
                if len(closes) > 20:
                    daily_rets = closes.pct_change().dropna()
                    if not daily_rets.empty:
                        factors["volatility"] = float(daily_rets.std() * (252 ** 0.5) * 100)
            for src, key in (("peTTM", "pe_ratio"), ("pbMRQ", "pb_ratio"), ("psTTM", "ps_ratio")):
                vals = pd.to_numeric(kdf[src], errors="coerce").dropna()
                if not vals.empty and float(vals.iloc[-1]) > 0:
                    factors[key] = float(vals.iloc[-1])
    except Exception as e:
        logger.debug(f"k-data factor calc failed for {code}: {e}")

    # Normalize to percentile scores
    # For percentile we'd need cross-sectional data; use absolute scale instead.
    normed = {}
    for fkey, fdef in FACTOR_DEFS.items():
        if fkey in factors:
            val = factors[fkey]
            # Simple normalization: map typical ranges to 0-100
            normed[fkey] = _normalize_single(val, fkey, fdef)

    return normed


def _normalize_single(value: float, fkey: str, fdef: dict) -> float:
    """Normalize a single factor value to a 0-100 score using typical ranges."""
    # Default: raw percentile assignment based on typical ranges
    ranges = {
        "pe_ratio": (0, 100, True),       # 0-100, lower=better
        "pb_ratio": (0, 20, True),
        "ps_ratio": (0, 20, True),
        "roe": (0, 50, False),            # 0-50%, higher=better
        "gross_margin": (0, 80, False),
        "net_margin": (0, 50, False),
        "debt_equity": (0, 200, True),
        "current_ratio": (0, 5, False),
        "ret_1m": (-30, 30, False),
        "ret_3m": (-40, 60, False),
        "ret_6m": (-50, 100, False),
        "volatility": (10, 80, True),     # annualized %, lower=better
        "revenue_growth": (-50, 100, False),
        "earnings_growth": (-50, 100, False),
    }

    rng = ranges.get(fkey, (0, 100, fdef["lower_better"]))
    lo, hi, lower_better = rng
    clamped = max(lo, min(hi, value))
    pct = (clamped - lo) / (hi - lo) * 100 if hi > lo else 50
    if lower_better:
        pct = 100 - pct
    return round(pct, 1)


def _get_stock_name(bs, code: str) -> str:
    try:
        rs = bs.query_stock_basic(code=code)
        if rs.error_code == "0":
            rs.next()
            return rs.get_row_data()[1] or code
    except Exception:
        pass
    return code


def _fallback_screen(stock_codes: Optional[list[str]], top_n: int) -> list[FactorResult]:
    """Fallback when baostock is unavailable: return empty results with a message."""
    logger.warning("Factor screening unavailable — baostock not accessible")
    return []
