"""Direct HTTP Data Sources — No third-party library dependencies.

Provides fallback data acquisition using raw HTTP APIs when akshare/yfinance
are unavailable or not installed. Only requires httpx (bundled with FastAPI).

Data source priority:
  A-share price:  akshare -> Tencent HTTP -> Sina HTTP -> stale cache
  A-share kline:  akshare -> Eastmoney HTTP -> Tencent HTTP -> stale cache
  A-share news:   akshare -> Eastmoney HTTP -> Sina HTTP -> placeholder
  US/HK price:    yfinance -> Stooq HTTP -> stale cache
  Market indices: akshare+yfinance -> Tencent HTTP + Stooq HTTP -> stale cache

Each function returns the same dict format as the primary sources.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("ai_berkshire.market_data_http")

# Shared async HTTP client (lazy-initialized)
_http_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    """Get or create the shared httpx async client."""
    global _http_client
    if _http_client is not None:
        try:
            if _http_client.is_closed:
                _http_client = None
        except Exception:
            _http_client = None
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AIBerkshire/1.0)"},
        )
    return _http_client


async def close_client():
    """Close the shared HTTP client (call on shutdown)."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        try:
            await _http_client.aclose()
        except Exception:
            pass
    _http_client = None


# ═══════════════════════════════════════════════════════
#  A-Share Real-time Price — Tencent Finance (direct HTTP)
# ═══════════════════════════════════════════════════════

async def fetch_a_share_price_tencent(symbol: str) -> Optional[dict]:
    """Fetch A-share real-time price via Tencent Finance HTTP API.

    No library dependency -- pure HTTP.
    API: http://qt.gtimg.cn/q=sh600519
    """
    code = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    prefix = 'sh' if code.startswith(('6', '9')) else 'sz'

    try:
        client = await get_client()
        resp = await client.get(f"http://qt.gtimg.cn/q={prefix}{code}")
        text = resp.text

        # Tencent format: v_sh600519="1~贵州茅台~600519~1800.00~1795.00~..."
        parts = text.split('~')
        if len(parts) < 50:
            return None

        return {
            "symbol": symbol,
            "market": "A_SHARE",
            "name": parts[1],
            "price": float(parts[3]) if parts[3] else 0,
            "prev_close": float(parts[4]) if parts[4] else 0,
            "open": float(parts[5]) if parts[5] else 0,
            "volume": float(parts[6]) if parts[6] else 0,
            "high": float(parts[33]) if parts[33] else 0,
            "low": float(parts[34]) if parts[34] else 0,
            "change_pct": float(parts[32]) if parts[32] else 0,
            "change_amount": float(parts[31]) if parts[31] else 0,
            "turnover": float(parts[37]) if parts[37] else 0,
            "pe": float(parts[39]) if parts[39] else 0,
            "pb": float(parts[46]) if parts[46] else 0,
            "total_market_cap": float(parts[45]) * 1e4 if parts[45] else 0,
            "circulating_market_cap": float(parts[44]) * 1e4 if parts[44] else 0,
            "timestamp": datetime.now().isoformat(),
            "_source": "tencent_http",
        }
    except Exception as e:
        logger.debug(f"Tencent HTTP price failed for {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  A-Share Real-time Price — Sina Finance (second fallback)
# ═══════════════════════════════════════════════════════

async def fetch_a_share_price_sina(symbol: str) -> Optional[dict]:
    """Fetch A-share real-time price via Sina Finance HTTP API.

    API: http://hq.sinajs.cn/list=sh600519
    Note: Requires Referer header for Sina's anti-hotlinking.
    """
    code = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    prefix = 'sh' if code.startswith(('6', '9')) else 'sz'

    try:
        client = await get_client()
        resp = await client.get(
            f"http://hq.sinajs.cn/list={prefix}{code}",
            headers={"Referer": "http://finance.sina.com.cn"},
        )
        text = resp.text

        match = re.search(r'="([^"]+)"', text)
        if not match:
            return None

        fields = match.group(1).split(',')
        if len(fields) < 10:
            return None

        price = float(fields[3]) if fields[3] else 0
        prev_close = float(fields[2]) if fields[2] else 0

        if price == 0:
            return None

        return {
            "symbol": symbol,
            "market": "A_SHARE",
            "name": fields[0],
            "open": float(fields[1]) if fields[1] else 0,
            "prev_close": prev_close,
            "price": price,
            "high": float(fields[4]) if fields[4] else 0,
            "low": float(fields[5]) if fields[5] else 0,
            "volume": float(fields[8]) if fields[8] else 0,
            "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "change_amount": round(price - prev_close, 2) if prev_close else 0,
            "timestamp": datetime.now().isoformat(),
            "_source": "sina_http",
        }
    except Exception as e:
        logger.debug(f"Sina HTTP price failed for {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  A-Share K-line — Eastmoney (direct HTTP)
# ═══════════════════════════════════════════════════════

async def fetch_a_share_kline_eastmoney(symbol: str, count: int = 250) -> Optional[list]:
    """Fetch A-share daily K-line via Eastmoney HTTP API.

    API: http://push2his.eastmoney.com/api/qt/stock/kline/get
    """
    code = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    secid = f"1.{code}" if code.startswith(('6', '9')) else f"0.{code}"

    try:
        client = await get_client()
        resp = await client.get(
            "http://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": "101",
                "fqt": "1",
                "end": "20500101",
                "lmt": str(count),
            },
        )
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None

        result = []
        for line in klines:
            parts = line.split(',')
            if len(parts) >= 7:
                result.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                })
        return result if result else None
    except Exception as e:
        logger.debug(f"Eastmoney HTTP kline failed for {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  Company News — Eastmoney (direct HTTP)
# ═══════════════════════════════════════════════════════

async def fetch_news_eastmoney(symbol: str, page_size: int = 20) -> Optional[list]:
    """Fetch company news via Eastmoney search API."""
    code = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')

    try:
        client = await get_client()
        resp = await client.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={
                "cb": "jQuery_callback",
                "param": code,
                "type": "8193",
                "pageIndex": "1",
                "pageSize": str(page_size),
            },
            timeout=10,
        )

        text = resp.text
        json_str = re.search(r'jQuery_callback\((.*)\)', text)
        if json_str:
            data = json.loads(json_str.group(1))
            news_list = []
            articles = data.get("Data", {}).get("Article", {}).get("List", [])
            for item in articles:
                news_list.append({
                    "title": item.get("Title", ""),
                    "content": (item.get("Content", "") or "")[:500],
                    "date": item.get("ShowTime", ""),
                    "source": item.get("Source", ""),
                })
            if news_list:
                return news_list
    except Exception as e:
        logger.debug(f"Eastmoney HTTP news failed for {symbol}: {e}")

    return None


# ═══════════════════════════════════════════════════════
#  Company News — Sina Finance (second fallback)
# ═══════════════════════════════════════════════════════

async def fetch_news_sina(symbol: str, page_size: int = 20) -> Optional[list]:
    """Fetch company news via Sina Finance rolling news feed."""
    code = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')

    try:
        client = await get_client()
        resp = await client.get(
            "https://feed.mix.sina.com.cn/api/roll/get",
            params={
                "pageid": "153",
                "lid": "2516",
                "k": code,
                "num": str(page_size),
                "page": "1",
            },
            timeout=10,
        )
        data = resp.json()
        news_list = []
        for item in data.get("data", []):
            news_list.append({
                "title": item.get("title", ""),
                "content": (item.get("intro", "") or "")[:500],
                "date": item.get("ctime", ""),
                "source": item.get("media_name", ""),
            })
        return news_list if news_list else None
    except Exception as e:
        logger.debug(f"Sina HTTP news failed for {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  Market Indices — Tencent HTTP (A-share indices)
# ═══════════════════════════════════════════════════════

async def fetch_indices_tencent() -> Optional[dict]:
    """Fetch A-share market indices via Tencent Finance HTTP API.

    Retrieves SSE, SZSE, and CHINEXT indices.
    """
    try:
        client = await get_client()
        resp = await client.get("http://qt.gtimg.cn/q=sh000001,sz399001,sz399006")
        text = resp.text

        result = {}
        # Tencent returns multiple entries separated by semicolons
        # Each entry: v_sh000001="1~上证指数~000001~3200.00~..."
        entries = text.strip().split(';')
        for entry in entries:
            entry = entry.strip()
            if not entry or '=' not in entry:
                continue
            # Extract the value between quotes
            match = re.search(r'="([^"]+)"', entry)
            if not match:
                continue
            parts = match.group(1).split('~')
            if len(parts) < 35:
                continue

            name = parts[1] if len(parts) > 1 else ""

            if '上证指数' in name or '000001' in parts[2]:
                result["SSE"] = {
                    "name": name,
                    "code": "000001",
                    "price": float(parts[3]) if parts[3] else 0,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                }
            elif '深证成指' in name or '399001' in parts[2]:
                result["SZSE"] = {
                    "name": name,
                    "code": "399001",
                    "price": float(parts[3]) if parts[3] else 0,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                }
            elif '创业板指' in name or '399006' in parts[2]:
                result["CHINEXT"] = {
                    "name": name,
                    "code": "399006",
                    "price": float(parts[3]) if parts[3] else 0,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                }

        return result if result else None
    except Exception as e:
        logger.debug(f"Tencent HTTP indices failed: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  US/HK Stock Price — Tencent Finance (primary HTTP fallback)
# ═══════════════════════════════════════════════════════

async def fetch_us_hk_price_tencent(symbol: str) -> Optional[dict]:
    """Fetch US/HK stock price via Tencent Finance HTTP API.

    No library dependency -- pure HTTP.
    US stocks: http://qt.gtimg.cn/q=usAAPL
    HK stocks: http://qt.gtimg.cn/q=hk07000 (5-digit padded)
    """
    sym_upper = symbol.upper()

    if sym_upper.endswith('.HK'):
        code = sym_upper.replace('.HK', '').zfill(5)
        tencent_sym = f"hk{code}"
        market = 'HK'
        currency = 'HKD'
    else:
        tencent_sym = f"us{sym_upper}"
        market = 'US'
        currency = 'USD'

    try:
        client = await get_client()
        resp = await client.get(f"http://qt.gtimg.cn/q={tencent_sym}")
        text = resp.text

        parts = text.split('~')
        if len(parts) < 50:
            return None

        price = float(parts[3]) if parts[3] else 0
        if price == 0:
            return None

        prev_close = float(parts[4]) if parts[4] else 0

        return {
            "symbol": symbol,
            "market": market,
            "name": parts[1] if parts[1] else sym_upper,
            "price": price,
            "prev_close": prev_close,
            "open": float(parts[5]) if parts[5] else 0,
            "volume": float(parts[6]) if parts[6] else 0,
            "high": float(parts[33]) if parts[33] else 0,
            "low": float(parts[34]) if parts[34] else 0,
            "change_pct": float(parts[32]) if parts[32] else 0,
            "change_amount": float(parts[31]) if parts[31] else 0,
            "turnover": float(parts[37]) if parts[37] else 0,
            "currency": currency,
            "timestamp": datetime.now().isoformat(),
            "_source": "tencent_http",
        }
    except Exception as e:
        logger.debug(f"Tencent HTTP US/HK price failed for {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  US/HK Stock Price — Yahoo Finance (secondary HTTP fallback)
# ═══════════════════════════════════════════════════════

async def fetch_us_hk_price_yahoo(symbol: str) -> Optional[dict]:
    """Fetch US/HK stock price via Yahoo Finance chart API.

    No API key required. Returns JSON.
    API: https://query1.finance.yahoo.com/v8/finance/chart/AAPL
    """
    yf_symbol = symbol.upper()

    try:
        client = await get_client()
        resp = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}",
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        chart = data.get("chart", {})
        result = chart.get("result", [])
        if not result:
            return None

        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice", 0)
        if not price:
            return None

        prev_close = meta.get("chartPreviousClose", meta.get("previousClose", price))
        market = 'HK' if '.HK' in yf_symbol else 'US'
        currency = meta.get("currency", 'HKD' if market == 'HK' else 'USD')

        # Extract OHLC from indicators
        indicators = result[0].get("indicators", {})
        quotes = indicators.get("quote", [{}])[0]
        ohlc_list = quotes.get("close", [])
        vol_list = quotes.get("volume", [])

        # Get latest valid OHLC
        open_price = 0
        high_price = 0
        low_price = 0
        volume = 0
        for i in range(len(ohlc_list) - 1, -1, -1):
            if ohlc_list[i] is not None:
                if i > 0 and quotes.get("open", [None] * (i + 1))[i] is not None:
                    open_price = quotes["open"][i]
                    high_price = quotes["high"][i]
                    low_price = quotes["low"][i]
                if vol_list and i < len(vol_list) and vol_list[i] is not None:
                    volume = vol_list[i]
                break

        return {
            "symbol": symbol,
            "market": market,
            "name": meta.get("symbol", yf_symbol),
            "price": float(price),
            "open": float(open_price) if open_price else 0,
            "high": float(high_price) if high_price else 0,
            "low": float(low_price) if low_price else 0,
            "volume": int(volume) if volume else 0,
            "prev_close": float(prev_close) if prev_close else 0,
            "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "change_amount": round(price - prev_close, 2) if prev_close else 0,
            "currency": currency,
            "timestamp": datetime.now().isoformat(),
            "_source": "yahoo_http",
        }
    except Exception as e:
        logger.debug(f"Yahoo HTTP price failed for {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  Market Indices — Tencent HTTP (US/HK indices)
# ═══════════════════════════════════════════════════════

async def fetch_indices_tencent_global() -> Optional[dict]:
    """Fetch US/HK market indices via Tencent Finance HTTP API.

    Retrieves HSI, S&P 500, NASDAQ, Dow Jones.
    """
    # Tencent index symbols
    tencent_indices = {
        "hkHSI": "HSI",        # Hang Seng Index
        "usDJI": "DOW",        # Dow Jones
        "usIXIC": "NASDAQ",    # NASDAQ
        "usSPX": "S&P500",     # S&P 500
    }

    try:
        client = await get_client()
        symbols_param = ','.join(tencent_indices.keys())
        resp = await client.get(f"http://qt.gtimg.cn/q={symbols_param}")
        text = resp.text

        result = {}
        entries = text.strip().split(';')
        for entry in entries:
            entry = entry.strip()
            if not entry or '=' not in entry:
                continue
            match = re.search(r'="([^"]+)"', entry)
            if not match:
                continue
            parts = match.group(1).split('~')
            if len(parts) < 35:
                continue

            name = parts[1] if len(parts) > 1 else ""
            price = float(parts[3]) if parts[3] else 0
            if price == 0:
                continue

            # Match by checking the variable name prefix
            for tencent_key, result_key in tencent_indices.items():
                if tencent_key.lower() in entry.lower():
                    result[result_key] = {
                        "name": name,
                        "price": price,
                        "change_pct": float(parts[32]) if parts[32] else 0,
                    }
                    break

        return result if result else None
    except Exception as e:
        logger.debug(f"Tencent HTTP global indices failed: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  Market Indices — Yahoo Finance (US/HK indices, secondary)
# ═══════════════════════════════════════════════════════

async def fetch_indices_yahoo() -> Optional[dict]:
    """Fetch US/HK market indices via Yahoo Finance chart API.

    Retrieves S&P 500, NASDAQ, Dow Jones, HSI.
    """
    yahoo_indices = {
        "^GSPC": "S&P500",
        "^IXIC": "NASDAQ",
        "^DJI": "DOW",
        "^HSI": "HSI",
    }

    async def _fetch_one(client, yf_sym, key):
        try:
            resp = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}",
                params={"range": "5d", "interval": "1d"},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=8,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            chart = data.get("chart", {})
            results = chart.get("result", [])
            if not results:
                return None

            meta = results[0].get("meta", {})
            price = meta.get("regularMarketPrice", 0)
            prev_close = meta.get("chartPreviousClose", meta.get("previousClose", price))
            if not price:
                return None

            return key, {
                "symbol": yf_sym,
                "price": float(price),
                "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            }
        except Exception as e:
            logger.debug(f"Yahoo indices {yf_sym} failed: {e}")
            return None

    result = {}
    try:
        import asyncio as _asyncio
        client = await get_client()
        # 4 个符号并发抓取（原为串行；Yahoo 不可达时串行耗时 4×10s=40s）
        rows = await _asyncio.gather(*[
            _fetch_one(client, yf_sym, key) for yf_sym, key in yahoo_indices.items()
        ])
        for row in rows:
            if row:
                result[row[0]] = row[1]
    except Exception as e:
        logger.debug(f"Yahoo HTTP indices failed: {e}")

    return result if result else None
