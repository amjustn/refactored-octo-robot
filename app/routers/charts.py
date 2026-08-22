"""Chart data routes: /api/charts/* — series for report visualizations."""
from fastapi import APIRouter, Query

from ..web_common import data_cache, logger

router = APIRouter()

CHART_CACHE_TTL = 3600 * 12  # financial statements change at most daily


@router.get("/api/charts/financials")
async def api_chart_financials(
    query: str = Query(..., description="股票代码、中文公司名或港股/美股代码"),
):
    """Multi-period financial + valuation series for chart rendering.

    Returns None-ish payload (resolved=False) when the query cannot be
    mapped to a tradable symbol, so the frontend can silently hide the
    chart panel instead of showing an error.
    """
    from ..tools.financial_charts import build_financial_series

    cached = data_cache.get("charts", query, max_age_seconds=CHART_CACHE_TTL)
    if cached:
        return cached

    try:
        payload = await build_financial_series(query)
    except Exception as e:
        logger.error(f"charts/financials failed for {query}: {e}")
        payload = None

    if not payload:
        return {"resolved": False, "query": query}

    payload["resolved"] = True
    data_cache.set("charts", query, payload)
    return payload
