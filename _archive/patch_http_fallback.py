#!/usr/bin/env python3
"""Patch script: Add HTTP fallback chain to AI Berkshire market_data.py and main.py.

This script modifies the existing files in-place, adding HTTP direct API
fallbacks when akshare/yfinance are unavailable.

Usage: cd /home/q/ai_berkshire_web && python3 patch_http_fallback.py
"""
import re
import shutil
from pathlib import Path

BASE = Path("/home/q/ai_berkshire_web/app")
MARKET_DATA = BASE / "tools" / "market_data.py"
MAIN_PY = BASE / "main.py"

# ═══════════════════════════════════════════════════════
#  1. Backup original files
# ═══════════════════════════════════════════════════════

for f in [MARKET_DATA, MAIN_PY]:
    bak = f.with_suffix(f".bak{f.suffix}")
    if not bak.exists():
        shutil.copy2(f, bak)
        print(f"  Backup: {f.name} -> {bak.name}")

# ═══════════════════════════════════════════════════════
#  2. Patch market_data.py
# ═══════════════════════════════════════════════════════

md = MARKET_DATA.read_text(encoding="utf-8")

# 2a. Add import for market_data_http at the top (after existing imports)
IMPORT_MARKER = "from ..core.data_cache import DataCache"
HTTP_IMPORT = """from ..core.data_cache import DataCache
from .market_data_http import (
    fetch_a_share_price_tencent,
    fetch_a_share_price_sina,
    fetch_news_eastmoney,
    fetch_news_sina,
    fetch_indices_tencent,
    fetch_indices_tencent_global,
    fetch_indices_yahoo,
    fetch_us_hk_price_tencent,
    fetch_us_hk_price_yahoo,
)"""

if "from .market_data_http import" not in md:
    md = md.replace(IMPORT_MARKER, HTTP_IMPORT, 1)
    print("  [market_data.py] Added HTTP fallback imports")
else:
    print("  [market_data.py] HTTP imports already present, skipping")

# 2b. Patch get_stock_price: add HTTP fallback after the primary source fails
# Find the "except ImportError" block in get_stock_price and add HTTP fallback before it

STOCK_PRICE_FALLBACK = '''        # ── HTTP fallback chain (no library dependency) ──
        if not data:
            logger.info(f"Primary source failed for {symbol}, trying HTTP fallback chain...")
            if market == 'A_SHARE':
                for fetcher_name, fetcher in [
                    ("tencent", fetch_a_share_price_tencent),
                    ("sina", fetch_a_share_price_sina),
                ]:
                    try:
                        data = await fetcher(symbol)
                        if data:
                            logger.info(f"HTTP fallback {fetcher_name} succeeded for {symbol}")
                            break
                    except Exception as e:
                        logger.debug(f"HTTP fallback {fetcher_name} failed: {e}")
            elif market in ('US', 'HK'):
                for fetcher_name, fetcher in [
                    ("tencent", fetch_us_hk_price_tencent),
                    ("yahoo", fetch_us_hk_price_yahoo),
                ]:
                    try:
                        data = await fetcher(symbol)
                        if data:
                            logger.info(f"HTTP fallback {fetcher_name} succeeded for {symbol}")
                            break
                    except Exception as e:
                        logger.debug(f"HTTP fallback {fetcher_name} failed: {e}")

        if data:
            _cache.set("price", symbol, data)
            return data

        # Try stale cache
        stale = _cache.get_stale("price", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]

        return {
            "symbol": symbol,
            "error": f"未获取到 {symbol} 的行情数据（所有数据源均失败）",
            "hint": "可能是数据源不可用且未安装 akshare/yfinance",
            "timestamp": datetime.now().isoformat(),
        }

    except ImportError:
        logger.warning("Data libraries not installed, trying HTTP fallback chain...")
        # ── HTTP fallback when libraries are missing ──
        if market == 'A_SHARE':
            for fetcher_name, fetcher in [
                ("tencent", fetch_a_share_price_tencent),
                ("sina", fetch_a_share_price_sina),
            ]:
                try:
                    data = await fetcher(symbol)
                    if data:
                        logger.info(f"HTTP fallback {fetcher_name} succeeded for {symbol} (no akshare)")
                        _cache.set("price", symbol, data)
                        return data
                except Exception as e:
                    logger.debug(f"HTTP fallback {fetcher_name} failed: {e}")
        elif market in ('US', 'HK'):
            for fetcher_name, fetcher in [
                ("tencent", fetch_us_hk_price_tencent),
                ("yahoo", fetch_us_hk_price_yahoo),
            ]:
                try:
                    data = await fetcher(symbol)
                    if data:
                        logger.info(f"HTTP fallback {fetcher_name} succeeded for {symbol} (no yfinance)")
                        _cache.set("price", symbol, data)
                        return data
                except Exception as e:
                    logger.debug(f"HTTP fallback {fetcher_name} failed: {e}")

        # Try stale cache as last resort
        stale = _cache.get_stale("price", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]
        return {
            "symbol": symbol,
            "error": "所有数据源均不可用 (akshare/yfinance 未安装，HTTP 降级也失败)",
            "hint": "请运行: pip install akshare yfinance，或检查网络连接",
            "timestamp": datetime.now().isoformat(),
        }'''

# The old block we're replacing (in get_stock_price function):
OLD_STOCK_PRICE_BLOCK = '''        if data:
            _cache.set("price", symbol, data)
            return data

        # Try stale cache
        stale = _cache.get_stale("price", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]

        return {
            "symbol": symbol,
            "error": f"未获取到 {symbol} 的行情数据",
            "hint": "可能是数据源暂时不可用或股票代码有误",
            "timestamp": datetime.now().isoformat(),
        }

    except ImportError:
        logger.warning("Data libraries not installed, trying stale cache")
        stale = _cache.get_stale("price", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]
        return {
            "symbol": symbol,
            "error": "数据源库未安装 (akshare/yfinance)",
            "hint": "请运行: pip install akshare yfinance",
            "timestamp": datetime.now().isoformat(),
        }'''

# This old block appears in get_stock_price. We need to be careful to only
# replace the first occurrence (in get_stock_price, not get_financial_data).
# We find it by the unique "未获取到" + "数据源库未安装" pattern.
if "HTTP fallback chain" not in md:
    # Find the exact position - the old block in get_stock_price
    idx = md.find(OLD_STOCK_PRICE_BLOCK)
    if idx >= 0:
        md = md[:idx] + STOCK_PRICE_FALLBACK + md[idx + len(OLD_STOCK_PRICE_BLOCK):]
        print("  [market_data.py] Patched get_stock_price with HTTP fallback chain")
    else:
        print("  [market_data.py] WARNING: Could not find get_stock_price block to patch!")
        # Try a more lenient approach
        print("  [market_data.py] Attempting partial patch...")

# 2c. Patch fetch_company_news: add HTTP fallback after akshare/yfinance fails
NEWS_OLD = '''    try:
        data = await _run_sync(_fetch)
        if data:
            _cache.set("news", cache_key, data)
            return data

        # No news found — check stale cache
        stale = _cache.get_stale("news", cache_key)'''

NEWS_NEW = '''    try:
        data = await _run_sync(_fetch)
        if not data:
            # ── HTTP fallback for news (no library dependency) ──
            logger.info(f"Primary news source failed for {symbol}, trying HTTP fallback...")
            if market == 'A_SHARE':
                for fetcher_name, fetcher in [
                    ("eastmoney", fetch_news_eastmoney),
                    ("sina", fetch_news_sina),
                ]:
                    try:
                        data = await fetcher(symbol)
                        if data:
                            logger.info(f"News HTTP fallback {fetcher_name} succeeded for {symbol}")
                            break
                    except Exception as e:
                        logger.debug(f"News HTTP fallback {fetcher_name} failed: {e}")

        if data:
            _cache.set("news", cache_key, data)
            return data

        # No news found — check stale cache
        stale = _cache.get_stale("news", cache_key)'''

if "News HTTP fallback" not in md:
    if NEWS_OLD in md:
        md = md.replace(NEWS_OLD, NEWS_NEW, 1)
        print("  [market_data.py] Patched fetch_company_news with HTTP fallback")
    else:
        print("  [market_data.py] WARNING: Could not find news block to patch!")

# 2d. Patch fetch_market_indices: add HTTP fallback after akshare/yfinance
INDICES_OLD = '''    try:
        data = await _run_sync(_fetch)
        if data and len(data) > 1:  # More than just timestamp
            _cache.set("indices", "global", data)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch market indices: {e}")
        # Try stale cache
        stale = _cache.get_stale("indices", "global")
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]
        # Return minimal valid structure instead of just error
        return {
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
            "_stale": True,
        }'''

INDICES_NEW = '''    try:
        data = await _run_sync(_fetch)

        # ── HTTP fallback for indices (no library dependency) ──
        # Check if we got any real index data (not just errors/metadata)
        _META_KEYS = {"timestamp", "_partial", "_errors", "_stale", "_source", "error"}
        real_keys = [k for k in (data or {}).keys() if k not in _META_KEYS] if data else []
        if not real_keys:
            logger.info("Primary indices source failed, trying HTTP fallback...")
            http_result = {}
            # A-share indices via Tencent
            try:
                tencent_data = await fetch_indices_tencent()
                if tencent_data:
                    http_result.update(tencent_data)
            except Exception as e:
                logger.debug(f"Tencent indices fallback failed: {e}")
            # US/HK indices via Tencent (primary)
            try:
                tencent_global = await fetch_indices_tencent_global()
                if tencent_global:
                    http_result.update(tencent_global)
            except Exception as e:
                logger.debug(f"Tencent global indices fallback failed: {e}")
            # US/HK indices via Yahoo (secondary)
            try:
                yahoo_data = await fetch_indices_yahoo()
                if yahoo_data:
                    http_result.update(yahoo_data)
            except Exception as e:
                logger.debug(f"Yahoo indices fallback failed: {e}")

            if http_result:
                http_result["timestamp"] = datetime.now().isoformat()
                http_result["_source"] = "http_fallback"
                data = http_result

        if data and len(data) > 1:
            _cache.set("indices", "global", data)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch market indices: {e}")

        # ── Last resort: HTTP fallback ──
        try:
            http_result = {}
            tencent_data = await fetch_indices_tencent()
            if tencent_data:
                http_result.update(tencent_data)
            tencent_global = await fetch_indices_tencent_global()
            if tencent_global:
                http_result.update(tencent_global)
            yahoo_data = await fetch_indices_yahoo()
            if yahoo_data:
                http_result.update(yahoo_data)
            if http_result:
                http_result["timestamp"] = datetime.now().isoformat()
                http_result["_source"] = "http_fallback"
                _cache.set("indices", "global", http_result)
                return http_result
        except Exception:
            pass

        # Try stale cache
        stale = _cache.get_stale("indices", "global")
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]
        # Return minimal valid structure instead of just error
        return {
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
            "_stale": True,
        }'''

if "indices fallback" not in md.lower():
    if INDICES_OLD in md:
        md = md.replace(INDICES_OLD, INDICES_NEW, 1)
        print("  [market_data.py] Patched fetch_market_indices with HTTP fallback")
    else:
        print("  [market_data.py] WARNING: Could not find indices block to patch!")

MARKET_DATA.write_text(md, encoding="utf-8")
print(f"  [market_data.py] Saved ({len(md)} bytes)")

# ═══════════════════════════════════════════════════════
#  3. Patch main.py: add health check endpoint for data sources
# ═══════════════════════════════════════════════════════

mp = MAIN_PY.read_text(encoding="utf-8")

# Add the data source health check endpoint after the existing /health endpoint
HEALTH_ENDPOINT = '''
@app.get("/api/health/data-sources")
async def api_data_source_health():
    """Check all data source availability (libraries + HTTP endpoints)."""
    from .core.bootstrap import health_check_data_sources
    return await health_check_data_sources()


@app.get("/api/data/price/{symbol}"'''

HEALTH_MARKER = '''@app.get("/api/data/price/{symbol}"'''

if "data-sources" not in mp:
    mp = mp.replace(HEALTH_MARKER, HEALTH_ENDPOINT, 1)
    print("  [main.py] Added /api/health/data-sources endpoint")
else:
    print("  [main.py] Health endpoint already present, skipping")

MAIN_PY.write_text(mp, encoding="utf-8")
print(f"  [main.py] Saved ({len(mp)} bytes)")

print("\n✅ Patch complete!")
print("  Backups saved as .bak files")
print("  New files needed: app/tools/market_data_http.py, app/core/bootstrap.py")
print("  Restart the server to apply changes.")
