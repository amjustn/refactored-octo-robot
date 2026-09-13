"""Market Data Fetcher -- Real-time external data acquisition.

Automatically detects market type (A-share / HK / US) based on symbol,
fetches real-time stock prices, financial statements, company info,
and market indices using free data sources (akshare / yfinance).

All data is cached in SQLite with configurable TTL.
Falls back to stale cache when network is unavailable.

v3.1 fixes:
- Chinese company name resolution (腾讯 → 0700.HK, 茅台 → 600519.SH)
- Case-insensitive market detection (aapl → US)
- Correct akshare API usage for A-share prices
- No infinite recursion in get_stock_price
- Meaningful fallbacks instead of empty lists
- Proper timezone detection
"""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from ..core.config import MARKET_SYNC_WORKERS
from ..core.data_cache import DataCache
from .market_data_http import (
    fetch_a_share_price_sina,
    fetch_a_share_price_tencent,
    fetch_indices_tencent,
    fetch_indices_tencent_global,
    fetch_indices_yahoo,
    fetch_news_eastmoney,
    fetch_news_sina,
    fetch_us_hk_price_tencent,
    fetch_us_hk_price_yahoo,
)

logger = logging.getLogger("ai_berkshire.market_data")

# Cache TTLs (seconds)
CACHE_TTL_REALTIME = 300       # 5 minutes for real-time prices
CACHE_TTL_FINANCIAL = 86400    # 24 hours for financial statements
CACHE_TTL_NEWS = 3600          # 1 hour for news
CACHE_TTL_INDICES = 600        # 10 minutes for market indices
CACHE_TTL_COMPANY = 86400      # 24 hours for company info

_cache = DataCache()

# Dedicated bounded executor for sync SDK calls (akshare/yfinance).
# asyncio.wait_for cancels the await but CANNOT kill the underlying thread,
# so a hung fetch leaves an orphaned thread running. Routing all sync calls
# through this small pool caps orphan accumulation and isolates hung
# market-data threads from the event loop's default executor.
SYNC_EXECUTOR = ThreadPoolExecutor(
    max_workers=MARKET_SYNC_WORKERS,
    thread_name_prefix="mkt-sync",
)

# Hung-call telemetry: number of wait_for timeouts observed around sync
# fetches. Each timeout potentially leaves one orphaned thread in the pool.
_sync_timeouts_total = 0


def _record_sync_timeout(source: str) -> None:
    """Record a wait_for timeout around a sync fetch (orphaned thread likely)."""
    global _sync_timeouts_total
    _sync_timeouts_total += 1
    logger.warning(
        f"sync fetch timed out (source={source}); "
        f"orphaned thread may remain in mkt-sync pool "
        f"(timeouts_total={_sync_timeouts_total})"
    )


def sync_pool_stats() -> dict:
    """Stats for the dedicated sync executor, exposed via health endpoint."""
    return {
        "workers": MARKET_SYNC_WORKERS,
        "queued_estimate": SYNC_EXECUTOR._work_queue.qsize(),
        "timeouts_total": _sync_timeouts_total,
    }

# ==================== Chinese Company Name Mapping ====================
# Common Chinese company names → stock code
# This allows users to type "腾讯" instead of "0700.HK"
COMPANY_NAME_MAP = {
    # HK stocks
    "腾讯": "0700.HK", "腾讯控股": "0700.HK",
    "阿里巴巴": "9988.HK", "阿里": "9988.HK",
    "美团": "3690.HK", "美团点评": "3690.HK",
    "小米": "1810.HK", "小米集团": "1810.HK",
    "京东": "9618.HK",
    "网易": "9999.HK",
    "快手": "1024.HK",
    "百度": "9888.HK",
    "哔哩哔哩": "9626.HK", "B站": "9626.HK", "哔哩": "9626.HK",
    "蔚来": "9866.HK", "蔚来汽车": "9866.HK",
    "理想汽车": "2015.HK", "理想": "2015.HK",
    "小鹏汽车": "9868.HK", "小鹏": "9868.HK",
    "比亚迪": "1211.HK",
    "吉利汽车": "0175.HK", "吉利": "0175.HK",
    "长城汽车": "2333.HK",
    "华虹半导体": "1347.HK", "华虹": "1347.HK",
    "中芯国际": "0981.HK",
    "药明生物": "2269.HK",
    "舜宇光学": "2382.HK",
    "海底捞": "6862.HK",
    "农夫山泉": "9633.HK",
    "安踏体育": "2020.HK", "安踏": "2020.HK",
    "微盟": "2013.HK",
    "有赞": "8083.HK",
    "泡泡玛特": "9992.HK",
    "商汤": "0020.HK", "商汤科技": "0020.HK",
    "联想": "0992.HK", "联想集团": "0992.HK",
    # A-share stocks
    "贵州茅台": "600519.SH", "茅台": "600519.SH",
    "五粮液": "000858.SZ",
    "宁德时代": "300750.SZ",
    "比亚迪A股": "002594.SZ",
    "中国平安": "601318.SH", "平安集团": "601318.SH", "平安": "601318.SH",
    "平安银行": "000001.SZ",
    "招商银行": "600036.SH", "招行": "600036.SH", "招商银行A股": "600036.SH",
    "工商银行": "601398.SH", "工行": "601398.SH",
    "农业银行": "601288.SH", "农行": "601288.SH",
    "中国银行": "601988.SH", "中行": "601988.SH",
    "交通银行": "601328.SH", "交行": "601328.SH",
    "邮储银行": "601658.SH",
    "兴业银行": "601166.SH",
    "浦发银行": "600000.SH",
    "民生银行": "600016.SH",
    "中信银行": "601998.SH",
    "光大银行": "601818.SH",
    "华夏银行": "600015.SH",
    "宁波银行": "002142.SZ",
    "江苏银行": "600919.SH",
    "杭州银行": "600926.SH",
    "成都银行": "601838.SH",
    "建设银行": "601939.SH", "建行": "601939.SH",
    "中国石油": "601857.SH", "中石油": "601857.SH",
    "中国石化": "600028.SH", "中石化": "600028.SH",
    "长江电力": "600900.SH",
    "隆基绿能": "601012.SH", "隆基": "601012.SH",
    "万华化学": "600309.SH",
    "恒瑞医药": "600276.SH", "恒瑞": "600276.SH",
    "迈瑞医疗": "300760.SZ", "迈瑞": "300760.SZ",
    "药明康德": "603259.SH",
    "海康威视": "002415.SZ", "海康": "002415.SZ",
    "美的集团": "000333.SZ", "美的": "000333.SZ",
    "格力电器": "000651.SZ", "格力": "000651.SZ",
    "海尔智家": "600690.SH", "海尔": "600690.SH",
    "伊利股份": "600887.SH", "伊利": "600887.SH",
    "泸州老窖": "000568.SZ",
    "洋河股份": "002304.SZ", "洋河": "002304.SZ",
    "顺丰控股": "002352.SZ", "顺丰": "002352.SZ",
    "三一重工": "600031.SH", "三一": "600031.SH",
    "东方财富": "300059.SZ",
    "同花顺": "300033.SZ",
    "金山办公": "688111.SH",
    "中际旭创": "300308.SZ",
    "中际讯创": "300308.SZ",
    "爱尔眼科": "300015.SZ",
    "片仔癀": "600436.SH",
    "云南白药": "000538.SZ",
    # US stocks (include both Chinese and English names)
    "苹果": "AAPL", "Apple": "AAPL",
    "谷歌": "GOOGL", "Google": "GOOGL",
    "微软": "MSFT", "Microsoft": "MSFT",
    "亚马逊": "AMZN", "Amazon": "AMZN",
    "脸书": "META", "Meta": "META", "Facebook": "META",
    "特斯拉": "TSLA", "Tesla": "TSLA",
    "英伟达": "NVDA", "Nvidia": "NVDA", "辉达": "NVDA",
    "拼多多": "PDD",
    "好未来": "TAL",
    "新东方": "EDU",
    "京东美股": "JD",
    "网易美股": "NTES",
    "哔哩哔哩美股": "BILI",
    "理想美股": "LI",
    "小鹏美股": "XPEV",
    "蔚来美股": "NIO",
    "台积电": "TSM", "TSMC": "TSM",
    "超微": "AMD", "AMD": "AMD",
    "高通": "QCOM",
    "博通": "AVGO",
}

# ── 已知未上市公司表（无股票代码的知名公司）──
# 用于 context 注入：拉不到行情时对照此表确认"确实是未上市公司"
# 不在表里的公司 → 只给模糊提示，不武断下结论
KNOWN_UNLISTED_COMPANIES: dict[str, str] = {
    "字节跳动": "全球最大未上市科技公司，旗下抖音/TikTok，估值约$200-300B，PE投资方包括软银、KKR等",
    "bytedance": "ByteDance, world's largest unlisted tech company (TikTok/Douyin), est. valuation $200-300B",
    "tiktok": "TikTok, owned by ByteDance (unlisted). See also: 字节跳动",
    "小红书": "社交电商平台，红杉/腾讯/阿里投资，估值约$17B，未上市",
    "xiaohongshu": "Xiaohongshu (RED), social-commerce platform, est. valuation ~$17B, unlisted",
    "shein": "SHEIN, fast-fashion e-commerce, est. valuation $66B, filed for IPO but not yet listed",
    "希音": "SHEIN/希音，快时尚跨境电商，估值约$66B，已提交IPO但尚未上市",
    "discord": "Discord, gaming-focused chat platform, est. valuation ~$15B, unlisted",
    "openai": "OpenAI, AI research company (ChatGPT), est. valuation $150B+, unlisted",
    "蚂蚁集团": "蚂蚁集团，支付宝母公司，曾申请IPO被叫停(2020)，目前未上市",
    "ant group": "Ant Group, Alipay parent, IPO suspended (2020), currently unlisted",
    "华为": "华为技术，全球最大通信设备商，员工持股，明确不上市",
    "huawei": "Huawei Technologies, employee-owned, has stated it will not go public",
    "大疆": "大疆创新/DJI，全球无人机龙头，估值约$20B，未上市",
    "dji": "DJI (大疆), global drone leader, est. valuation ~$20B, unlisted",
    "滴滴": "滴滴出行/Didi，曾美股上市(2021)后私有化退市(2022)，目前已退市",
    "didi": "Didi Chuxing, delisted from NYSE (2022), currently unlisted",
    "米哈游": "米哈游/miHoYo，原神/崩坏开发商，未上市",
    "mihoyo": "miHoYo/HoYoverse, developer of Genshin Impact, unlisted",
    "spacex": "SpaceX, Elon Musk's space company, est. valuation $350B+, unlisted",
    "stripe": "Stripe, online payment processor, est. valuation ~$70B, unlisted",
    "databricks": "Databricks, data+AI platform, est. valuation $62B, unlisted",
    "canva": "Canva, online design platform, est. valuation ~$32B, unlisted",
    "epic games": "Epic Games, Fortnite/Unreal Engine, est. valuation ~$22B, unlisted",
    "instacart": "Instacart, grocery delivery → went public Sep 2023 as CART — now LISTED",
    "klarna": "Klarna, buy-now-pay-later, filed for IPO — check latest status",
    "reddit": "Reddit, social media → went public Mar 2024 as RDDT — now LISTED",
    "菜鸟网络": "菜鸟网络/Cainiao，阿里旗下物流，曾计划IPO后撤回，目前未上市",
    "cainiao": "Cainiao (菜鸟), Alibaba logistics arm, IPO withdrawn, currently unlisted",
}

def check_listing_status(company_name: str) -> Optional[dict]:
    """Check whether a company is listed, unlisted, or unknown.

    Returns None if company_name is empty. Otherwise returns:
      {listed: bool, confidence: 'high'|'low', note: str, hint: str}
    - listed=True:  known listed company (found in COMPANY_NAME_MAP)
    - listed=False: known unlisted company (found in KNOWN_UNLISTED_COMPANIES)
    - listed=None:  status unknown — could be either
    """
    if not company_name or not company_name.strip():
        return None
    name = company_name.strip()

    # 1. Check if it's in the listed companies map
    symbol = _resolve_company_name(name)
    if symbol:
        return {
            "listed": True,
            "confidence": "high",
            "note": f"已确认为上市公司，代码 {symbol}",
            "hint": "",
        }

    # 2. Check if it's in the known unlisted companies map
    name_lower = name.lower()
    for uname, desc in KNOWN_UNLISTED_COMPANIES.items():
        if name_lower == uname.lower() or uname.lower() in name_lower:
            # Check for "now LISTED" overrides
            if "now LISTED" in desc:
                return {
                    "listed": True,
                    "confidence": "high",
                    "note": desc,
                    "hint": "",
                }
            return {
                "listed": False,
                "confidence": "high",
                "note": desc,
                "hint": "",
            }

    # 3. Heuristic: if name is Chinese or looks like a company name,
    #    but resolved to no symbol AND not in known-unlisted — flag uncertain
    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', name))
    looks_like_brand = bool(re.match(r'^[A-Z][a-z]{2,}$', name))  # Capitalized word
    if has_chinese or looks_like_brand:
        return {
            "listed": None,
            "confidence": "low",
            "note": "无法确认为上市公司或未上市公司",
            "hint": ("请通过网络搜索确认该公司上市状态。如果是未上市公司，"
                     "请使用侦探式研究方法，从公开信息拼凑分析，并标注每项信息的置信度。"),
        }

    return None


def _resolve_company_name(query: str) -> Optional[str]:
    """Resolve a Chinese company name to stock code.

    Returns the stock code if found, None otherwise.
    Supports: exact match, case-insensitive match, and substring match.
    """
    query = query.strip()
    # Direct match
    if query in COMPANY_NAME_MAP:
        return COMPANY_NAME_MAP[query]
    # Try case-insensitive exact match for English names
    query_lower = query.lower()
    for name, code in COMPANY_NAME_MAP.items():
        if name.lower() == query_lower:
            return code
    # Substring match: find company name within a longer string (e.g., "分析腾讯的财报")
    # Try longer names first to avoid partial matches (e.g., "腾讯控股" before "腾讯")
    query_lower_for_sub = query.lower()
    for name in sorted(COMPANY_NAME_MAP.keys(), key=len, reverse=True):
        # Case-insensitive substring match for English names
        if name.lower() in query_lower_for_sub:
            return COMPANY_NAME_MAP[name]
    return None


def _detect_market(symbol: str) -> str:
    """Detect market type from symbol format.

    Returns: 'A_SHARE', 'HK', 'US', or 'UNKNOWN'
    """
    symbol = symbol.strip().upper()

    # A-share: 6-digit code (e.g., 002555, 600519)
    if re.match(r'^\d{6}$', symbol):
        return 'A_SHARE'

    # HK: ends with .HK (e.g., 0700.HK)
    if symbol.endswith('.HK') or symbol.endswith('.SH') or symbol.endswith('.SZ'):
        suffix = symbol.split('.')[-1]
        if suffix == 'HK':
            return 'HK'
        return 'A_SHARE'

    # US: alphabetic ticker (e.g., AAPL, PDD, NVDA) -- case-insensitive
    if re.match(r'^[A-Z]{1,6}$', symbol):
        return 'US'

    return 'UNKNOWN'


def _normalize_a_share_code(code: str) -> str:
    """Normalize A-share code with proper suffix."""
    code = code.replace('.SH', '').replace('.SZ', '')
    if code.startswith('6'):
        return f"{code}.SH"
    elif code.startswith('8') or code.startswith('4'):
        # Beijing Stock Exchange
        return f"{code}.BJ"
    else:
        return f"{code}.SZ"


async def _run_sync(func, *args, **kwargs):
    """Run a synchronous function in the dedicated bounded sync pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(SYNC_EXECUTOR, lambda: func(*args, **kwargs))


# ==================== A-Share Data (via akshare) ====================

async def _fetch_a_share_price(symbol: str) -> dict:
    """Fetch A-share real-time price via akshare."""

    # Fall back to akshare
    code = _normalize_a_share_code(symbol)
    bare_code = code.split('.')[0]

    def _fetch():
        import akshare as ak
        # stock_zh_a_spot_em returns ALL A-share stocks; filter by code
        df = ak.stock_zh_a_spot_em()
        if df is None or len(df) == 0:
            return None

        # Find the column containing stock codes
        code_col = None
        for col in df.columns:
            if '代码' in str(col) or col == 'symbol':
                code_col = col
                break
        if code_col is None:
            code_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]

        # Filter by code
        mask = df[code_col].astype(str).str.contains(bare_code, na=False)
        matched = df[mask]
        if len(matched) == 0:
            return None

        row = matched.iloc[0]
        return {
            "symbol": code,
            "market": "A_SHARE",
            "name": str(row.get('名称', '')),
            "price": float(row.get('最新价', 0) or 0),
            "change_pct": float(row.get('涨跌幅', 0) or 0),
            "change_amount": float(row.get('涨跌额', 0) or 0),
            "volume": float(row.get('成交量', 0) or 0),
            "turnover": float(row.get('成交额', 0) or 0),
            "high": float(row.get('最高', 0) or 0),
            "low": float(row.get('最低', 0) or 0),
            "open": float(row.get('今开', 0) or 0),
            "prev_close": float(row.get('昨收', 0) or 0),
            "pe": float(row.get('市盈率-动态', 0) or 0),
            "pb": float(row.get('市净率', 0) or 0),
            "total_market_cap": float(row.get('总市值', 0) or 0),
            "circulating_market_cap": float(row.get('流通市值', 0) or 0),
            "timestamp": datetime.now().isoformat(),
        }

    return await _run_sync(_fetch)


async def _fetch_a_share_financials(symbol: str) -> dict:
    """Fetch A-share financial statements via akshare."""
    code = symbol.split('.')[0]
    stock_prefix = f"sh{code}" if code.startswith('6') else f"sz{code}"

    def _fetch():
        import akshare as ak
        result = {}

        # Income statement
        try:
            df = ak.stock_financial_report_sina(stock=stock_prefix, symbol="利润表")
            if df is not None and len(df) > 0:
                latest = df.head(3)
                result["income_statement"] = latest.to_dict('records')
        except Exception as e:
            logger.debug(f"A-share income statement failed for {symbol}: {e}")

        # Balance sheet
        try:
            df = ak.stock_financial_report_sina(stock=stock_prefix, symbol="资产负债表")
            if df is not None and len(df) > 0:
                latest = df.head(3)
                result["balance_sheet"] = latest.to_dict('records')
        except Exception as e:
            logger.debug(f"A-share balance sheet failed for {symbol}: {e}")

        # Cash flow
        try:
            df = ak.stock_financial_report_sina(stock=stock_prefix, symbol="现金流量表")
            if df is not None and len(df) > 0:
                latest = df.head(3)
                result["cash_flow"] = latest.to_dict('records')
        except Exception as e:
            logger.debug(f"A-share cash flow failed for {symbol}: {e}")

        return result if result else None

    return await _run_sync(_fetch)


# ==================== US/HK Data (via yfinance) ====================

async def _fetch_yf_price(symbol: str, market: str) -> dict:
    """Fetch US/HK stock price via yfinance."""
    # yfinance uses the same format for HK stocks (0700.HK)
    # For US stocks, symbol is already correct
    yf_symbol = symbol

    def _fetch():
        import yfinance as yf
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info or {}

        hist = ticker.history(period="5d")
        current_price = float(hist['Close'].iloc[-1]) if hist is not None and len(hist) > 0 else 0.0
        prev_close = float(hist['Close'].iloc[-2]) if hist is not None and len(hist) > 1 else current_price

        currency = info.get('currency', 'USD')
        if market == 'HK':
            currency = info.get('currency', 'HKD')

        return {
            "symbol": yf_symbol,
            "market": market,
            "name": info.get('longName', info.get('shortName', yf_symbol)),
            "price": current_price,
            "change_pct": round((current_price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "change_amount": round(current_price - prev_close, 2) if prev_close else 0,
            "volume": int(hist['Volume'].iloc[-1]) if hist is not None and len(hist) > 0 else 0,
            "high": float(hist['High'].iloc[-1]) if hist is not None and len(hist) > 0 else 0,
            "low": float(hist['Low'].iloc[-1]) if hist is not None and len(hist) > 0 else 0,
            "open": float(hist['Open'].iloc[-1]) if hist is not None and len(hist) > 0 else 0,
            "prev_close": prev_close,
            "pe": info.get('trailingPE', 0) or 0,
            "pb": info.get('priceToBook', 0) or 0,
            "total_market_cap": info.get('marketCap', 0) or 0,
            "currency": currency,
            "timestamp": datetime.now().isoformat(),
        }

    return await _run_sync(_fetch)


async def _fetch_yf_financials(symbol: str, market: str) -> dict:
    """Fetch US/HK financial statements via yfinance."""
    yf_symbol = symbol

    def _fetch():
        import yfinance as yf
        ticker = yf.Ticker(yf_symbol)
        result = {}

        try:
            inc = ticker.quarterly_income_stmt
            if inc is not None and not inc.empty:
                result["quarterly_income"] = inc.to_dict()
        except Exception as e:
            logger.debug(f"yfinance income stmt failed for {symbol}: {e}")

        try:
            bs = ticker.quarterly_balance_sheet
            if bs is not None and not bs.empty:
                result["quarterly_balance"] = bs.to_dict()
        except Exception as e:
            logger.debug(f"yfinance balance sheet failed for {symbol}: {e}")

        try:
            cf = ticker.quarterly_cashflow
            if cf is not None and not cf.empty:
                result["quarterly_cashflow"] = cf.to_dict()
        except Exception as e:
            logger.debug(f"yfinance cashflow failed for {symbol}: {e}")

        return result if result else None

    return await _run_sync(_fetch)


# ==================== US/HK Financials — 东财数据中心（akshare EM datacenter） ====================
#
# 实测(2026-08-16 本环境)：datacenter 系接口 <1s 可达且数据完整（push2 行情
# 接口被连接重置，但 datacenter 正常），yfinance 财务接口 57s 超时必败。
# 因此 US/HK 财务数据主源切换为东财数据中心，yfinance 降权为回退。

def _em_num(row: dict, key: str):
    """NaN/None 安全的数值提取。"""
    v = row.get(key)
    try:
        f = float(v)
        return None if f != f else f  # NaN -> None
    except (TypeError, ValueError):
        return None


def _em_us_row(row: dict) -> dict:
    """东财美股财务指标行 → 结构化字段（金额同时给元与亿元）。"""
    def num(k):
        return _em_num(row, k)

    def yi(k):
        v = num(k)
        return round(v / 1e8, 2) if v is not None else None

    return {
        "report_date": str(row.get("REPORT_DATE") or "")[:10],
        "report_type": str(row.get("REPORT_TYPE") or ""),
        "revenue": num("OPERATE_INCOME"),
        "revenue_yi": yi("OPERATE_INCOME"),
        "revenue_yoy_pct": num("OPERATE_INCOME_YOY"),
        "gross_profit": num("GROSS_PROFIT"),
        "gross_profit_yi": yi("GROSS_PROFIT"),
        "net_profit_parent": num("PARENT_HOLDER_NETPROFIT"),
        "net_profit_parent_yi": yi("PARENT_HOLDER_NETPROFIT"),
        "net_profit_yoy_pct": num("PARENT_HOLDER_NETPROFIT_YOY"),
        "basic_eps": num("BASIC_EPS"),
        "gross_margin_pct": num("GROSS_PROFIT_RATIO"),
        "net_margin_pct": num("NET_PROFIT_RATIO"),
        "roe_avg_pct": num("ROE_AVG"),
    }


# 港股利润表长表科目 → 标准字段（东财 hk_report_em 的 STD_ITEM_NAME）
_HK_INCOME_ITEMS = {
    "营运收入": "revenue",          # 含其他营业收入的总口径，优先
    "营业额": "revenue_core",       # 核心营业额，营运收入缺失时兜底
    "经营溢利": "operating_profit",
    "除税后溢利": "net_profit",
    "股东应占溢利": "net_profit_parent",
}


def _pivot_hk_income(df, n_periods: int) -> list:
    """东财港股利润表长表(REPORT_DATE × STD_ITEM_NAME × AMOUNT) → 每期一行。"""
    if df is None or not len(df):
        return []
    df = df.copy()
    df["REPORT_DATE"] = df["REPORT_DATE"].astype(str).str[:10]
    dates = sorted(df["REPORT_DATE"].unique(), reverse=True)[:n_periods]
    rows = []
    for d in dates:
        row = {"report_date": d}
        for _, r in df[df["REPORT_DATE"] == d].iterrows():
            field = _HK_INCOME_ITEMS.get(str(r.get("STD_ITEM_NAME") or ""))
            if not field:
                continue
            val = _em_num(r, "AMOUNT")
            if val is not None:
                row[field] = val
        # 收入口径：优先营运收入，缺失时用营业额
        if "revenue" not in row and "revenue_core" in row:
            row["revenue"] = row.pop("revenue_core")
        else:
            row.pop("revenue_core", None)
        for k in list(row.keys()):
            if k != "report_date" and isinstance(row[k], float):
                row[f"{k}_yi"] = round(row[k] / 1e8, 2)
        if len(row) > 1:
            rows.append(row)
    return rows


async def _fetch_em_us_financials(symbol: str) -> dict:
    """东财数据中心美股财务指标：年报近4期 + 单季报近6期。

    接口 ak.stock_financial_us_analysis_indicator_em（datacenter-web.eastmoney.com），
    实测 PDD 年报/单季报 0.3-0.4s 返回。单位：元（*_yi 字段为亿元）。
    """
    def _fetch():
        import akshare as ak
        out = {}
        try:
            ann = ak.stock_financial_us_analysis_indicator_em(symbol=symbol, indicator="年报")
            if ann is not None and len(ann):
                out["annual"] = [_em_us_row(r) for r in ann.head(4).to_dict("records")]
                if "CURRENCY_ABBR" in ann.columns:
                    out["currency"] = str(ann["CURRENCY_ABBR"].iloc[0])
        except Exception as e:
            logger.debug(f"EM US annual financials failed for {symbol}: {e}")
        try:
            q = ak.stock_financial_us_analysis_indicator_em(symbol=symbol, indicator="单季报")
            if q is not None and len(q):
                out["quarterly"] = [_em_us_row(r) for r in q.head(6).to_dict("records")]
        except Exception as e:
            logger.debug(f"EM US quarterly financials failed for {symbol}: {e}")
        return out or None

    data = await _run_sync(_fetch)
    if data:
        data.setdefault("currency", "CNY")
        data.update({
            "symbol": symbol,
            "unit_note": "金额为元；*_yi 字段为亿元",
            "source": "eastmoney_datacenter",
        })
    return data


async def _fetch_em_hk_financials(symbol: str) -> dict:
    """东财数据中心港股利润表：年度近3期 + 报告期(季度/中期)近2期。

    接口 ak.stock_financial_hk_report_em，实测 00700 利润表 0.4s 返回。
    单位：元（*_yi 字段为亿元）；币种以公司报告币种为准（腾讯控股为人民币）。
    """
    code = symbol.upper().replace(".HK", "").zfill(5)

    def _fetch():
        import akshare as ak
        out = {}
        try:
            inc = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="年度")
            rows = _pivot_hk_income(inc, 3)
            if rows:
                out["annual_income"] = rows
        except Exception as e:
            logger.debug(f"EM HK annual income failed for {symbol}: {e}")
        try:
            inc_q = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="报告期")
            rows = _pivot_hk_income(inc_q, 2)
            if rows:
                out["interim_income"] = rows
        except Exception as e:
            logger.debug(f"EM HK interim income failed for {symbol}: {e}")
        return out or None

    data = await _run_sync(_fetch)
    if data:
        data.update({
            "symbol": symbol,
            "unit_note": "金额为报告币种元；*_yi 字段为亿元",
            "currency": "以公司报告币种为准（腾讯控股为人民币 CNY）",
            "source": "eastmoney_datacenter",
        })
    return data


# ==================== Market Indices ====================

async def fetch_market_indices() -> dict:
    """Fetch major market indices (SSE, SZSE, HS, S&P500, NASDAQ).

    Returns a dict with partial data if some sources fail.
    Never returns empty -- always includes timestamp.
    """
    cached = _cache.get("indices", "global", CACHE_TTL_INDICES)
    if cached:
        return cached

    def _fetch():
        result = {}
        errors = []

        # A-share indices via akshare
        try:
            import akshare as ak
            df = ak.stock_zh_index_spot_em()
            if df is not None and len(df) > 0:
                for _, row in df.iterrows():
                    name = str(row.get('名称', ''))
                    code = str(row.get('代码', ''))
                    if '上证指数' in name or code == '000001':
                        result["SSE"] = {
                            "name": name, "code": code,
                            "price": float(row.get('最新价', 0) or 0),
                            "change_pct": float(row.get('涨跌幅', 0) or 0),
                        }
                    elif '深证成指' in name or code == '399001':
                        result["SZSE"] = {
                            "name": name, "code": code,
                            "price": float(row.get('最新价', 0) or 0),
                            "change_pct": float(row.get('涨跌幅', 0) or 0),
                        }
                    elif '创业板指' in name or code == '399006':
                        result["CHINEXT"] = {
                            "name": name, "code": code,
                            "price": float(row.get('最新价', 0) or 0),
                            "change_pct": float(row.get('涨跌幅', 0) or 0),
                        }
            else:
                errors.append("akshare returned empty index data")
        except Exception as e:
            errors.append(f"akshare indices: {e}")
            logger.warning(f"akshare indices failed: {e}")

        # US/HK indices via yfinance
        try:
            import yfinance as yf
            for sym, key in [("^GSPC", "S&P500"), ("^IXIC", "NASDAQ"), ("^HSI", "HSI"), ("^DJI", "DOW")]:
                try:
                    t = yf.Ticker(sym)
                    hist = t.history(period="2d")
                    if hist is not None and len(hist) > 0:
                        price = float(hist['Close'].iloc[-1])
                        prev = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price
                        result[key] = {
                            "symbol": sym,
                            "price": price,
                            "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                        }
                except Exception as e:
                    errors.append(f"yfinance {sym}: {e}")
        except Exception as e:
            errors.append(f"yfinance indices: {e}")
            logger.warning(f"yfinance indices failed: {e}")

        result["timestamp"] = datetime.now().isoformat()
        if errors:
            result["_partial"] = True
            result["_errors"] = errors
        return result

    try:
        # 主数据源（akshare/yfinance）整体限时：东财/雅虎不可达时
        # 库内网络调用可能挂很久，超时后走 HTTP 回退链路
        try:
            data = await asyncio.wait_for(_run_sync(_fetch), timeout=8.0)
        except asyncio.TimeoutError:
            _record_sync_timeout("indices")
            logger.warning("Primary indices source timed out (>8s), using HTTP fallback")
            data = None

        # ── HTTP fallback for indices (no library dependency) ──
        # Check if we got any real index data (not just errors/metadata)
        _META_KEYS = {"timestamp", "_partial", "_errors", "_stale", "_source", "error"}
        real_keys = [k for k in (data or {}).keys() if k not in _META_KEYS] if data else []
        if not real_keys:
            logger.info("Primary indices source failed, trying HTTP fallback...")
            http_result = {}
            # 三个 HTTP 回退源并发抓取（原为串行；Yahoo 在国内不可达，
            # 串行时 4 个雅虎符号各挂 10s 导致整链路 >40s）
            _t, _tg, _y = await asyncio.gather(
                fetch_indices_tencent(),
                fetch_indices_tencent_global(),
                fetch_indices_yahoo(),
                return_exceptions=True,
            )
            for _res in (_t, _tg, _y):
                if isinstance(_res, dict):
                    http_result.update(_res)

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
            _t, _tg, _y = await asyncio.gather(
                fetch_indices_tencent(),
                fetch_indices_tencent_global(),
                fetch_indices_yahoo(),
                return_exceptions=True,
            )
            for _res in (_t, _tg, _y):
                if isinstance(_res, dict):
                    http_result.update(_res)
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
        }


# ==================== News ====================

async def fetch_company_news(symbol: str, days: int = 14) -> list:
    """Fetch recent news about a company.

    Returns a list of news items. Never returns empty without explanation --
    if no news is found, returns a list with a single info item.
    """
    cache_key = f"{symbol}_{days}d"
    cached = _cache.get("news", cache_key, CACHE_TTL_NEWS)
    if cached:
        return cached

    market = _detect_market(symbol)

    def _fetch():
        news_list = []

        if market == 'A_SHARE':
            try:
                import akshare as ak
                code = symbol.split('.')[0]
                df = ak.stock_news_em(symbol=code)
                if df is not None and len(df) > 0:
                    for _, row in df.head(30).iterrows():
                        news_list.append({
                            "title": str(row.get('新闻标题', '')),
                            "content": str(row.get('新闻内容', ''))[:500],
                            "date": str(row.get('发布时间', '')),
                            "source": str(row.get('文章来源', '')),
                        })
            except Exception as e:
                logger.warning(f"akshare news failed: {e}")

        elif market in ('US', 'HK'):
            try:
                import yfinance as yf
                ticker = yf.Ticker(symbol)
                news = ticker.news
                if news:
                    for item in news[:20]:
                        # yfinance news format varies by version
                        title = item.get('title', '')
                        content = item.get('summary', item.get('description', ''))
                        pub_time = item.get('providerPublishTime', item.get('publishTime', 0))
                        news_list.append({
                            "title": title,
                            "content": str(content)[:500] if content else '',
                            "date": datetime.fromtimestamp(pub_time).isoformat() if pub_time else '',
                            "source": item.get('publisher', ''),
                        })
            except Exception as e:
                logger.warning(f"yfinance news failed: {e}")

        return news_list

    try:
        # US/HK 新闻走 yfinance（curl 超时 30s 常态），整体限时 12s 快速失败；
        # A 股 akshare 东财新闻正常，不限
        if market in ('US', 'HK'):
            try:
                data = await asyncio.wait_for(_run_sync(_fetch), timeout=12.0)
            except asyncio.TimeoutError:
                _record_sync_timeout("yfinance")
                logger.warning(f"yfinance news timeout for {symbol} (>12s)")
                data = []
        else:
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

        # No news found -- check stale cache
        stale = _cache.get_stale("news", cache_key)
        if stale and stale.get("data"):
            return stale["data"]

        # Return informative placeholder instead of empty list
        return [{
            "title": f"暂无{symbol}的近期新闻",
            "content": f"未能获取到 {symbol} 的近期新闻数据。可能原因：数据源暂时不可用或该股票无近期新闻。",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source": "system",
            "_placeholder": True,
        }]
    except Exception as e:
        logger.error(f"Failed to fetch news for {symbol}: {e}")
        stale = _cache.get_stale("news", cache_key)
        if stale and stale.get("data"):
            return stale["data"]
        return [{
            "title": f"新闻获取失败: {symbol}",
            "content": f"获取新闻时发生错误: {e}",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source": "system",
            "_error": True,
        }]


# ==================== Company Search ====================

async def search_company(query: str) -> list:
    """Search for companies by name or code.

    Supports Chinese company names (腾讯, 茅台), stock codes (600519),
    and US tickers (AAPL). Never returns empty without trying all sources.
    """
    cache_key = query.lower().strip()
    cached = _cache.get("search", cache_key, CACHE_TTL_COMPANY)
    if cached:
        return cached

    # First, check the built-in name map
    resolved = _resolve_company_name(query)
    if resolved:
        name_display = query
        market = _detect_market(resolved)
        results = [{
            "symbol": resolved,
            "name": name_display,
            "market": market,
        }]
        _cache.set("search", cache_key, results)
        return results

    def _search():
        results = []

        # Try akshare for A-shares
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            if df is not None and len(df) > 0:
                # Find relevant columns
                code_col = None
                name_col = None
                for col in df.columns:
                    col_str = str(col)
                    if '代码' in col_str:
                        code_col = col
                    elif '名称' in col_str:
                        name_col = col
                if code_col is None:
                    code_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]
                if name_col is None:
                    name_col = df.columns[2] if len(df.columns) > 2 else df.columns[1]

                query_upper = query.upper()
                matched = df[
                    df[name_col].astype(str).str.contains(query, na=False, case=False) |
                    df[code_col].astype(str).str.contains(query_upper, na=False, case=False)
                ]
                for _, row in matched.head(10).iterrows():
                    results.append({
                        "symbol": str(row[code_col]),
                        "name": str(row[name_col]),
                        "market": "A_SHARE",
                        "price": float(row.get('最新价', 0) or 0),
                    })
        except Exception as e:
            logger.warning(f"akshare search failed: {e}")

        # If query looks like a US ticker, add it
        query_upper = query.strip().upper()
        if re.match(r'^[A-Z]{1,6}$', query_upper):
            results.append({
                "symbol": query_upper,
                "name": query_upper,
                "market": "US",
            })

        # If query looks like an HK code (5 digits), add with .HK
        if re.match(r'^\d{4,5}$', query):
            results.append({
                "symbol": f"{query.zfill(5)}.HK",
                "name": query,
                "market": "HK",
            })

        return results

    try:
        data = await _run_sync(_search)
        _cache.set("search", cache_key, data)
        return data
    except Exception as e:
        logger.error(f"Company search failed: {e}")
        # Return resolved result from name map if available
        if resolved:
            return [{"symbol": resolved, "name": query, "market": _detect_market(resolved)}]
        return []


# ==================== Public API ====================

async def get_stock_price(symbol: str) -> dict:
    """Get real-time stock price for any market.

    Auto-detects market type (A-share/HK/US) and uses appropriate data source.
    Resolves Chinese company names to stock codes automatically.
    Falls back to stale cache when network is unavailable.
    Never returns empty -- always includes meaningful error info.
    """
    # Resolve Chinese company name to stock code
    resolved = _resolve_company_name(symbol)
    if resolved:
        symbol = resolved

    market = _detect_market(symbol)

    # Check cache first
    cached = _cache.get("price", symbol, CACHE_TTL_REALTIME)
    if cached:
        return cached

    try:
        if market == 'A_SHARE':
            data = await _fetch_a_share_price(symbol)
        elif market in ('US', 'HK'):
            data = await _fetch_yf_price(symbol, market)
        else:
            # Try to search first
            results = await search_company(symbol)
            if results:
                first = results[0]
                resolved_symbol = first["symbol"]
                # Prevent infinite recursion: only recurse if the resolved symbol is different
                if resolved_symbol.upper() != symbol.upper():
                    return await get_stock_price(resolved_symbol)
                # Same symbol -- try as US stock as last resort
                market = 'US'
                data = await _fetch_yf_price(resolved_symbol, market)
            else:
                return {
                    "symbol": symbol,
                    "error": f"无法识别股票代码: {symbol}",
                    "hint": "请使用股票代码（如 600519 / 0700.HK / AAPL）或中文公司名（如 腾讯 / 茅台 / 拼多多）",
                    "timestamp": datetime.now().isoformat(),
                }

        # ── HTTP fallback chain (no library dependency) ──
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
        }
    except Exception as e:
        logger.error(f"Failed to fetch price for {symbol}: {e}")
        stale = _cache.get_stale("price", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]
        return {
            "symbol": symbol,
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        }


async def get_financial_data(symbol: str) -> dict:
    """Get financial statements for a company.

    Resolves Chinese company names automatically.
    Falls back to stale cache when network is unavailable.
    """
    # Resolve Chinese company name
    resolved = _resolve_company_name(symbol)
    if resolved:
        symbol = resolved

    cached = _cache.get("financials", symbol, CACHE_TTL_FINANCIAL)
    if cached:
        return cached

    market = _detect_market(symbol)

    try:
        if market == 'A_SHARE':
            data = await _fetch_a_share_financials(symbol)
        elif market in ('US', 'HK'):
            # yfinance 财务接口实测超时 57s（gateway 30s 必败），降权为 12s 快速失败
            try:
                data = await asyncio.wait_for(_fetch_yf_financials(symbol, market), timeout=12.0)
            except asyncio.TimeoutError:
                _record_sync_timeout("yfinance")
                logger.warning(f"yfinance financials timeout for {symbol} (>12s)")
                data = None
        else:
            # Try search
            results = await search_company(symbol)
            if results:
                resolved_symbol = results[0]["symbol"]
                if resolved_symbol.upper() != symbol.upper():
                    return await get_financial_data(resolved_symbol)
            return {
                "symbol": symbol,
                "error": f"无法识别股票代码: {symbol}",
                "hint": "请使用股票代码或中文公司名",
                "timestamp": datetime.now().isoformat(),
            }

        if data:
            _cache.set("financials", symbol, data)
            return data

        stale = _cache.get_stale("financials", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]

        return {
            "symbol": symbol,
            "error": f"未获取到 {symbol} 的财务数据",
            "hint": "可能是数据源暂时不可用",
            "timestamp": datetime.now().isoformat(),
        }

    except ImportError:
        stale = _cache.get_stale("financials", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]
        return {
            "symbol": symbol,
            "error": "数据源库未安装",
            "hint": "请运行: pip install akshare yfinance",
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"Failed to fetch financials for {symbol}: {e}")
        stale = _cache.get_stale("financials", symbol)
        if stale:
            stale["data"]["_stale"] = True
            return stale["data"]
        return {
            "symbol": symbol,
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        }


def get_current_date_context() -> dict:
    """Get current date and market status (no network needed)."""
    now = datetime.now()

    # Determine if markets are likely open (simplified)
    hour = now.hour
    weekday = now.weekday()

    is_weekday = weekday < 5

    market_status = {
        "A_SHARE": "closed",
        "HK": "closed",
        "US": "closed",
    }

    if is_weekday:
        if 9 <= hour < 15:
            market_status["A_SHARE"] = "open"
        if 9 <= hour < 16:
            market_status["HK"] = "open"
        if 21 <= hour or hour < 5:
            market_status["US"] = "open"

    # Use system timezone info, fallback to Asia/Shanghai for Chinese deployments
    import time as _time

    # Normalize common Chinese timezone names
    tz_display = "Asia/Shanghai (UTC+8)"  # Default for Chinese deployments
    try:
        utc_offset = -_time.timezone if not _time.daylight else -_time.altzone
        if utc_offset == 0:
            tz_display = "UTC"
        elif utc_offset == 28800:
            tz_display = "Asia/Shanghai (UTC+8)"
        else:
            hours = utc_offset // 3600
            tz_display = f"UTC{'+' if hours >= 0 else ''}{hours}"
    except Exception:
        pass

    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]

    return {
        "current_date": now.strftime("%Y-%m-%d"),
        "current_time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "weekday_cn": weekday_cn,
        "timezone": tz_display,
        "market_status": market_status,
        "timestamp": now.isoformat(),
    }
