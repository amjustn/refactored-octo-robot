"""定期报告正文摘录 —— 把巨潮公告 PDF 里的"董监高/持股/薪酬"章节抽出来注入上下文。

为什么需要这个模块（2026-09-13 静态审计发现 + 本机实测）：
- `announcements.py` 只给元数据（披露日/标题/链接）；正文读不到：
    东财公告详情页 → read_webpage 只拿到 51 字符通用免责声明；
    巨潮公告详情页 → 264 字符全是 `{{stockName}}` 这类 Vue 占位符（JS 空壳）。
- 但正文本身是可拿的：巨潮静态 PDF 模板实测可用（半年度报告 196 页 3.29MB、
  2025 年报 248 页 3.72MB，均 HTTP 200）。
- 而这正是报告里反复写"需要核实"的地方。实测 2025 年报里有整节
  「五、董事和高级管理人员的情况」（现任/离任董监高与核心技术人员的持股变动及薪酬、
  报告期变动情况），可回答"高管名单、持股变动、薪酬"这类缺口。

实现要点：
- PDF 地址由巨潮公告链接里的 announcementId + announcementTime 拼出：
    http://static.cninfo.com.cn/finalpage/{YYYY-MM-DD}/{announcementId}.PDF
- 章节定位用"字符间允许空白"的正则（PDF 抽文本会按行断字，
  `董事、监事、高级管理人员情况` 在原始文本里可能被换行切开）。
- 只缓存抽取后的章节摘录（不是整份 20 万字符正文），category
  `announcement_doc_sections`，TTL 30 天：同一份年报只下载/解析一次。
- 下载慢（实测同一文件 4.4s~35s 波动），故：大小上限 40MB、超时参数化、
  失败一律返回空并在日志写明原因，绝不编造正文。
"""
from __future__ import annotations

import logging
import re
import urllib.request
from typing import Optional

from ..core.data_cache import DataCache

logger = logging.getLogger("ai_berkshire.announcement_docs")

CACHE_CATEGORY = "announcement_doc_sections"
CACHE_TTL = 30 * 86400          # 30 天：定期报告内容不会变
PDF_URL_TPL = "http://static.cninfo.com.cn/finalpage/{date}/{aid}.PDF"
MAX_PDF_BYTES = 40 * 1024 * 1024
DEFAULT_TIMEOUT = 60
CACHE_EXCERPT_CHARS = 6000      # 缓存固定截取长度（取用时再按调用方需求截断）

# 章节锚点：上交所/科创板年报模板用「董事和高级管理人员的情况」，
# 主板模板用「董事、监事、高级管理人员情况」；再退到持股/薪酬表标题。
MGMT_ANCHORS = (
    "董事和高级管理人员的情况",
    "董事、监事和高级管理人员情况",
    "董事、监事、高级管理人员情况",
    "董事、监事及高级管理人员情况",
    "现任及报告期内离任董事、高级管理人员和核心技术人员持股变动及薪酬情况",
    "持股变动及薪酬情况",
    "高级管理人员情况",
)

_cache: Optional[DataCache] = None


def _get_cache() -> DataCache:
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache


# ═══════════════════════════════════════════════════════
#  链接解析
# ═══════════════════════════════════════════════════════

def parse_announcement_id(link: str) -> str:
    """从巨潮公告链接里取 announcementId；取不到返回 ''。"""
    if not link:
        return ""
    m = re.search(r"[?&]announcementId=(\d+)", str(link), re.I)
    return m.group(1) if m else ""


def parse_announcement_date(link: str) -> str:
    """从巨潮公告链接里取公告日期 YYYY-MM-DD；取不到返回 ''。"""
    if not link:
        return ""
    m = re.search(r"(?:announcementTime=|/finalpage/)(\d{4}-\d{2}-\d{2})", str(link), re.I)
    return m.group(1) if m else ""


def build_pdf_url(link: str) -> str:
    """由巨潮公告链接拼静态 PDF 地址；信息不全返回 ''。"""
    aid = parse_announcement_id(link)
    date = parse_announcement_date(link)
    if not aid or not date:
        return ""
    return PDF_URL_TPL.format(date=date, aid=aid)


# ═══════════════════════════════════════════════════════
#  PDF 下载与文本抽取
# ═══════════════════════════════════════════════════════

def download_pdf_text(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """下载 PDF 并抽取全文文本；失败返回 '' 并记 warning。"""
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"定期报告 PDF HTTP {resp.status}: {url}")
                return ""
            data = resp.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            logger.warning(f"定期报告 PDF 超过 {MAX_PDF_BYTES // 1024 // 1024}MB 上限，跳过: {url}")
            return ""
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            text = "\n".join(str(page.get_text()) for page in doc)
        finally:
            doc.close()
        logger.info(f"定期报告 PDF 解析完成: {len(data)/1e6:.2f}MB -> {len(text)} 字符")
        return text
    except Exception as e:  # noqa: BLE001 - 网络/解析失败都不应打断上下文构建
        logger.warning(f"定期报告 PDF 获取失败 ({type(e).__name__}): {str(e)[:120]} url={url}")
        return ""


def _anchor_re(anchor: str) -> re.Pattern:
    """字符间允许空白/换行的锚点正则（PDF 抽文本会按行断字）。"""
    return re.compile(r"\s*".join(re.escape(ch) for ch in anchor))


def find_section(text: str, anchors=MGMT_ANCHORS, max_chars: int = 3000) -> tuple:
    """定位章节并截取 max_chars。

    返回 (anchor, excerpt)；未命中返回 ('', '')。
    """
    if not text:
        return "", ""
    for anchor in anchors:
        m = _anchor_re(anchor).search(text)
        if not m:
            continue
        excerpt = text[m.start(): m.start() + max_chars]
        # 折叠 PDF 抽文本里的碎空白，保留换行，便于大模型阅读
        excerpt = re.sub(r"[ \t\u3000]{2,}", " ", excerpt)
        excerpt = re.sub(r"\n{3,}", "\n\n", excerpt)
        return anchor, excerpt.strip()
    return "", ""


def _clean_excerpt(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())


# ═══════════════════════════════════════════════════════
#  对外：拿到"董监高/持股/薪酬"摘录
# ═══════════════════════════════════════════════════════

def get_management_excerpt(
    symbol: str,
    company_name: str = "",
    max_chars: int = 3000,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """取最近一期定期报告里的董监高/持股/薪酬章节摘录。

    返回 dict：
      {ok, symbol, title, date, pdf_url, anchor, excerpt, cached, note}
    ok=False 时 excerpt 为空、note 写明原因（网络失败/未命中章节/港股美股不覆盖）。
    """
    out = {
        "ok": False, "symbol": symbol or "", "title": "", "date": "",
        "pdf_url": "", "anchor": "", "excerpt": "", "cached": False, "note": "",
    }
    try:
        from .announcements import get_disclosure_reports, normalize_symbol

        code = normalize_symbol(symbol or "")
        if not code:
            out["note"] = "定期报告正文仅覆盖A股（巨潮资讯）；港股/美股请用 web_search"
            return out
        records = get_disclosure_reports(code, limit=8) or []
        # 年报优先（董监高章节最全），其次半年报
        order = {"年报": 0, "半年报": 1}
        cands = [r for r in records if r.get("kind") in order]
        cands.sort(key=lambda r: (order.get(r.get("kind"), 9), r.get("date") or ""), reverse=False)
        if not cands:
            out["note"] = "未取到年报/半年报记录（巨潮接口无返回）"
            return out

        cache = _get_cache()
        for rec in cands:
            pdf_url = build_pdf_url(rec.get("url") or "")
            if not pdf_url:
                continue
            # 缓存不按 max_chars 分键（否则换个长度就重新下载一份 3MB PDF），
            # 统一按 CACHE_EXCERPT_CHARS 缓存，取用时再截断。
            key = f"{parse_announcement_id(rec.get('url') or '')}|mgmt"
            cached = None
            try:
                cached = cache.get(CACHE_CATEGORY, key, max_age_seconds=CACHE_TTL)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"章节缓存读取失败: {e}")
            if isinstance(cached, dict) and cached.get("excerpt"):
                out.update({
                    "ok": True, "title": cached.get("title", rec.get("title", "")),
                    "date": cached.get("date", rec.get("date", "")), "pdf_url": pdf_url,
                    "anchor": cached.get("anchor", ""),
                    "excerpt": cached["excerpt"][:max_chars],
                    "cached": True, "note": "",
                })
                return out

            text = download_pdf_text(pdf_url, timeout=timeout)
            if not text:
                continue
            anchor, excerpt = find_section(text, max_chars=CACHE_EXCERPT_CHARS)
            if not excerpt:
                logger.info(f"定期报告未命中董监高章节: {rec.get('title')}")
                continue
            payload = {
                "title": rec.get("title", ""), "date": rec.get("date", ""),
                "anchor": anchor, "excerpt": excerpt,
            }
            try:
                cache.set(CACHE_CATEGORY, key, payload)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"章节缓存写入失败: {e}")
            out.update({
                "ok": True, "title": payload["title"], "date": payload["date"],
                "pdf_url": pdf_url, "anchor": anchor, "excerpt": excerpt[:max_chars],
                "cached": False, "note": "",
            })
            return out

        out["note"] = "已取到定期报告链接，但正文下载或章节定位未成功（详见日志）"
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"定期报告摘录失败 ({type(e).__name__}): {str(e)[:150]}")
        out["note"] = f"取数异常：{type(e).__name__}"
        return out


def build_doc_context(symbol: str, company_name: str = "", max_chars: int = 3000) -> str:
    """生成注入文本块；拿不到正文返回 ''（不编造）。"""
    r = get_management_excerpt(symbol, company_name=company_name, max_chars=max_chars)
    if not r.get("ok") or not r.get("excerpt"):
        return ""
    name = (company_name or "").strip() or r.get("symbol", "")
    head = "## 定期报告正文摘录（董监高 / 持股 / 薪酬）"
    meta = (f"*来源：巨潮资讯《{r.get('title') or '定期报告'}》（{r.get('date') or '日期未知'}）"
            f"章节「{r.get('anchor') or '未标注'}」｜原文 PDF：{r.get('pdf_url')}*")
    tail = ("（以上为公告原文摘录，引用时请标注来源：巨潮资讯 <公告标题> <日期>；"
            "本摘录按长度截断，完整章节见原文 PDF。）")
    return "\n".join([head, meta, "", f"**{name}**", r["excerpt"], "", tail])


async def aget_management_excerpt(symbol: str, company_name: str = "", max_chars: int = 3000,
                                  timeout: int = DEFAULT_TIMEOUT) -> dict:
    import asyncio
    return await asyncio.to_thread(
        get_management_excerpt, symbol, company_name, max_chars, timeout
    )


async def abuild_doc_context(symbol: str, company_name: str = "", max_chars: int = 3000) -> str:
    import asyncio
    return await asyncio.to_thread(build_doc_context, symbol, company_name, max_chars)
