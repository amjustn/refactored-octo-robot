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
        "覆盖说明：本块覆盖电子板块两条可稳定抓取的公开信号（SIA 月度销售额、DRAM 现货价）；"
        "SEMI 设备出货、晶圆厂产能利用率、渠道库存与交期等本机无源，不得凭空填数。"
    )
    return "\n".join(lines) + "\n"


ELECTRONICS_SECTOR_KEY = "dianzi"

# ── 商品价格（现货价口径，覆盖资源/农业/新能源板块，2026-10-11）──────────
# 用户口径：网上公开、能稳定抓到的都接上；抓不到的不硬凑。
# 实测：akshare futures_spot_price 一次返回 54 个品种的现货价（含基差），覆盖
# 有色/钢铁/化工/建材/农林牧渔/新能源/纺织/轻工。两个缺口：动力煤（ZC 不在现货
# 列表、ZC0 主力连续返回空）⇒ **动力煤明确无源，煤炭只给焦煤**；原油（SC 不在
# 现货列表）⇒ 用主力连续收盘价补，输出时标注口径不同。
COMMODITY_MAP: dict = {
    "youse": [("CU", "铜"), ("AL", "铝"), ("ZN", "锌"), ("LC", "碳酸锂"), ("AU", "黄金"), ("AG", "白银")],
    "gangtie": [("RB", "螺纹钢"), ("I", "铁矿石"), ("J", "焦炭"), ("JM", "焦煤")],
    "meitan": [("JM", "焦煤")],
    "shiyou": [("FU", "燃料油"), ("BU", "沥青")],
    "huagong": [("TA", "PTA"), ("MA", "甲醇"), ("SA", "纯碱"), ("UR", "尿素"), ("PP", "聚丙烯")],
    "jiancai": [("FG", "玻璃")],
    "nonglin": [("LH", "生猪"), ("C", "玉米"), ("M", "豆粕"), ("SR", "白糖"), ("CF", "棉花")],
    "dianlishebei": [("LC", "碳酸锂"), ("SI", "工业硅"), ("PS", "多晶硅")],
    "fangzhi": [("CF", "棉花")],
    "qinggong": [("SP", "纸浆")],
}
COMMODITY_EXTRA: dict = {"shiyou": [("SC0", "原油")]}
COMMODITY_UNITS = {"AU": "元/克", "AG": "元/千克"}
COMMODITY_TITLE = "行业景气信号（商品价格·公开源）"


def _fetch_spot_two_days() -> dict:
    """抓最近两个交易日的现货价快照；futures_spot_price 对非交易日返回空表。"""
    import akshare as ak

    snaps: list = []
    day = dt.date.today()
    for _ in range(8):
        df = None
        try:
            df = ak.futures_spot_price(date=day.strftime("%Y%m%d"))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"现货价 {day} 抓取失败: {type(e).__name__}")
        if df is not None and len(df):
            snaps.append((day.strftime("%Y-%m-%d"), {str(k): float(v) for k, v in
                                                    zip(df["symbol"], df["spot_price"])}))
            if len(snaps) == 2:
                break
        day -= dt.timedelta(days=1)
    if not snaps:
        return {}
    out = {"date": snaps[0][0], "prices": snaps[0][1]}
    if len(snaps) > 1:
        out["prev_date"] = snaps[1][0]
        out["prev_prices"] = snaps[1][1]
    return out


def fetch_commodity_snapshot() -> dict:
    return _cached("commodity_spot", _fetch_spot_two_days)


def _fetch_commodity_extra() -> dict:
    """现货列表缺失的品种（原油）用主力连续收盘价补。"""
    import akshare as ak

    out: dict = {}
    start = (dt.date.today() - dt.timedelta(days=25)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")
    for sym, label in [x for lst in COMMODITY_EXTRA.values() for x in lst]:
        try:
            df = ak.futures_main_sina(symbol=sym, start_date=start, end_date=end)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"主力连续 {sym} 抓取失败: {type(e).__name__}")
            continue
        if df is None or len(df) == 0:
            logger.warning(f"主力连续 {sym} 无数据（品种可能已退市/停牌）")
            continue
        last, prev = df.iloc[-1], (df.iloc[-2] if len(df) > 1 else df.iloc[-1])
        try:
            px, pv = float(last["收盘价"]), float(prev["收盘价"])
        except Exception:  # noqa: BLE001
            continue
        out[sym] = {
            "label": label, "price": px, "prev": pv,
            "chg_pct": round((px / pv - 1) * 100, 2) if pv else None,
            "date": str(last["日期"])[:10],
        }
    return out


def fetch_commodity_extra() -> dict:
    return _cached("commodity_extra", _fetch_commodity_extra)


def _sector_cn_name(key: str) -> str:
    try:
        from .sector_knowledge import SECTORS
        return (SECTORS.get(key) or {}).get("name", key)
    except Exception:  # noqa: BLE001
        return key


def format_commodity_block(sector_keys: list, snap: dict, extra: dict) -> str:
    """按识别到的板块列出对应商品价格；同一品种只出现一次。"""
    prices = (snap or {}).get("prices") or {}
    prevs = (snap or {}).get("prev_prices") or {}
    if not prices and not extra:
        return ""
    used: set = set()
    rows: list = []
    for key in sector_keys:
        seg: list = []
        for sym, label in COMMODITY_MAP.get(key, []):
            if sym in used:
                continue
            px = prices.get(sym)
            if px is None:
                continue
            used.add(sym)
            unit = COMMODITY_UNITS.get(sym, "元/吨")
            item = f"{label} {px:,.2f} {unit}"
            pv = prevs.get(sym)
            if pv:
                chg = (px / pv - 1) * 100
                item += "（持平）" if abs(chg) < 0.005 else f"（{chg:+.2f}%）"
            seg.append(item)
        for sym, label in COMMODITY_EXTRA.get(key, []):
            info = (extra or {}).get(sym)
            if not info:
                continue
            item = f"{label} {info['price']:,.2f} 元/桶（主力合约收盘价）"
            if info.get("chg_pct") is not None:
                item += f"（{info['chg_pct']:+.2f}%）"
            seg.append(item)
        if seg:
            rows.append(f"- {_sector_cn_name(key)}：" + "、".join(seg))
    if not rows:
        return ""
    lines = [f"## {COMMODITY_TITLE}", ""]
    head = f"数据日期 {snap.get('date', '?')}"
    if snap.get("prev_date"):
        head += f"（涨跌对比 {snap['prev_date']}）"
    head += "；来源：akshare 现货价格接口"
    lines.append(head)
    lines.extend(rows)
    lines.append(
        "覆盖说明：以上是**商品价格代理指标**（现货价口径，原油为主力合约价）；行业景气还包括"
        "开工率、库存、订单、渠道等本机无源数据。动力煤、水泥、多数制造与消费行业本机无稳定源，"
        "不得凭空填数。"
    )
    return "\n".join(lines) + "\n"


async def _gather_commodity() -> tuple[dict, dict]:
    import asyncio

    async def _wrap(fn) -> dict:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout=OVERALL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"商品价格: {fn.__name__} 超时（>{OVERALL_TIMEOUT}s）")
            return {}

    snap, extra = await asyncio.gather(_wrap(fetch_commodity_snapshot), _wrap(fetch_commodity_extra))
    return snap or {}, extra or {}


async def build_industry_signal_context(sector_keys: list) -> str:
    """按识别到的板块拼注入块：电子走专用源（SIA/DRAM），资源与农业类走商品价格。

    任一源失败只影响它自己那一段；全失败返回空串（零注入），不影响主链路。
    """
    try:
        keys = list(sector_keys or [])
        if not keys:
            return ""
        blocks: list = []
        if ELECTRONICS_SECTOR_KEY in keys:
            sia, dram = await _gather_signals()
            b = format_electronics_block(sia, dram)
            if b:
                blocks.append(b)
                logger.info(
                    f"行业景气信号: 电子 SIA={'有' if sia.get('amount_100m') else '无'} "
                    f"DRAM={'有' if dram.get('session_avg') is not None else '无'}"
                )
        if any(k in COMMODITY_MAP or k in COMMODITY_EXTRA for k in keys):
            snap, extra = await _gather_commodity()
            b = format_commodity_block(keys, snap, extra)
            if b:
                blocks.append(b)
                logger.info(
                    f"行业景气信号: 商品价格 日期={snap.get('date')} "
                    f"板块={[k for k in keys if k in COMMODITY_MAP or k in COMMODITY_EXTRA]}"
                )
        if not blocks:
            return ""
        block = "\n".join(blocks)
        logger.info(f"行业景气信号注入: 共 {len(block)} 字符")
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
