"""行业景气信号（试点：电子板块，2026-10-11）。

板块知识库给出的信号清单里，行业景气类数据本机大多没有源（实测电子板块 4 个视角
只有风险逆向派落地）。用户口径：**只做有代表性的试点，其余行业空着，不硬凑**。
本模块先给电子接两条公开免费、可稳定解析的信号：

- SIA 全球半导体销售额（月度，WSTS 三个月移动平均口径）
  路径：官网 latest-news 分类列表 → 最新一期 sales 新闻稿 → 正文抽
  金额 / 环比 / 同比 / 发布日。实测 2026-10-11：列表页 200、72KB，
  最新一期为 2026 年 8 月（1,597 亿美元、环比 +8%、同比 +144.3%、发布日 Oct. 5, 2026）。
- TrendForce DRAM 现货价
  路径：免费页面直接抓表格，DDR5 16Gb (2Gx8) 4800/5600 的盘平均（现货均价）
  与盘涨跌幅。实测 2026-10-11：均价 58.90 美元、▲0.40%。

纪律（与通用信号层一致）：抓不到就写「数据不可得」，绝不猜；来源 URL 与抓取时点
随数字一起进提示词；只对识别到电子板块（sector_router 命中 dianzi）时注入。

不覆盖（本机无源，禁止凭空填数）：SEMI 设备出货、晶圆厂产能利用率、渠道库存与交期、
其余 30 个板块的景气信号。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

import httpx

from ..core.data_cache import DataCache

logger = logging.getLogger("ai_berkshire.industry_signals")

CACHE_CATEGORY = "industry_signals"
CACHE_TTL = 6 * 3600            # 6 小时：SIA 月度稿与 DRAM 日频价
OVERALL_TIMEOUT = 25.0
UA = "Mozilla/5.0 (compatible; AIBerkshire/1.0)"
BLOCK_TITLE = "行业景气信号（电子·试点）"
SIA_LIST_URL = "https://www.semiconductors.org/news-events/latest-news/?fwp_news_categories=sales"
TRENDFORCE_DRAM_URL = "https://www.trendforce.cn/price/dram/dram_spot"

_cache: Optional[DataCache] = None


def _get_cache() -> DataCache:
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache


def _cached(key: str, producer):
    try:
        hit = _get_cache().get(CACHE_CATEGORY, key, max_age_seconds=CACHE_TTL)
        if hit:
            return hit
    except Exception as e:  # noqa: BLE001
        logger.debug(f"行业信号缓存读取失败({key}): {e}")
    val = producer()
    try:
        _get_cache().set(CACHE_CATEGORY, key, val)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"行业信号缓存写入失败({key}): {e}")
    return val


def _fetch(url: str, timeout: float = 20.0) -> str:
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


# ── SIA 全球半导体销售额 ────────────────────────────────────────────────

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}

_SIA_AMOUNT = re.compile(r"sales were \$([\d.]+)\s*billion\s+during the month of ([A-Za-z]+)\s+(\d{4})")
_SIA_MOM = re.compile(
    r"(?:an increase|a decrease) of ([\d.]+)%\s+compared\s+to\s+the\s+([A-Za-z]+)\s+(\d{4})\s+"
    r"total of \$([\d.]+)\s*billion")
_SIA_YOY = re.compile(
    r"([\d.]+)%\s+(more|less)\s+than the\s+([A-Za-z]+)\s+(\d{4})\s+total of \$([\d.]+)\s*billion")
_SIA_DATE = re.compile(r"WASHINGTON, ([A-Z][a-z]+\. \d{1,2}, \d{4})")


def parse_sia(text: str) -> dict:
    """从 SIA 新闻稿正文抽最新一期数据；缺项即 None（不猜）。"""
    out: dict = {}
    m = _SIA_AMOUNT.search(text)
    if m:
        out["amount_100m"] = round(float(m.group(1)) * 10, 1)      # $X billion → X*10 亿美元
        mon = _MONTHS.get(m.group(2))
        out["month"] = f"{m.group(3)}-{mon:02d}" if mon else m.group(2)
    m = _SIA_MOM.search(text)
    if m:
        sign = -1 if "decrease" in m.group(0)[:40] else 1
        mon = _MONTHS.get(m.group(2))
        out["mom_pct"] = sign * float(m.group(1))
        out["prev_month"] = f"{m.group(3)}-{mon:02d}" if mon else m.group(2)
        out["prev_amount_100m"] = round(float(m.group(4)) * 10, 1)
    m = _SIA_YOY.search(text)
    if m:
        mon = _MONTHS.get(m.group(3))
        out["yoy_pct"] = float(m.group(1)) * (1 if m.group(2) == "more" else -1)
        out["yoy_month"] = f"{m.group(4)}-{mon:02d}" if mon else m.group(3)
        out["yoy_amount_100m"] = round(float(m.group(5)) * 10, 1)
    m = _SIA_DATE.search(text)
    if m:
        out["published"] = m.group(1)
    return out


def fetch_sia_latest(fetcher=_fetch) -> dict:
    """列表页 → 最新一期 sales 稿 → 抽数据。失败返回 {}（调用方写「数据不可得」）。"""
    def _do() -> dict:
        try:
            listing = fetcher(SIA_LIST_URL)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SIA 列表页抓取失败: {type(e).__name__}")
            return {}
        hrefs = re.findall(r'href="(https://www\.semiconductors\.org/[^"]*(?:sales|revenue)[^"]*)"', listing)
        if not hrefs:
            logger.warning("SIA 列表页未解析到 sales 新闻稿链接")
            return {}
        # 列表按时间倒序，取第一条含 sales 的稿件
        for url in dict.fromkeys(hrefs):
            try:
                body = fetcher(url)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"SIA 稿件抓取失败({url[:60]}): {type(e).__name__}")
                continue
            data = parse_sia(body)
            if data.get("amount_100m"):
                data["url"] = url
                data["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
                return data
        logger.warning("SIA 稿件正文未解析到金额（可能改版）")
        return {}

    return _cached("sia_latest", _do)


# ── TrendForce DRAM 现货价 ──────────────────────────────────────────────

_DRAM_ROW = re.compile(
    r"DDR5 16Gb \(2Gx8\) 4800/5600.*?"
    r'<td class="lcd-num-l">([\d.]+)</td>\s*'
    r'<td class="lcd-num-l">([\d.]+)</td>\s*'
    r'<td class="lcd-num-l">([\d.]+)</td>\s*'
    r'<td class="lcd-num-l">([\d.]+)</td>\s*'
    r'<td class="lcd-num-l">([\d.]+)</td>\s*'
    r'<td class="percent-cell">.*?([\d.]+)\s*%',
    re.S,
)


def parse_dram_spot(html: str) -> dict:
    """抽 DDR5 16Gb 现货：盘平均（现货均价）+ 盘涨跌幅 + 日高低。"""
    m = _DRAM_ROW.search(html)
    if not m:
        return {}
    vals = [float(m.group(i)) for i in range(1, 6)]
    out = {
        "item": "DDR5 16Gb (2Gx8) 4800/5600",
        "day_high": vals[0], "day_low": vals[1],
        "session_high": vals[2], "session_low": vals[3],
        "session_avg": vals[4],
        "change_pct": float(m.group(6)),
    }
    seg = html[max(0, m.start()):m.end()]
    # 涨跌方向只认箭头字形（TrendForce 用 HTML 实体：&#9650; = ▲ / &#9660; = ▼）。
    # ⚠️ 不要用 class 名判断：实测涨势那一行的 class 依然是 "fall-trend"，会判反。
    up = ("▲" in seg) or ("&#9650;" in seg) or ("&#x25B2;" in seg)
    down = ("▼" in seg) or ("&#9660;" in seg) or ("&#x25BC;" in seg)
    out["direction"] = "涨" if up else ("跌" if down else "—")
    return out


def fetch_dram_spot(fetcher=_fetch) -> dict:
    def _do() -> dict:
        try:
            html = fetcher(TRENDFORCE_DRAM_URL)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"TrendForce 现货价抓取失败: {type(e).__name__}")
            return {}
        data = parse_dram_spot(html)
        if not data:
            logger.warning("TrendForce 页面未解析到 DDR5 16Gb 现货行（可能改版）")
            return {}
        data["url"] = TRENDFORCE_DRAM_URL
        data["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return data

    return _cached("dram_spot", _do)


# ── 注入块 ──────────────────────────────────────────────────────────────

def _fmt_amount(v) -> str:
    """金额按量级给格式：整数不带小数（1,597 亿），非整数保留一位。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:,.0f}" if f.is_integer() else f"{f:,.1f}"


def format_electronics_block(sia: dict, dram: dict) -> str:
    """数据不足时返回空串（宁可不注入，也不给空架子）。"""
    if not sia and not dram:
        return ""
    lines = [f"## {BLOCK_TITLE}", "", "以下为电子板块的行业景气数据（公开源抓取，随数字附来源与时点）。"]
    if sia.get("amount_100m"):
        line = f"- 全球半导体销售额（SIA/WSTS，{sia.get('month', '?')}）：{_fmt_amount(sia['amount_100m'])} 亿美元"
        if sia.get("mom_pct") is not None:
            line += f"，环比 {sia['mom_pct']:+.1f}%（{sia.get('prev_month')} 为 {_fmt_amount(sia.get('prev_amount_100m'))} 亿）"
        if sia.get("yoy_pct") is not None:
            line += f"，同比 {sia['yoy_pct']:+.1f}%（{sia.get('yoy_month')} 为 {_fmt_amount(sia.get('yoy_amount_100m'))} 亿）"
        line += "；三个月移动平均口径"
        if sia.get("published"):
            line += f"，SIA 发布日 {sia['published']}"
        line += f"（来源：SIA 官网 {sia.get('url', '')}，抓取于 {sia.get('fetched_at', '')}）"
        lines.append(line)
    else:
        lines.append("- 全球半导体销售额（SIA/WSTS）：数据不可得（官网抓取或解析失败，不得凭空填数）")
    if dram.get("session_avg") is not None:
        lines.append(
            f"- DRAM 现货价（TrendForce，{dram.get('item', '')}）：盘平均 {dram['session_avg']:.2f} 美元"
            f"（日高 {dram.get('day_high', 0):.2f} / 日低 {dram.get('day_low', 0):.2f}），"
            f"盘涨跌幅 {dram.get('direction', '')}{dram.get('change_pct', 0):.2f}%"
            f"（来源：{dram.get('url', '')}，抓取于 {dram.get('fetched_at', '')}）"
        )
    else:
        lines.append("- DRAM 现货价（TrendForce）：数据不可得（抓取或解析失败，不得凭空填数）")
    lines.append(
        "覆盖说明：本块只覆盖电子板块两条可稳定抓取的公开信号；SEMI 设备出货、晶圆厂产能利用率、"
        "渠道库存与交期等本机无源，不得凭空填数；其余 30 个板块暂不注入行业景气数据。"
    )
    return "\n".join(lines) + "\n"


ELECTRONICS_SECTOR_KEY = "dianzi"


async def build_industry_signal_context(sector_keys: list) -> str:
    """仅当识别到电子板块时返回块；异常/超时返回空串，不影响主链路。"""
    try:
        keys = list(sector_keys or [])
        if ELECTRONICS_SECTOR_KEY not in keys:
            return ""
        sia, dram = await _gather_signals()
        block = format_electronics_block(sia, dram)
        if block:
            logger.info(
                f"行业景气信号注入: 电子试点 SIA={'有' if sia.get('amount_100m') else '无'} "
                f"DRAM={'有' if dram.get('session_avg') is not None else '无'} 注入 {len(block)} 字符"
            )
        return block
    except Exception as e:  # noqa: BLE001
        logger.warning(f"行业景气信号注入失败，本轮不注入: {type(e).__name__}: {str(e)[:120]}")
        return ""


async def _gather_signals() -> tuple[dict, dict]:
    import asyncio

    async def _wrap(fn) -> dict:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout=OVERALL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"行业景气信号: {fn.__name__} 超时（>{OVERALL_TIMEOUT}s）")
            return {}

    sia, dram = await asyncio.gather(_wrap(fetch_sia_latest), _wrap(fetch_dram_spot))
    return sia or {}, dram or {}
