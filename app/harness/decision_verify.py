"""P4: Decision-log verification — backfill prices & validate conclusions.

Decision log (decision-log.md) records investment conclusions at decision
time.  This module closes the loop:

1. backfill_missing_prices(): 老条目(缺代码/记录价)按决策时点拉历史K线
   收盘价回填 — 让验证对历史决策立即生效。
2. verify_pending_decisions(): 对照真实行情自动标记 已兑现/已证伪/已过期。

Both are async (they call market-data tools / network).  Failures are
per-entry and never fatal — a single bad entry is skipped, not an error.

P4-闭环(2026-09-01).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .decision_log import list_decisions, update_entry_fields

logger = logging.getLogger("ai_berkshire.harness.decision_verify")

# 验证阈值: 收益达 ±8% 判兑现/证伪(避开噪声)
VERIFY_THRESHOLD = 0.08
# 决策超 90 天仍无定论 → 过期
VERIFY_MAX_AGE_DAYS = 90
# 历史K线回填窗口: 决策日往前 N 天找最近交易日收盘
BACKFILL_LOOKBACK_DAYS = 7


# ==================== 代码解析 ====================

def _is_code(text: str) -> bool:
    """True if text looks like a raw stock code (600000 / 0700.HK / PDD)."""
    t = text.strip().upper()
    return bool(re.match(r"^\d{4,6}$", t) or re.match(r"^\d{4,6}\.[A-Z]{2,3}$", t) or re.match(r"^[A-Z]{1,5}$", t))


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse '2026-08-06 16:32 UTC' → aware datetime. None on failure."""
    try:
        return datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _resolve_code(stock: str, title_code: str = "") -> Optional[str]:
    """Resolve entry to a stock code.

    Priority: title already a code → built-in company map → None
    (caller falls back to async search).  Returns None when unresolvable
    locally.
    """
    # Title itself is a code (e.g. "688981", "0700.HK")
    if _is_code(title_code):
        return title_code.upper()
    if _is_code(stock):
        return stock.upper()
    # Built-in company map (zero network)
    try:
        from ..tools.market_data import _resolve_company_name
        code = _resolve_company_name(stock)
        if code:
            return code
    except Exception:
        pass
    return None


# ==================== 历史价获取 ====================

def _tencent_symbol(code: str) -> Optional[str]:
    """Convert stock code to Tencent kline symbol (sh600519 / hk00700 / usAAPL)."""
    c = code.upper()
    if re.match(r"^\d{4,6}\.(SH|SZ|BJ)$", c):
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[c.split(".")[1]]
        return f"{prefix}{c.split('.')[0]}"
    if re.match(r"^\d{4,6}$", c):  # bare A-share code — assume SH if 6xx, else SZ
        return f"{'sh' if c.startswith(('6', '9')) else 'sz'}{c}"
    if re.match(r"^\d{4,5}\.HK$", c):
        return f"hk{int(c.split('.')[0]):05d}"
    if re.match(r"^[A-Z]{1,5}$", c):  # US ticker
        return f"us{c}"
    return None


def _sina_a_symbol(code: str) -> Optional[str]:
    c = code.upper()
    if re.match(r"^\d{4,6}\.(SH|SZ|BJ)$", c):
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[c.split(".")[1]]
        return f"{prefix}{c.split('.')[0]}"
    if re.match(r"^\d{4,6}$", c):
        return f"{'sh' if c.startswith(('6', '9')) else 'sz'}{c}"
    return None


async def _fetch_tencent_hist_close(symbol: str, day: str) -> Optional[float]:
    """Close of the nearest trading day at/before ``day`` via Tencent kline."""
    import httpx
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10.0,
        ) as client:
            resp = await client.get(
                "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": f"{symbol},day,,,60,qfq"},
            )
            data = resp.json()
        node = data.get("data", {}).get(symbol, {})
        bars = node.get("qfqday") or node.get("day") or []
        for bar in reversed(bars):
            date = bar[0]
            close = bar[2]
            if date <= day and close is not None:
                return float(close)
        return None
    except Exception as e:
        logger.debug(f"Tencent kline failed for {symbol}: {e}")
        return None


async def _fetch_sina_a_hist_close(symbol: str, day: str) -> Optional[float]:
    """Close of the nearest trading day at/before ``day`` via Sina A-share kline."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": 60},
            )
            rows = resp.json()
        for row in reversed(rows or []):
            date = row.get("day", "")
            close = row.get("close")
            if date <= day and close is not None:
                return float(close)
        return None
    except Exception as e:
        logger.debug(f"Sina kline failed for {symbol}: {e}")
        return None


async def _fetch_sina_us_hist_close(symbol: str, day: str) -> Optional[float]:
    """Close of the nearest trading day at/before ``day`` via Sina US kline."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/var%20t=/US_MinKService.getDailyK",
                params={"symbol": symbol, "___qn": 3},
            )
            text = resp.text
        m = re.search(r"\((\[.*\])\)", text, re.S)
        if not m:
            return None
        rows = json.loads(m.group(1))
        for row in reversed(rows or []):
            date = row.get("d", "")
            close = row.get("c")
            if date <= day and close is not None:
                return float(close)
        return None
    except Exception as e:
        logger.debug(f"Sina US kline failed for {symbol}: {e}")
        return None


async def _fetch_historical_price(code: str, asof: datetime) -> Optional[float]:
    """Close price of the nearest trading day at/before ``asof`` (UTC).

    A-shares: Tencent kline (UA header) → Sina A-share kline.
    HK: Tencent kline.  US: Sina US kline → Tencent.
    东财系(akshare 历史K线底层即东财)在本机不可用, 不使用。
    """
    day = asof.strftime("%Y-%m-%d")

    tsym = _tencent_symbol(code)
    ssym = _sina_a_symbol(code)

    # A-share: Tencent → Sina
    if tsym and tsym.startswith(("sh", "sz", "bj")):
        price = await _fetch_tencent_hist_close(tsym, day)
        if price is not None:
            return price
        if ssym:
            return await _fetch_sina_a_hist_close(ssym, day)
        return None

    # HK: Tencent only
    if tsym and tsym.startswith("hk"):
        return await _fetch_tencent_hist_close(tsym, day)

    # US: Sina → Tencent
    if tsym and tsym.startswith("us"):
        price = await _fetch_sina_us_hist_close(tsym[2:], day)
        if price is not None:
            return price
        return await _fetch_tencent_hist_close(tsym, day)

    return None


# ==================== 回填 ====================

async def backfill_missing_prices(limit: int = 50) -> dict:
    """Backfill code/record_price for entries missing them.

    Returns a summary dict {backfilled, skipped, retry_later, failed} for
    logging.  Entries whose code search fails with a network error are
    left untouched (retry_later) so a later run can try again — a network
    outage must never permanently skip an entry.
    """
    entries = list_decisions(max_entries=1000)
    summary = {"backfilled": 0, "skipped": 0, "retry_later": 0, "failed": 0}
    processed = 0

    for e in entries:
        if processed >= limit:
            break
        # Only entries that still need backfill
        has_code = e.get("code") and e.get("code") != "未指定"
        has_price = e.get("record_price") and e.get("record_price") != "未指定"
        if has_code and has_price:
            continue
        if e.get("verify") not in ("待验证", ""):
            continue  # already resolved / skipped

        processed += 1
        task_id = e.get("task_id") or ""
        stock = e.get("stock") or ""
        asof = _parse_ts(e.get("time") or "")
        if not asof:
            update_entry_fields(task_id, {"验证": "跳过:时间无法解析"})
            summary["skipped"] += 1
            continue

        # 1) resolve code: local map first, then async search
        code = _resolve_code(stock)
        if not code:
            try:
                from ..tools.market_data import search_company
                results = await search_company(stock)
            except Exception as e:
                logger.debug(f"search_company failed for {stock}: {e}")
                # 网络失败 → 不落盘, 下次重试
                summary["retry_later"] += 1
                continue
            if results:
                code = results[0].get("symbol")
        if not code:
            update_entry_fields(task_id, {"验证": "跳过:无法解析代码"})
            summary["skipped"] += 1
            continue

        # 2) fetch price at decision time
        price = await _fetch_historical_price(code, asof)
        if price is None:
            update_entry_fields(
                task_id,
                {"代码": code, "验证": f"跳过:行情不可用({asof.strftime('%Y-%m-%d')})"},
            )
            summary["skipped"] += 1
            continue

        # 3) write back
        update_entry_fields(
            task_id,
            {"代码": code, "记录价": f"{price:.2f}", "验证": "待验证"},
        )
        summary["backfilled"] += 1
        logger.info(f"Backfilled {stock}: {code} @ {price:.2f} ({asof.strftime('%Y-%m-%d')})")

    return summary


# ==================== 验证判定 ====================

def _classify_conclusion(conclusion: str) -> str:
    """Map conclusion text to a direction: buy / sell / hold / unknown."""
    c = conclusion or ""
    if any(k in c for k in ("买入", "增持", "加仓", "重仓", "看多", "乐观")):
        return "buy"
    if any(k in c for k in ("卖出", "减持", "减仓", "回避", "看空", "悲观", "清仓")):
        return "sell"
    if any(k in c for k in ("持有", "观望", "等待", "中性", "HOLD", "hold")):
        return "hold"
    return "unknown"


def _extract_target_price(target: str) -> Optional[float]:
    """Extract a numeric target price from the target field."""
    t = target or ""
    m = re.search(r"(\d+(?:\.\d+)?)", t.replace(",", ""))
    if not m:
        return None
    # HK$639 / 639 / 200元 all fine — relative comparison only
    return float(m.group(1))


def judge_entry(e: dict, current_price: float, now: Optional[datetime] = None) -> str:
    """Decide verify status for one entry given current price.

    Returns one of: 已兑现 / 已证伪 / 已过期 / 待验证.
    Pure function — no I/O, unit-test friendly.
    """
    now = now or datetime.now(timezone.utc)
    record_price = e.get("record_price")
    try:
        base = float(record_price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "待验证"  # no base price → cannot judge

    # Age check: 超90天仍无定论 → 过期(只对结论尚未验证的条目)
    asof = _parse_ts(e.get("time") or "")
    if asof:
        age_days = (now - asof).total_seconds() / 86400
        if age_days > VERIFY_MAX_AGE_DAYS:
            return "已过期"

    ret = (current_price - base) / base
    direction = _classify_conclusion(e.get("conclusion") or "")

    # 目标价优先: 现价达到目标价区间 → 兑现; 跌破记录价-8% → 证伪
    target_price = _extract_target_price(e.get("target") or "")
    if target_price and target_price > 0:
        if current_price >= target_price:
            return "已兑现"
        if ret <= -VERIFY_THRESHOLD:
            return "已证伪"
        return "待验证"

    if direction == "buy":
        if ret >= VERIFY_THRESHOLD:
            return "已兑现"
        if ret <= -VERIFY_THRESHOLD:
            return "已证伪"
        return "待验证"
    if direction == "sell":
        if ret <= -VERIFY_THRESHOLD:
            return "已兑现"  # 避损成功
        if ret >= VERIFY_THRESHOLD:
            return "已证伪"  # 卖飞
        return "待验证"
    # hold / unknown → 不自动判定
    return "待验证"


async def verify_pending_decisions(limit: int = 50) -> dict:
    """Verify pending entries against latest close prices.

    Uses historical kline close (not intraday realtime) so noisy ticks do
    not flip ±8% thresholds, and to avoid hammering the realtime API.

    Returns summary dict {verified, unchanged, skipped} for logging.
    """
    entries = list_decisions(max_entries=1000)
    summary = {"verified": 0, "unchanged": 0, "skipped": 0}
    processed = 0
    now = datetime.now(timezone.utc)

    for e in entries:
        if processed >= limit:
            break
        if e.get("verify") != "待验证":
            continue
        code = e.get("code") or ""
        record_price = e.get("record_price") or ""
        if code == "未指定" or not code or record_price in ("", "未指定"):
            summary["skipped"] += 1
            continue

        processed += 1
        try:
            price = await _fetch_historical_price(code, now)
            if price is None:
                summary["skipped"] += 1
                continue
        except Exception as e:
            logger.debug(f"verify: price fetch failed for {code}: {e}")
            summary["skipped"] += 1
            continue

        status = judge_entry(e, price, now)
        if status != "待验证":
            update_entry_fields(
                e.get("task_id") or "",
                {
                    "验证": status,
                    "最新价": f"{price:.2f}",
                    "上次验证": now.strftime("%Y-%m-%d %H:%M UTC"),
                },
            )
            summary["verified"] += 1
            logger.info(f"Verified {e.get('stock')}: {status} (record={record_price}, now={price:.2f})")
        else:
            # 待验证也刷新最新价 — 复盘注入需要"记录价→当前价(涨跌幅)",
            # 否则 LLM 看不到上次判断后来涨跌了多少。
            update_entry_fields(
                e.get("task_id") or "",
                {"最新价": f"{price:.2f}"},
            )
            summary["unchanged"] += 1

    return summary
