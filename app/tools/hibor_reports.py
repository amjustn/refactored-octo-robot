"""慧博投研机构研报数据源 — 频道列表页抓取 + 站内关键词过滤 + Wind 可用性判定。

为什么需要这个数据源（相对东财研报中心 broker_reports.py）：
  - 东财研报中心（reportapi.eastmoney.com）只覆盖 A 股个股/行业研报；
  - 慧博投研（www.hibor.com.cn）的「港美研究」频道覆盖港股/美股研报，
    是东财没有的缺口；另外还有精选研报 / 宏观研究 / 投资策略 / 晨会早刊等频道。

实测结论（2026-09-13，WSL + requests 2.34.2，均带浏览器 UA + Referer 匿名抓取）：
  1. 频道列表页（/elitelist.html、/microns_*.html）可匿名抓取，返回 UTF-8 HTML。
     条目形如 <a href="/data/<md5>.html">机构名-标题-日期</a>，标题尾部为 -YYMMDD。
     注意：部分频道（microns_*）每条报告有**两个**指向同一 /data/ 的 <a>：
     一个是标题链接（含日期），另一个是同 URL 的摘要「[详细]」链接。
     因此必须按 URL 去重，并优先选取「以日期结尾」的那个标题。
     频道实测条数（去重后）：精选研报 38 / 公司调研 82 / 行业分析 120 / 宏观研究 117
     / 港美研究 98 / 投资策略 116 / 晨会早刊 80 / 新股研究 80。
  2. 站内搜索 /newweb/HuiSou/s 是 JS 渲染的（GET 返回壳页，无条目），
     **不可依赖**。本模块改为「拉频道列表页 + 本地按标题子串过滤」。
  3. 详情页 /data/<md5>.html 本身是 JS 壳页（约 13KB，正文为空，出现「加载中......」）。
     真实内容通过 AJAX 获取：GET 详情页拿 cookie + 页面内 var ncid，
     再 POST /hiborweb/DocDetail/NewContent（form: ncid=...，需 X-Requested-With）。
     **匿名只能拿到被截断的「摘要预览」**（div.abstruct-info，尾部带「...」），
     全文被登录墙拦截——摘要尾部的 <p class="all-abstruct" onclick="loginLayer('点击展开全文')">
     即证据；页面同时提供「打开报告原文 / 下载报告」并要求「请阅读并同意免责条款」。
     因此 get_report_detail 对「正文」返回 available=False，并在 note 里写明实测证据，
     绝不编造正文。
  4. Wind：本机无 Wind 金融终端、无 WindPy 授权、无 WIND_API_KEY/WIND_USER，
     wind_available() 返回 (False, <实测原因>)。要接入 Wind 需要付费的 Wind 终端 +
     账号授权（含 WindPy 权限），具体见 wind_reports 文档字符串。

缓存：app.core.data_cache.DataCache，category 'hibor_reports'，
      列表 key = 'channel|keyword|limit'，TTL 1800 秒。

公开 API：
  HIBOR_CHANNELS                                   — 频道名 -> 路径
  fetch_hibor_reports(channel, keyword, limit)     — 频道列表（可按关键词过滤）
  fetch_hibor_for_symbol(symbol, company_name, limit) — 按公司名/代码跨频道检索
  get_report_detail(url)                           — 详情页（摘要/正文可得性）
  format_hibor_context(keyword, symbol, company_name, limit) — 注入文本块
  wind_available() / wind_reports(symbol)          — Wind 可用性判定

所有函数均为**同步**实现（requests）。为便于在 async 主线中集成，
文件末尾提供 asyncio.to_thread 包装的 afetch_* / aget_report_detail / aformat_* 版本。
任何网络/解析异常都 logger.warning 后返回空结果，禁止静默失败。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("ai_berkshire.hibor_reports")

# ═══════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════

_BASE = "https://www.hibor.com.cn"
_DETAIL_AJAX = _BASE + "/hiborweb/DocDetail/NewContent"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Referer": _BASE + "/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_AJAX_HEADERS = {
    **_HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# 频道名 -> 路径（实测可匿名抓取）
HIBOR_CHANNELS: dict[str, str] = {
    "精选研报": "/elitelist.html",
    "公司调研": "/microns_1.html",
    "行业分析": "/microns_2.html",
    "宏观研究": "/microns_13.html",
    "港美研究": "/microns_9.html",
    "投资策略": "/microns_4.html",
    "晨会早刊": "/microns_14.html",
    "新股研究": "/microns_21.html",
}

# fetch_hibor_for_symbol / 关键词检索默认覆盖的频道（按优先级）
_SYMBOL_CHANNELS = ("公司调研", "行业分析", "精选研报", "港美研究")
_KEYWORD_CHANNELS = ("公司调研", "行业分析", "港美研究", "精选研报")

_CACHE_CATEGORY = "hibor_reports"
_CACHE_TTL = 1800  # 秒

# 详情页「仅摘要」的判定词（实测证据）
_LOGIN_WALL_MARKERS = ("点击展开全文", "打开报告原文", "下载报告", "请阅读并同意免责条款")

try:  # trafilatura 仅在详情页兜底抽取时使用（已安装 2.1.0）
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover - 环境缺失时优雅降级
    trafilatura = None  # type: ignore


# ═══════════════════════════════════════════════════════
#  缓存 & 会话
# ═══════════════════════════════════════════════════════

try:
    from ..core.data_cache import DataCache as _DataCache  # type: ignore
except Exception:  # pragma: no cover - 非包上下文兜底
    try:
        from app.core.data_cache import DataCache as _DataCache  # type: ignore
    except Exception as _e:  # pragma: no cover
        _DataCache = None  # type: ignore
        logging.getLogger(__name__).warning(f"HIBOR: DataCache 不可用，将禁用缓存: {_e}")

_cache = _DataCache() if _DataCache is not None else None

_session_lock = threading.Lock()
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """进程内复用的 requests.Session（保留 cookie，详情页 AJAX 需要）。"""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                s.headers.update(_HEADERS)
                _session = s
    return _session


def _cache_get(key: str) -> Any:
    if _cache is None:
        return None
    try:
        return _cache.get(_CACHE_CATEGORY, key, max_age_seconds=_CACHE_TTL)
    except Exception as e:  # pragma: no cover
        logger.warning(f"HIBOR cache get failed [{key}]: {e}")
        return None


def _cache_set(key: str, value: Any) -> None:
    if _cache is None:
        return
    try:
        _cache.set(_CACHE_CATEGORY, key, value)
    except Exception as e:  # pragma: no cover
        logger.warning(f"HIBOR cache set failed [{key}]: {e}")


# ═══════════════════════════════════════════════════════
#  纯函数（可离线自检）
# ═══════════════════════════════════════════════════════

def _convert_date(code: str) -> str:
    """'260913' -> '2026-09-13'；非法/越界返回 ''。

    两位年约定：yy < 70 → 20yy，否则 19yy（研报日期均为 2020+，此规则只影响边界测试）。
    使用 date() 校验合法性（含闰年）：'240229' -> '2024-02-29'（2024 闰年，合法）；
    '260229' -> ''（2026 非闰年，非法）；'261340' -> ''（月份/日越界）。
    """
    s = (code or "").strip()
    if not re.fullmatch(r"\d{6}", s):
        return ""
    yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        date(year, mm, dd)
    except ValueError:
        return ""
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def _date_from_title(title: str) -> str:
    """从标题尾部解析日期：优先 '-YYMMDD'，其次 'YYYY-MM-DD'；解析不到返回 ''。"""
    t = (title or "").strip()
    m = re.search(r"-(\d{6})\s*$", t)
    if m:
        d = _convert_date(m.group(1))
        if d:
            return d
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m2:
        try:
            date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
            return m2.group(0)
        except ValueError:
            return ""
    return ""


def _split_institution(title: str) -> str:
    """取标题前缀的机构名，如 '国信证券-港股市场速览：...-260913' -> '国信证券'。

    规则：按第一个 '-' 切分取前缀；拒绝纯数字（可能是日期前缀）、
    含标点/空白、长度不在 [2, 20] 的前缀；无 '-' 返回 ''。
    """
    t = (title or "").strip()
    if not t or "-" not in t:
        return ""
    cand = t.split("-", 1)[0].strip()
    if not cand or cand.isdigit():
        return ""
    if not (2 <= len(cand) <= 20):
        return ""
    if any(ch in cand for ch in "：:，,、（）()【】 "):
        return ""
    return cand


def _score_title(t: str) -> int:
    """给同一 URL 的多个候选标题打分：以日期结尾的最优，截断/摘要式的最差。"""
    t = t or ""
    score = 0
    if re.search(r"-\d{6}\s*$", t) or re.search(r"\d{4}-\d{2}-\d{2}\s*$", t):
        score += 1000
    if t.rstrip().endswith("…") or t.rstrip().endswith("..."):
        score -= 500
    if t[:1] in ("　", " ", "　"):
        score -= 300  # 摘要式文本常以全角空格开头
    score += min(len(t), 200)
    return score


def _dedupe_reports(reports: list) -> list:
    """按 url 去重（保留首次出现的顺序）；无 url 的项原样保留。"""
    seen: set = set()
    out: list = []
    for r in reports or []:
        if not isinstance(r, dict):
            continue
        u = r.get("url") or ""
        if u:
            if u in seen:
                continue
            seen.add(u)
        out.append(r)
    return out


def _abs_url(href: str) -> str:
    h = (href or "").strip()
    if not h:
        return ""
    if h.startswith("http://") or h.startswith("https://"):
        return h
    if h.startswith("/"):
        return _BASE + h
    return _BASE + "/" + h.lstrip("./")


def _clean_title(t: str) -> str:
    t = (t or "").replace("\u00a0", " ").strip()
    t = re.sub(r"\s+", " ", t)
    if t in ("[详细]", "详细", "更多>>", "更多"):
        return ""
    return t


def _extract_ncid(html: str) -> str:
    """详情页壳页里的 var ncid = '...'（AJAX 取正文所需）。取不到返回 ''。"""
    m = re.search(r"var\s+ncid\s*=\s*'([^']+)'", html or "")
    return m.group(1) if m else ""


# ═══════════════════════════════════════════════════════
#  列表页抓取与解析
# ═══════════════════════════════════════════════════════

def _fetch_list_page(path: str) -> Optional[str]:
    """抓频道列表页 HTML；失败 logger.warning 并返回 None。"""
    url = _abs_url(path)
    try:
        resp = _get_session().get(url, headers=_HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"HIBOR 列表页 HTTP {resp.status_code}: {url}")
            return None
        resp.encoding = "utf-8"  # 站点为 UTF-8
        return resp.text
    except Exception as e:
        logger.warning(f"HIBOR 列表页抓取失败 {url}: {type(e).__name__}: {e}")
        return None


def _parse_list_page(html: str) -> list:
    """解析列表页 -> [{'url','title'}]，同 URL 只保留得分最高的标题。"""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:
        logger.warning(f"HIBOR 列表页解析失败(lxml): {e}")
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e2:
            logger.warning(f"HIBOR 列表页解析失败(html.parser): {e2}")
            return []

    best: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if "/data/" not in href:
            continue
        url = _abs_url(href)
        if not url.endswith(".html"):
            continue
        # 排除分享/贴吧等噪音链接
        cls = " ".join(a.get("class") or [])
        if "tieba" in cls:
            continue
        title = _clean_title(str(a.get("title") or "").strip() or a.get_text(" ", strip=True))
        if not title:
            continue
        prev = best.get(url)
        if prev is None or _score_title(title) > _score_title(prev):
            best[url] = title
    return [{"url": u, "title": t} for u, t in best.items()]


def _entry(url: str, title: str, channel: str) -> dict:
    return {
        "title": title,
        "institution": _split_institution(title),
        "date": _date_from_title(title),
        "url": url,
        "channel": channel,
    }


# ═══════════════════════════════════════════════════════
#  公开 API — 列表
# ═══════════════════════════════════════════════════════

# ── 未来日期过滤（2026-09-13，静态审计第 2 条）────────────────────
# 慧博列表页偶尔出现"源日期晚于抓取日"的条目：实测 2026-09-13 抓取时，
# 港美研究 / 公司调研 / 行业分析 三个频道各有 1 条日期为 2026-09-14
# （华鑫证券-传媒行业动态研究报告-260914）。这类条目会让报告把"明天"的研报
# 当"最新"，故在返回前剔除并留痕（剔除数量 + 样例写进 warning 日志）。
def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def is_future_date(date_str: str, today: str = "") -> bool:
    """日期字符串晚于今天 → True；空值/非法日期 → False（不误删）。"""
    d = str(date_str or "").strip()
    t = str(today or "").strip() or _today_str()
    try:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
        tt = datetime.strptime(t, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return dd > tt


def drop_future_entries(entries: list, channel: str = "") -> list:
    """剔除日期晚于今天的条目；有剔除时记 warning（含数量与样例）。"""
    kept, dropped = [], []
    for e in entries or []:
        if isinstance(e, dict) and is_future_date(e.get("date")):
            dropped.append(e)
        else:
            kept.append(e)
    if dropped:
        sample = "；".join(f"{e.get('date')} {str(e.get('title') or '')[:40]}" for e in dropped[:3])
        logger.warning(
            f"HIBOR 剔除 {len(dropped)} 条未来日期条目（频道={channel or '-'}）：{sample}"
        )
    return kept


def fetch_hibor_reports(channel: str = "精选研报", keyword: str = "", limit: int = 20) -> list:
    """抓取慧博某频道的研报列表，可按标题关键词过滤。

    Args:
        channel: HIBOR_CHANNELS 的频道名（默认「精选研报」）。
        keyword: 非空时按标题做**大小写不敏感**的子串过滤。
        limit:   最多返回条数。

    Returns:
        [{'title','institution','date','url','channel'}, ...]；任何异常返回 []。
    """
    if channel not in HIBOR_CHANNELS:
        logger.warning(f"HIBOR 未知频道: {channel!r}，可用: {list(HIBOR_CHANNELS)}")
        return []

    try:
        limit = max(1, int(limit or 20))
    except (TypeError, ValueError):
        logger.warning(f"HIBOR limit 非法: {limit!r}，回退为 20")
        limit = 20

    kw = (keyword or "").strip().lower()
    cache_key = f"{channel}|{kw}|{limit}"

    cached = _cache_get(cache_key)
    if cached is not None:
        # 缓存里可能存着过滤规则上线之前抓的条目（TTL 30 分钟），
        # 返回前同样过一遍未来日期过滤，避免旧缓存绕过规则。
        return drop_future_entries(cached, channel)

    html = _fetch_list_page(HIBOR_CHANNELS[channel])
    if not html:
        return []

    rows = _parse_list_page(html)
    reports = []
    for r in rows:
        title = r["title"]
        if kw and kw not in title.lower():
            continue
        reports.append(_entry(r["url"], title, channel))

    # 剔除源日期晚于今天的条目（见 is_future_date/drop_future_entries 注释）
    reports = drop_future_entries(reports, channel)

    # 有日期的在前，按日期倒序；无日期保持原顺序垫底
    reports.sort(key=lambda x: (x.get("date") or "0000-00-00"), reverse=True)
    reports = reports[:limit]

    if reports:
        _cache_set(cache_key, reports)
    else:
        logger.warning(f"HIBOR 频道 {channel} 解析到 0 条（keyword={kw!r}）")
    return reports


def fetch_hibor_for_symbol(symbol: str = "", company_name: str = "", limit: int = 15) -> list:
    """按公司名或 6 位代码在多个频道中检索研报，合并去重后按日期倒序。

    symbol/company_name 都为空 -> []（不发起网络请求）。
    """
    sym = (symbol or "").strip()
    name = (company_name or "").strip()
    if not sym and not name:
        return []

    try:
        limit = max(1, int(limit or 15))
    except (TypeError, ValueError):
        limit = 15

    keys = [k for k in (sym, name) if k]
    merged: list = []
    for ch in _SYMBOL_CHANNELS:
        for r in fetch_hibor_reports(channel=ch, keyword="", limit=120):
            title = r.get("title") or ""
            if any(k in title for k in keys):
                merged.append(r)

    merged = _dedupe_reports(merged)
    merged.sort(key=lambda x: (x.get("date") or "0000-00-00"), reverse=True)
    return merged[:limit]


def _search_keyword(keyword: str, limit: int = 15) -> list:
    """跨频道按标题关键词检索（列表页无检索接口，改为本地子串过滤）。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    merged: list = []
    for ch in _KEYWORD_CHANNELS:
        merged.extend(fetch_hibor_reports(channel=ch, keyword=kw, limit=limit))
    merged = _dedupe_reports(merged)
    merged.sort(key=lambda x: (x.get("date") or "0000-00-00"), reverse=True)
    return merged[:limit]


# ═══════════════════════════════════════════════════════
#  公开 API — 详情页
# ═══════════════════════════════════════════════════════

def _extract_detail(html: str, url: str) -> dict:
    """从详情页（AJAX 返回体）抽取标题/日期/机构/摘要，并给出可得性证据。"""
    out = {
        "title": "",
        "date": "",
        "institution": "",
        "abstract": "",
        "authors": "",
        "pages": "",
        "evidence": [],
    }
    if not html:
        return out

    # metaTitle（AJAX 返回的 <script> 内）
    m = re.search(r"var\s+metaTitle\s*=\s*'([^']*)'", html)
    if m:
        out["title"] = m.group(1).replace("-慧博投研资讯", "").strip()

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    if not out["title"]:
        ld = soup.find("div", class_=re.compile(r"left-detail"))
        if ld:
            h = ld.find(["h1", "h2", "h3"]) or ld.find("a")
            if h:
                out["title"] = str(h.get("title") or h.get_text(" ", strip=True)).strip()

    # 摘要（匿名可得，但被截断）
    abs_div = soup.find("div", class_=re.compile(r"abstruct-info"))
    if abs_div:
        txt = abs_div.get_text("\n", strip=True)
        txt = txt.replace("展开全文>>", "").replace("展开全文", "").strip()
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        out["abstract"] = txt

    # 元信息：日期 / 研报出处 / 作者 / 页数
    ld = soup.find("div", class_=re.compile(r"left-detail"))
    if ld:
        ltxt = ld.get_text("\n", strip=True)
        dm = re.search(r"日期：\s*(\d{4}-\d{2}-\d{2})", ltxt)
        if dm:
            out["date"] = dm.group(1)
        im = re.search(r"研报出处：\s*\n?\s*([^\n]+)", ltxt)
        if im:
            out["institution"] = im.group(1).strip()
        pm = re.search(r"(\d+)\s*页", ltxt)
        if pm:
            out["pages"] = pm.group(1)
        if not out["date"]:
            m2 = re.search(r"日期：\s*(\d{4}-\d{2}-\d{2})", html)
            if m2:
                out["date"] = m2.group(1)

    km = re.search(r"var\s+metaKeywords\s*=\s*'([^']*)'", html)
    if km:
        out["authors"] = km.group(1).strip()

    # 登录墙证据
    for marker in _LOGIN_WALL_MARKERS:
        if marker in html:
            out["evidence"].append(marker)
    if re.search(r"onclick\s*=\s*\"?loginLayer\(", html):
        out["evidence"].append('onclick="loginLayer(...)"')
    if "abstruct-info" in html:
        out["evidence"].append("div.abstruct-info 摘要预览（尾部截断为 ...）")
    return out


def get_report_detail(url: str) -> dict:
    """抓慧博 /data/<md5>.html 详情页，判断匿名能否拿到正文。

    实测流程：GET 详情页（壳页）拿 cookie + var ncid → POST /hiborweb/DocDetail/NewContent
    取回真实内容片段（含 div.abstruct-info 摘要、标题、日期、机构、页数）。

    实测结论：匿名**只能拿到被截断的「摘要预览」**，全文被登录墙拦截
    （证据：摘要尾部 <p class="all-abstruct" onclick="loginLayer('点击展开全文')">，
    以及「打开报告原文 / 下载报告 / 请阅读并同意免责条款」）。

    Returns:
        {
          'url': str,
          'title': str,
          'content': str,      # 匿名实得文本（通常是摘要预览）；
                               # 拿不到任何正文/摘要时为 ''，绝不编造
          'available': bool,   # True 仅当拿到未截断的正文；本机实测为 False（登录墙）
          'note': str,         # 实测证据 / 阻塞原因
          # 以下为附加字段
          'date','institution','authors','pages','abstract','has_content','blocked_by_login'
        }
    """
    result = {
        "url": url or "",
        "title": "",
        "content": "",
        "available": False,
        "note": "",
        "date": "",
        "institution": "",
        "authors": "",
        "pages": "",
        "abstract": "",
        "has_content": False,
        "blocked_by_login": False,
    }

    if not url:
        result["note"] = "缺少 url 参数"
        return result

    full_url = _abs_url(url)
    result["url"] = full_url

    cache_key = f"detail|{full_url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sess = _get_session()
    try:
        g = sess.get(full_url, headers=_HEADERS, timeout=25)
        if g.status_code != 200:
            result["note"] = f"详情页 HTTP {g.status_code}"
            logger.warning(f"HIBOR 详情页 HTTP {g.status_code}: {full_url}")
            return result
        g.encoding = "utf-8"
        shell_html = g.text

        ncid = _extract_ncid(shell_html)
        html = ""
        if ncid:
            try:
                r = sess.post(
                    _DETAIL_AJAX,
                    data={"ncid": ncid},
                    headers={**_AJAX_HEADERS, "Referer": full_url},
                    timeout=25,
                )
                r.encoding = "utf-8"
                if r.status_code == 200 and len(r.text or "") > 2000:
                    html = r.text
                else:
                    logger.warning(
                        f"HIBOR 详情 AJAX 返回异常 status={r.status_code} len={len(r.text or '')}"
                    )
            except Exception as e:
                logger.warning(f"HIBOR 详情 AJAX 失败 {full_url}: {type(e).__name__}: {e}")
        else:
            logger.warning(f"HIBOR 详情页未找到 ncid: {full_url}")

        if not html:
            # 兜底：壳页本身（通常只有导航与「加载中......」，无正文）
            html = shell_html

        info = _extract_detail(html, full_url)
        result["title"] = info.get("title", "")
        result["date"] = info.get("date", "")
        result["institution"] = info.get("institution", "")
        result["authors"] = info.get("authors", "")
        result["pages"] = info.get("pages", "")
        result["abstract"] = info.get("abstract", "")

        content = info.get("abstract", "") or ""
        if not content and trafilatura is not None:
            try:
                ext = trafilatura.extract(html, url=full_url, include_comments=False)
                if ext and len(ext) >= 200:
                    content = ext.strip()
            except Exception as e:
                logger.warning(f"HIBOR trafilatura 抽取失败: {e}")

        result["content"] = content
        result["has_content"] = bool(content)

        evidence = info.get("evidence") or []
        blocked = any(m in ("点击展开全文", "打开报告原文", "下载报告") for m in evidence)
        result["blocked_by_login"] = blocked

        if blocked:
            # 匿名拿到的是被截断的摘要，正文被登录墙拦截
            result["available"] = False
            ev = "；".join(evidence[:4]) if evidence else "未捕获到明确标记"
            if content:
                result["note"] = (
                    f"仅取到被截断的「摘要预览」{len(content)} 字符，未取到完整正文——"
                    f"全文需登录/客户端，被墙。实测证据：{ev}；"
                    f"页面提示「10秒免费下载安装慧博PC客户端…支持手机号或用户名登录」"
                )
            else:
                result["note"] = (
                    f"未取到正文与摘要（详情为 JS 壳页/AJAX 拦截）。实测证据：{ev}"
                )
        elif content:
            result["available"] = True
            result["note"] = f"匿名取到正文 {len(content)} 字符"
        else:
            result["available"] = False
            result["note"] = (
                "未取到正文——详情页为 JS 壳页（出现「加载中......」）且 AJAX 未返回可用内容"
            )

        _cache_set(cache_key, result)
        return result

    except Exception as e:
        logger.warning(f"HIBOR 详情页抓取失败 {full_url}: {type(e).__name__}: {e}")
        result["note"] = f"抓取异常: {type(e).__name__}: {e}"
        return result


# ═══════════════════════════════════════════════════════
#  公开 API — 注入上下文
# ═══════════════════════════════════════════════════════

_MAX_CONTEXT_CHARS = 3000


def format_hibor_context(
    keyword: str = "", symbol: str = "", company_name: str = "", limit: int = 12
) -> str:
    """生成机构研报注入文本块。

    首行固定 '## 机构研报（慧博投研）'，每条一行 '机构名 | 标题 | 日期 | 链接'，
    文末注明来源与抓取时间。无数据返回 ''。总长 ≤ 3000 字符。
    """
    kw = (keyword or "").strip()
    sym = (symbol or "").strip()
    name = (company_name or "").strip()
    if not (kw or sym or name):
        return ""

    try:
        limit = max(1, int(limit or 12))
    except (TypeError, ValueError):
        limit = 12

    reports: list = []
    try:
        if sym or name:
            reports = fetch_hibor_for_symbol(symbol=sym, company_name=name, limit=limit)
        if not reports and kw:
            reports = _search_keyword(kw, limit=limit)
        if not reports:
            return ""

        lines_header = "## 机构研报（慧博投研）"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        total = len(reports)
        rows = [
            f"{r.get('institution') or ''} | {r.get('title') or ''} | {r.get('date') or ''} | {r.get('url') or ''}"
            for r in reports
        ]

        def _build(shown: int) -> str:
            foot = (
                f"（来源：慧博投研资讯 www.hibor.com.cn；抓取时间 {ts}；共 {total} 条"
                + (f"，因长度上限仅展示前 {shown} 条" if shown < total else "")
                + "。以上为匿名抓取的公开研报列表，仅供研究参考。）"
            )
            return "\n".join([lines_header, *rows[:shown], foot])

        # 从「全部」递减，保证文末来源/时间不被截断（总长 ≤ 3000）
        shown = total
        text = _build(shown)
        while shown > 0 and len(text) > _MAX_CONTEXT_CHARS:
            shown -= 1
            text = _build(shown)
        if shown == 0:
            return ""
        return text
    except Exception as e:
        logger.warning(f"HIBOR 上下文构建失败: {type(e).__name__}: {e}")
        return ""


# ═══════════════════════════════════════════════════════
#  Wind 可用性判定（不臆造）
# ═══════════════════════════════════════════════════════

_WIND_TERMINAL_HINTS = (
    "C:/Wind/Wind.NET.Client",
    "C:/Wind/Wind Terminal",
    "/mnt/c/Wind/Wind.NET.Client",
    "/mnt/c/Wind/Wind Terminal",
    "/opt/Wind",
    "Wind/Wind.NET.Client",
)


def _detect_wind_terminal() -> str:
    """检测本机是否安装 Wind 金融终端，返回命中的路径或 ''。"""
    for env in ("WIND_HOME", "WIND_TERMINAL_HOME"):
        v = (os.environ.get(env) or "").strip()
        if v and Path(v).exists():
            return v
    for hint in _WIND_TERMINAL_HINTS:
        try:
            if Path(hint).exists():
                return hint
        except Exception:
            continue
    return ""


def wind_available() -> tuple:
    """检测 Wind（WindPy + 凭据 + 终端）是否可用。

    Returns:
        (available: bool, reason: str)
        不可用时 reason 写清**实测到的**原因（本机实测：无 WindPy、无凭据、无终端）。
    """
    reasons: list = []

    has_pypy = False
    try:
        has_pypy = importlib.util.find_spec("WindPy") is not None
    except Exception as e:
        reasons.append(f"检测 WindPy 时异常: {type(e).__name__}: {e}")
    if not has_pypy:
        reasons.append("WindPy 未安装（import WindPy 失败：ModuleNotFoundError: No module named 'WindPy'）")

    api_key = (os.environ.get("WIND_API_KEY") or "").strip()
    wind_user = (os.environ.get("WIND_USER") or "").strip()
    if not api_key:
        reasons.append("环境变量 WIND_API_KEY 未设置")
    if not wind_user:
        reasons.append("环境变量 WIND_USER 未设置")

    terminal = _detect_wind_terminal()
    if not terminal:
        reasons.append("未检测到本机 Wind 金融终端安装（无 WIND_HOME，且 C:/Wind、/opt/Wind 均不存在）")

    if reasons:
        return False, "；".join(reasons)
    return True, "WindPy 可导入、凭据环境变量已设置、检测到 Wind 终端"


def wind_reports(symbol: str) -> dict:
    """Wind 研报数据。

    Wind **不可用**时返回 {'available': False, 'reason': ...}，并明确不返回任何数据
    （reports 为空、无 forecast/rating 等字段），绝不臆造。

    Wind **可用**时需要什么：付费的 Wind 金融终端（Wind Terminal）+ 账号授权
    （含 WindPy 接口权限），启动终端后 w.start() 才可调用 w.wsd/w.wss/wset 等接口；
    研报明细属于 Wind「研报库」增值权限，需额外开通。本机无终端、无授权，
    因此该分支在本环境从未被验证过 —— 下面的调用是最小化 best-effort，
    任何异常都会降级为 available=False，不会返回伪造数据。
    """
    ok, reason = wind_available()
    if not ok:
        return {
            "available": False,
            "symbol": (symbol or "").strip(),
            "reason": reason,
            "reports": [],
            "note": "Wind 不可用，本接口不返回任何数据（不会用其它来源冒充 Wind）。",
        }

    try:  # pragma: no cover - 本机无 Wind，此分支不会执行
        from WindPy import w  # type: ignore

        if not getattr(w, "isconnected", lambda: False)():
            w.start()
        data = w.wsd(symbol, "west_eps_fy1,west_eps_fy2", "", "")
        return {
            "available": True,
            "symbol": symbol,
            "reason": reason,
            "reports": [],
            "raw_error_code": getattr(data, "ErrorCode", None),
            "note": (
                "WindPy 已连接；研报明细需 Wind「研报库」权限，当前仅返回一致预期类字段，"
                "研报列表需在开通研报库后另行接入。"
            ),
        }
    except Exception as e:  # pragma: no cover
        return {
            "available": False,
            "symbol": (symbol or "").strip(),
            "reason": f"WindPy 调用失败: {type(e).__name__}: {e}",
            "reports": [],
            "note": "WindPy 不可用，不返回任何数据。",
        }


# ═══════════════════════════════════════════════════════
#  async 兼容层（供 async 主线用 asyncio.to_thread 集成）
# ═══════════════════════════════════════════════════════

async def afetch_hibor_reports(channel: str = "精选研报", keyword: str = "", limit: int = 20) -> list:
    import asyncio

    return await asyncio.to_thread(fetch_hibor_reports, channel, keyword, limit)


async def afetch_hibor_for_symbol(symbol: str = "", company_name: str = "", limit: int = 15) -> list:
    import asyncio

    return await asyncio.to_thread(fetch_hibor_for_symbol, symbol, company_name, limit)


async def aget_report_detail(url: str) -> dict:
    import asyncio

    return await asyncio.to_thread(get_report_detail, url)


async def aformat_hibor_context(
    keyword: str = "", symbol: str = "", company_name: str = "", limit: int = 12
) -> str:
    import asyncio

    return await asyncio.to_thread(format_hibor_context, keyword, symbol, company_name, limit)


__all__ = [
    "HIBOR_CHANNELS",
    "fetch_hibor_reports",
    "fetch_hibor_for_symbol",
    "get_report_detail",
    "format_hibor_context",
    "wind_available",
    "wind_reports",
    "afetch_hibor_reports",
    "afetch_hibor_for_symbol",
    "aget_report_detail",
    "aformat_hibor_context",
]
