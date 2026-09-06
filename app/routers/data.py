"""Market data routes: /api/data/*, /api/health/data-sources*, /api/screen/factors."""
import asyncio
from datetime import datetime

from fastapi import APIRouter

from ..web_common import data_cache, logger

router = APIRouter()


# ==================== Market Data API ====================


@router.get("/api/health/data-sources")
async def api_data_source_health():
    """Check all data source availability (libraries + HTTP endpoints),
    plus ToolGateway circuit-breaker states."""
    from ..core.bootstrap import health_check_data_sources
    from ..harness.tools import get_gateway
    from ..tools.market_data import sync_pool_stats
    result = await health_check_data_sources()
    result["circuit_breakers"] = get_gateway().breaker_states()
    result["sync_pool"] = sync_pool_stats()
    return result


@router.post("/api/health/data-sources/reset")
async def api_data_source_reset(tool: str = None):
    """Manually reset circuit breakers (one tool, or all when omitted)."""
    from ..harness.tools import get_gateway
    return get_gateway().reset_breaker(tool)



@router.get("/api/data/web-search")
async def api_web_search(q: str, max_results: int = 10):
    """Search the web for any topic."""
    from ..tools.web_search import web_search
    results = await web_search(q, max_results=max_results)
    return {"query": q, "results": results}


# ── P6: Factor screening ────────────────────────────────────────

@router.get("/api/screen/factors")
async def api_screen_factors(
    categories: str = "value,quality,momentum,growth",
    top_n: int = 20,
):
    """P6: Screen A-share stocks using multi-factor scoring.

    categories: comma-separated list of factor categories
    top_n: number of top-ranked stocks to return
    """
    from ..tools.factor_screen import CATEGORY_LABELS, FACTOR_DEFS, last_screen_partial, screen_stocks

    cat_list = [c.strip() for c in categories.split(",") if c.strip()]
    try:
        # E4: screen_stocks does dozens of blocking baostock round-trips —
        # keep them off the event loop.
        results = await asyncio.wait_for(
            asyncio.to_thread(screen_stocks, categories=cat_list, top_n=min(top_n, 50)),
            timeout=30.0,  # 函数内已有25s预算+兜底; 此处防线程彻底卡死
        )
    except asyncio.TimeoutError:
        logger.error("Factor screening timed out (>30s) — returning degraded response")
        return {
            "status": "partial",
            "message": "因子筛选超时，数据源响应过慢，请稍后重试",
            "results": [],
        }
    except Exception as e:
        logger.error(f"Factor screening failed: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"因子筛选失败: {str(e)}",
            "results": [],
        }

    # Format for frontend
    resp = {
        "status": "ok",
        "categories": {c: CATEGORY_LABELS.get(c, c) for c in cat_list},
        "factor_count": len(FACTOR_DEFS),
        "total_factors": len(FACTOR_DEFS),
        "results": [
            {
                "rank": r.rank,
                "code": r.code,
                "name": r.name,
                "score": round(r.score, 1),
                "factors": r.factors,
            }
            for r in results
        ],
    }
    if last_screen_partial:
        resp["status"] = "partial"
        resp["message"] = f"时间预算内仅完成 {len(results)} 只股票的因子计算，结果可能不完整"
    return resp


@router.get("/api/data/read-url")
async def api_read_url(url: str, max_chars: int = 8000):
    """Read any web page and extract text content."""
    from ..tools.web_search import read_webpage
    # E8: clamp caller-supplied max_chars — an unbounded value lets a request
    # pull arbitrarily large page text into memory / the prompt.
    max_chars = max(100, min(int(max_chars), 20000))
    result = await read_webpage(url, max_chars=max_chars)
    return result



@router.get("/api/data/industry-search")
async def api_industry_search(q: str, max_per_source: int = 3):
    """Search industry chain information from curated sources."""
    from ..tools.industry_sources import search_industry_chain
    result = await search_industry_chain(q, max_per_source=max_per_source)
    return result


@router.get("/api/data/industry-sources")
async def api_industry_sources(category: str = ""):
    """List available industry research data sources."""
    from ..tools.industry_sources import list_industry_sources
    return {"sources": list_industry_sources(category)}


@router.get("/api/data/price/{symbol}")
async def api_get_price(symbol: str):
    """Get real-time stock price for any market."""
    import math

    from ..tools.market_data import get_stock_price
    try:
        data = await get_stock_price(symbol)
        # Sanitize NaN values
        if isinstance(data, dict):
            for k, v in list(data.items()):
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    data[k] = None
        return {"symbol": symbol, "data": data}
    except Exception as e:
        logger.error(f"Price API error for {symbol}: {e}", exc_info=True)
        return {"symbol": symbol, "data": {"error": str(e)}}


@router.get("/api/data/financials/{symbol}")
async def api_get_financials(symbol: str):
    """Get financial statements for a company."""
    import math

    from ..tools.market_data import get_financial_data
    try:
        data = await get_financial_data(symbol)
        # Sanitize NaN values (akshare returns pandas NaN, not JSON-serializable)
        def _sanitize(obj):
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_sanitize(v) for v in obj]
            if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                return None
            return obj
        data = _sanitize(data)
        return {"symbol": symbol, "data": data}
    except Exception as e:
        logger.error(f"Financial data API error for {symbol}: {e}", exc_info=True)
        return {"symbol": symbol, "data": {"error": str(e), "timestamp": datetime.now().isoformat()}}


@router.get("/api/data/news/{symbol}")
async def api_get_news(symbol: str, days: int = 14):
    """Get recent news for a company."""
    from ..tools.market_data import fetch_company_news
    data = await fetch_company_news(symbol, days)
    return {"symbol": symbol, "news": data}


@router.get("/api/data/indices")
async def api_get_indices():
    """Get major market indices."""
    from ..tools.market_data import fetch_market_indices
    data = await fetch_market_indices()
    return {"indices": data}


@router.get("/api/data/search")
async def api_search_company(q: str):
    """Search for companies by name or code."""
    from ..tools.market_data import search_company
    results = await search_company(q)
    return {"query": q, "results": results}


# ── 券商/机构研报 ──────────────────────────────────────

@router.get("/api/data/broker-reports/{symbol}")
async def api_broker_reports(symbol: str, days: int = 365):
    """Get broker research reports + rating summary for an A-share stock.

    Returns rating distribution, consensus estimates (next-year EPS/PE,
    target price range), and the most recent reports.
    """
    from ..tools.broker_reports import get_rating_summary
    data = await get_rating_summary(symbol, days=days)
    return data


@router.get("/api/data/broker-reports/{symbol}/list")
async def api_broker_reports_list(symbol: str, days: int = 90, limit: int = 20):
    """Get the raw broker report list for an A-share stock."""
    from ..tools.broker_reports import get_stock_reports
    data = await get_stock_reports(symbol, days=days, limit=limit)
    return data


@router.get("/api/data/broker-reports/{symbol}/industry")
async def api_broker_industry_reports(keyword: str = "", days: int = 30, limit: int = 15):
    """Get industry-level broker reports, optionally filtered by keyword."""
    from ..tools.broker_reports import get_industry_reports
    data = await get_industry_reports(keyword=keyword, days=days, limit=limit)
    return data


@router.get("/api/data/broker-reports/detail/{info_code}")
async def api_broker_report_detail(info_code: str, max_chars: int = 6000):
    """Get the full text body of a broker report by its info_code."""
    from ..tools.broker_reports import get_report_detail
    data = await get_report_detail(info_code, max_chars=max_chars)
    return data


@router.get("/api/data/context")
async def api_get_context(arguments: str = "", skill_name: str = ""):
    """Get the full real-world context that would be injected into agent prompts."""
    import asyncio

    from ..core.context import build_full_context, build_time_context
    try:
        # 整体超时保护：数据源（如 Yahoo）不可达时不得无限挂起
        context = await asyncio.wait_for(
            build_full_context(arguments, skill_name=skill_name),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        context = build_time_context() + "\n\n*（市场数据获取超时，仅返回时间上下文）*"
    return {"context": context}


@router.get("/api/data/cache/stats")
async def api_cache_stats():
    """Get data cache statistics."""
    return {"stats": data_cache.get_stats()}


@router.post("/api/data/cache/cleanup")
async def api_cache_cleanup():
    """Manually trigger cache cleanup."""
    deleted = data_cache.cleanup_expired()
    return {"deleted": deleted}
