"""Bootstrap — Dependency check and data source health monitoring.

Checks if akshare/yfinance are installed at startup and provides
a health check endpoint to monitor all data source availability.
"""
import logging
import time

logger = logging.getLogger("ai_berkshire.bootstrap")


def check_dependencies() -> dict:
    """Check which data source libraries are installed."""
    results = {}

    # Check akshare
    try:
        import akshare
        results["akshare"] = {
            "status": "ok",
            "version": getattr(akshare, "__version__", "unknown"),
        }
    except ImportError:
        results["akshare"] = {"status": "missing"}

    # Check yfinance
    try:
        import yfinance
        results["yfinance"] = {
            "status": "ok",
            "version": getattr(yfinance, "__version__", "unknown"),
        }
    except ImportError:
        results["yfinance"] = {"status": "missing"}

    # Check httpx (should always be available with FastAPI)
    try:
        import httpx
        results["httpx"] = {
            "status": "ok",
            "version": getattr(httpx, "__version__", "unknown"),
        }
    except ImportError:
        results["httpx"] = {"status": "missing"}

    return results


async def health_check_data_sources() -> dict:
    """Check all data source endpoints for availability.

    Tests both library-based and HTTP-direct sources.
    Returns latency and status for each source.
    """
    results = {}

    # ── Library availability ──
    lib_deps = check_dependencies()
    results["libraries"] = lib_deps

    # ── HTTP direct sources ──
    http_sources = {}

    # Test Tencent HTTP (A-share price)
    try:
        from ..tools.market_data_http import get_client
        client = await get_client()

        start = time.monotonic()
        resp = await client.get("http://qt.gtimg.cn/q=sh000001", timeout=5)
        latency_ms = round((time.monotonic() - start) * 1000, 1)

        http_sources["tencent_http"] = {
            "status": "ok" if resp.status_code == 200 else f"http_{resp.status_code}",
            "latency_ms": latency_ms,
        }
    except Exception as e:
        http_sources["tencent_http"] = {"status": "error", "error": str(e)[:100]}

    # Test Eastmoney HTTP (A-share kline)
    try:
        from ..tools.market_data_http import get_client
        client = await get_client()

        start = time.monotonic()
        resp = await client.get(
            "http://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": "1.600519",
                "fields1": "f1",
                "fields2": "f51",
                "klt": "101",
                "fqt": "1",
                "end": "20500101",
                "lmt": "1",
            },
            timeout=5,
        )
        latency_ms = round((time.monotonic() - start) * 1000, 1)

        http_sources["eastmoney_http"] = {
            "status": "ok" if resp.status_code == 200 else f"http_{resp.status_code}",
            "latency_ms": latency_ms,
        }
    except Exception as e:
        http_sources["eastmoney_http"] = {"status": "error", "error": str(e)[:100]}

    # Test Sina HTTP
    try:
        from ..tools.market_data_http import get_client
        client = await get_client()

        start = time.monotonic()
        resp = await client.get(
            "http://hq.sinajs.cn/list=sh600519",
            headers={"Referer": "http://finance.sina.com.cn"},
            timeout=5,
        )
        latency_ms = round((time.monotonic() - start) * 1000, 1)

        http_sources["sina_http"] = {
            "status": "ok" if resp.status_code == 200 else f"http_{resp.status_code}",
            "latency_ms": latency_ms,
        }
    except Exception as e:
        http_sources["sina_http"] = {"status": "error", "error": str(e)[:100]}

    # Test Yahoo Finance HTTP (US/HK)
    try:
        from ..tools.market_data_http import get_client
        client = await get_client()

        start = time.monotonic()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
            params={"range": "1d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=5,
        )
        latency_ms = round((time.monotonic() - start) * 1000, 1)

        http_sources["yahoo_http"] = {
            "status": "ok" if resp.status_code == 200 else f"http_{resp.status_code}",
            "latency_ms": latency_ms,
        }
    except Exception as e:
        http_sources["yahoo_http"] = {"status": "error", "error": str(e)[:100]}

    results["http_sources"] = http_sources

    # ── Summary ──
    lib_ok = sum(1 for v in lib_deps.values() if v["status"] == "ok")
    http_ok = sum(1 for v in http_sources.values() if v["status"] == "ok")
    results["summary"] = {
        "libraries_available": f"{lib_ok}/{len(lib_deps)}",
        "http_sources_available": f"{http_ok}/{len(http_sources)}",
        "total_sources": f"{lib_ok + http_ok}/{len(lib_deps) + len(http_sources)}",
        "has_fallback": http_ok > 0,  # At least one HTTP source works
    }

    return results
