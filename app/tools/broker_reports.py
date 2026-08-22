"""券商/机构研报数据源 — 东方财富研报中心。

提供:
1. get_stock_reports(symbol, days)      — 个股研报列表（评级/机构/作者/日期/目标价/EPS预测）
2. get_rating_summary(symbol, days)     — 评级分布 + 机构一致预期聚合（EPS/PE 中位数、目标价区间）
3. get_industry_reports(keyword, days)  — 行业研报（东财行业分类, 按标题关键词过滤）
4. get_report_detail(info_code)         — 研报正文（事件描述/事件点评，zw-content div）

数据源: https://reportapi.eastmoney.com/report/list (无鉴权, 国内直连)
接口字段说明:
  - emRatingName: 东方财富评级中文 (买入/增持/持有/中性/减持/卖出)
  - emRatingValue: 评级数值 (买入=3, 增持=2, 持有=1 一带 — 具体取值以接口为准)
  - indvAimPriceL / indvAimPriceT: 目标价区间下/上限
  - predictNextYearEps / predictNextYearPe: 机构对次年 EPS / PE 的预测
  - infoCode: 研报唯一 ID, 用于详情页与正文抓取
  - encodeUrl: PDF 加密标识（当前不可直接拼接下载，正文走详情页）

缓存: SQLite DataCache, 列表 TTL 30min, 正文 TTL 24h。失败静默降级为空数据。
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("ai_berkshire.broker_reports")

# ═══════════════════════════════════════════════════════
#  HTTP 客户端（独立于 web_search 的 client，避免头部冲突）
# ═══════════════════════════════════════════════════════

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_BASE = "https://reportapi.eastmoney.com/report/list"
_DETAIL = "https://data.eastmoney.com/report/info/{info_code}.html"


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is not None:
        try:
            if _client.is_closed:
                _client = None
        except Exception:
            _client = None
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(15.0, connect=10.0),
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "Referer": "https://data.eastmoney.com/",
                    },
                )
    return _client


async def close_client():
    global _client
    if _client and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None


# ═══════════════════════════════════════════════════════
#  代码规范化
# ═══════════════════════════════════════════════════════

def normalize_symbol(symbol: str) -> Optional[str]:
    """把常见代码形式归一为东财可用的 6 位 A 股代码。

    - "600519" / "600519.SH" / "SH600519" → "600519"
    - "000001.SZ" → "000001"
    - 港股 "00700.HK" / "0700.HK" → None（东财该接口不覆盖港股）
    - 美股 "AAPL" → None（东财该接口不覆盖美股）
    """
    if not symbol:
        return None
    s = symbol.strip().upper()
    # 去掉交易所后缀
    s = re.sub(r"\.(SH|SZ|HK)$", "", s)
    s = re.sub(r"^(SH|SZ)\.?", "", s)
    if re.fullmatch(r"\d{6}", s):
        return s
    return None


def _is_covered(symbol: str) -> bool:
    """该接口是否覆盖此标的（仅 A 股 6 位代码）。"""
    return normalize_symbol(symbol) is not None


# ═══════════════════════════════════════════════════════
#  研报列表
# ═══════════════════════════════════════════════════════

async def _fetch_report_list(
    code: str = "",
    q_type: int = 0,
    begin: str = "",
    end: str = "",
    page_size: int = 20,
) -> list:
    """调用东财研报列表接口。qType=0 个股, qType=1 行业。"""
    client = await _get_client()
    params = {
        "industryCode": "*",
        "pageSize": str(page_size),
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": begin,
        "endTime": end,
        "pageNo": "1",
        "fields": "",
        "qType": str(q_type),
        "orgCode": "",
        "code": code,
        "rcode": "",
    }
    resp = await asyncio.wait_for(client.get(_BASE, params=params), timeout=12.0)
    if resp.status_code != 200:
        logger.warning(f"Report list HTTP {resp.status_code}")
        return []
    try:
        j = resp.json()
        data = j.get("data") or []
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Report list parse failed: {e}")
        return []


def _clean_num(v) -> Optional[float]:
    """字符串数值 → float；空/非法 → None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s.lower() in ("none", "null", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_report(r: dict) -> dict:
    """把东财原始记录精简为研报卡片字段。"""
    aim_lo = _clean_num(r.get("indvAimPriceL"))
    aim_hi = _clean_num(r.get("indvAimPriceT"))
    return {
        "info_code": r.get("infoCode") or "",
        "title": r.get("title") or "",
        "org": r.get("orgSName") or r.get("orgName") or "",
        "researchers": r.get("researcher") or r.get("author") or "",
        "publish_date": (r.get("publishDate") or "")[:10],
        "rating": r.get("emRatingName") or r.get("sRatingName") or "",
        "rating_value": _clean_num(r.get("emRatingValue")),
        "prev_rating": r.get("lastEmRatingName") or "",
        "aim_price_lo": aim_lo,
        "aim_price_hi": aim_hi,
        "predict_next_year_eps": _clean_num(r.get("predictNextYearEps")),
        "predict_next_year_pe": _clean_num(r.get("predictNextYearPe")),
        "industry": r.get("industryName") or "",
        "stock_name": r.get("stockName") or "",
        "attach_pages": r.get("attachPages"),
    }


async def get_stock_reports(symbol: str, days: int = 90, limit: int = 20) -> dict:
    """获取个股研报列表。

    Returns:
    {
        "symbol": "600519",
        "name": "贵州茅台" | "",
        "covered": True/False,        # False = 该接口不覆盖(港股/美股/未上市)
        "total": int,                  # 时间段内研报总数
        "reports": [{info_code, title, org, researchers, publish_date,
                     rating, aim_price_lo, aim_price_hi,
                     predict_next_year_eps, predict_next_year_pe}, ...],
    }
    """
    code = normalize_symbol(symbol)
    if not code:
        return {
            "symbol": (symbol or "").strip(),
            "covered": False,
            "message": "东财研报接口暂不覆盖港股/美股，仅支持A股6位代码",
            "total": 0,
            "reports": [],
        }

    # 缓存
    from ..web_common import data_cache
    cache_key = f"{code}:{days}:{limit}"
    cached = data_cache.get("broker_reports", cache_key, max_age_seconds=1800)
    if cached is not None:
        return cached

    end = datetime.now()
    begin = end - timedelta(days=days)
    rows = await _fetch_report_list(
        code=code, q_type=0,
        begin=begin.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        page_size=min(limit, 100),
    )

    reports = [_format_report(r) for r in rows]
    name = ""
    if reports:
        name = reports[0].get("stock_name") or ""
    result = {
        "symbol": code,
        "name": name,
        "covered": True,
        "total": len(reports),
        "reports": reports,
    }
    data_cache.set("broker_reports", cache_key, result)
    return result


# ═══════════════════════════════════════════════════════
#  评级分布 + 机构一致预期
# ═══════════════════════════════════════════════════════

async def get_rating_summary(symbol: str, days: int = 365) -> dict:
    """聚合评级分布与一致预期（用于注入分析上下文）。

    Returns:
    {
        "symbol": "600519",
        "covered": True/False,
        "period_days": 365,
        "total_reports": int,
        "rating_distribution": {"买入": n, "增持": n, ...},
        "consensus": {
            "eps_next_year": 中位数 | None,   # 机构预测次年 EPS 中位数
            "pe_next_year": 中位数 | None,
            "aim_price_lo": 目标价区间下限 中位数 | None,
            "aim_price_hi": 目标价区间上限 中位数 | None,
        },
        "top_reports": [{...}, ...],   # 最近 5 篇
    }
    """
    code = normalize_symbol(symbol)
    if not code:
        return {"symbol": (symbol or "").strip(), "covered": False,
                "total_reports": 0, "rating_distribution": {}, "consensus": {},
                "top_reports": []}

    from ..web_common import data_cache
    cache_key = f"{code}:{days}"
    cached = data_cache.get("broker_rating", cache_key, max_age_seconds=3600)
    if cached is not None:
        return cached

    end = datetime.now()
    begin = end - timedelta(days=days)
    rows = await _fetch_report_list(
        code=code, q_type=0,
        begin=begin.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        page_size=100,
    )

    reports = [_format_report(r) for r in rows]

    # 评级分布
    dist = {}
    for r in reports:
        rt = r.get("rating") or ""
        if rt:
            dist[rt] = dist.get(rt, 0) + 1

    # 一致预期：取中位数（抗异常值）
    def _median(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        vals.sort()
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    consensus = {
        "eps_next_year": _median([r.get("predict_next_year_eps") for r in reports]),
        "pe_next_year": _median([r.get("predict_next_year_pe") for r in reports]),
        "aim_price_lo": _median([r.get("aim_price_lo") for r in reports if r.get("aim_price_lo")]),
        "aim_price_hi": _median([r.get("aim_price_hi") for r in reports if r.get("aim_price_hi")]),
        "report_count_with_eps": sum(1 for r in reports if r.get("predict_next_year_eps") is not None),
    }

    result = {
        "symbol": code,
        "covered": True,
        "period_days": days,
        "total_reports": len(reports),
        "rating_distribution": dist,
        "consensus": consensus,
        "top_reports": reports[:5],
    }
    data_cache.set("broker_rating", cache_key, result)
    return result


# ═══════════════════════════════════════════════════════
#  行业研报
# ═══════════════════════════════════════════════════════

async def get_industry_reports(keyword: str = "", days: int = 30, limit: int = 15) -> dict:
    """获取行业研报列表（东财行业分类），可按标题关键词过滤。

    keyword 为空返回最新行业研报；非空则按标题包含关键词过滤。
    """
    from ..web_common import data_cache
    cache_key = f"{keyword or '*'}:{days}:{limit}"
    cached = data_cache.get("broker_industry", cache_key, max_age_seconds=1800)
    if cached is not None:
        return cached

    end = datetime.now()
    begin = end - timedelta(days=days)
    rows = await _fetch_report_list(
        code="", q_type=1,
        begin=begin.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        page_size=min(limit * 3, 100),  # 多取一些供关键词过滤
    )

    reports = []
    for r in rows:
        fmt = _format_report(r)
        if keyword:
            kw = keyword.strip()
            if kw and kw not in (fmt.get("title") or "") and kw not in (fmt.get("industry") or ""):
                continue
        reports.append(fmt)
        if len(reports) >= limit:
            break

    result = {
        "keyword": keyword,
        "period_days": days,
        "total": len(reports),
        "reports": reports,
    }
    data_cache.set("broker_industry", cache_key, result)
    return result


# ═══════════════════════════════════════════════════════
#  研报正文
# ═══════════════════════════════════════════════════════

async def get_report_detail(info_code: str, max_chars: int = 6000) -> dict:
    """抓取研报详情页正文（事件描述/事件点评）。

    Returns:
    {
        "info_code": str,
        "title": str,
        "content": str,      # 正文纯文本（zw-content 区域, 截断到 max_chars）
        "error": str | None,
    }
    """
    if not info_code:
        return {"info_code": "", "error": "缺少 info_code"}

    from ..web_common import data_cache
    cache_key = f"{info_code}:{max_chars}"
    cached = data_cache.get("broker_detail", cache_key, max_age_seconds=86400)
    if cached is not None:
        return cached

    client = await _get_client()
    url = _DETAIL.format(info_code=info_code)
    try:
        resp = await asyncio.wait_for(client.get(url), timeout=12.0)
        if resp.status_code != 200:
            return {"info_code": info_code, "error": f"HTTP {resp.status_code}"}
        soup = BeautifulSoup(resp.text, "html.parser")
        title = ""
        if soup.title:
            title = soup.title.string.strip() if soup.title.string else ""
        # 正文容器: div.zw-content（含"事件描述/事件点评"）
        content = ""
        zw = soup.find("div", class_=re.compile(r"zw-content"))
        if zw:
            content = zw.get_text("\n", strip=True)
        if not content:
            ctx = soup.find("div", class_=re.compile(r"ctx-content"))
            if ctx:
                content = ctx.get_text("\n", strip=True)
        # 去掉尾部版权噪音
        content = re.sub(r"www\.eastmoney\.com", "", content)
        content = re.sub(r"\n{3,}", "\n\n", content).strip()
        result = {
            "info_code": info_code,
            "title": title,
            "content": content[:max_chars],
        }
        if content:
            data_cache.set("broker_detail", cache_key, result)
        return result
    except Exception as e:
        logger.warning(f"Report detail failed {info_code}: {e}")
        return {"info_code": info_code, "error": str(e)}


# ═══════════════════════════════════════════════════════
#  分析上下文（供 build_full_context 注入）
# ═══════════════════════════════════════════════════════

async def build_broker_context(symbol: str = "", arguments: str = "", skill_name: str = "") -> str:
    """构建机构研报上下文块 — 每个 Agent 分析时自动带上的券商观点。

    当目标是 A 股代码时返回一段格式化文本：
    - 近一年评级分布
    - 机构一致预期（次年 EPS/PE 中位数、目标价区间）
    - 最近 5 篇研报（机构/日期/评级/标题）
    非 A 股标的返回 ""（不阻断分析）。
    """
    code = normalize_symbol(symbol)
    if not code:
        return ""

    try:
        import asyncio as _aio
        from ..tools.broker_reports import get_rating_summary
        summary = await _aio.wait_for(get_rating_summary(code, days=365), timeout=15.0)
        if not summary or not summary.get("covered") or summary.get("total_reports", 0) == 0:
            return ""

        parts = ["## 📊 机构研报观点（东方财富研报中心）"]
        name = ""
        if summary.get("top_reports"):
            name = summary["top_reports"][0].get("stock_name") or ""
        parts.append(f"*标的: {name or code} | 近一年研报 {summary['total_reports']} 篇*")
        parts.append("")

        # 评级分布
        dist = summary.get("rating_distribution") or {}
        if dist:
            dist_str = "、".join(f"{k} {v}篇" for k, v in sorted(dist.items(), key=lambda x: -x[1]))
            parts.append(f"**评级分布**：{dist_str}")

        # 一致预期
        c = summary.get("consensus") or {}
        cons_parts = []
        if c.get("eps_next_year") is not None:
            cons_parts.append(f"次年EPS一致预期 {c['eps_next_year']:.2f}（{c.get('report_count_with_eps', 0)}家机构）")
        if c.get("pe_next_year") is not None:
            cons_parts.append(f"次年PE {c['pe_next_year']:.1f}")
        if c.get("aim_price_lo") is not None and c.get("aim_price_hi") is not None:
            cons_parts.append(f"目标价区间 {c['aim_price_lo']:.2f}~{c['aim_price_hi']:.2f}")
        if cons_parts:
            parts.append("**机构一致预期**：" + "；".join(cons_parts))

        # 最近研报
        top = summary.get("top_reports") or []
        if top:
            parts.append("")
            parts.append("**最近研报**：")
            for r in top[:5]:
                line = f"- [{r.get('publish_date', '')}] {r.get('org', '')} | {r.get('rating', '')}"
                tp = r.get("aim_price_hi")
                if tp is not None:
                    line += f" | 目标价 {tp:.2f}"
                parts.append(line)
                if r.get("title"):
                    parts.append(f"  《{r['title']}》")
        parts.append("")
        parts.append("**使用要求（强制）**：以上为机构公开发布的研报评级与预测数据。")
        parts.append("**报告中凡是使用了研报数据的地方（评级、目标价、盈利预测、机构观点），必须在引用处标注来源。**")
        parts.append("标注格式：单篇研报用 `（来源：<机构名> <日期> 研报）`；")
        parts.append("一致预期/评级分布用 `（来源：东方财富研报中心，近一年N篇研报）`。")
        parts.append("未标注来源的研报数据不得出现在报告中。")
        parts.append("分析时应参考机构观点，但须独立判断——机构的评级可能滞后或有利益冲突，")
        parts.append("最终结论应基于你自身的框架，并在报告中明确标注机构观点与你自己判断的差异。")
        parts.append("")
        return "\n".join(parts)
    except Exception as e:
        logger.debug(f"Broker context failed: {e}")
        return ""
