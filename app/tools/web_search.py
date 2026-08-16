"""Web Search & Page Reader — lets the LLM browse the internet.

Provides two tools:
1. web_search(query) — search the web via DuckDuckGo (no API key needed)
2. read_webpage(url) — fetch any URL and extract clean text content

The LLM can autonomously decide what to search for and which pages to read,
giving it the ability to look up ANY information on the internet — not just
structured financial data.

Dependencies: httpx, beautifulsoup4, trafilatura (all installed in venv)
"""
import asyncio
import ipaddress
import logging
import re
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("ai_berkshire.web_search")

# 批次相关性阈值：批次中"含足够查询 token"的结果占比低于该值判为投毒批次
_MIN_BATCH_RELEVANCE = 0.5

# Shared HTTP client (separate from market_data_http to avoid conflicts)
_web_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _web_client
    if _web_client is not None:
        try:
            if _web_client.is_closed:
                _web_client = None
        except Exception:
            _web_client = None
    if _web_client is None:
        # 加锁防止并发请求同时创建多个客户端
        async with _client_lock:
            if _web_client is None:
                _web_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(20.0, connect=15.0),
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                )
    return _web_client


async def close_web_client():
    global _web_client
    if _web_client and not _web_client.is_closed:
        try:
            await _web_client.aclose()
        except Exception:
            pass
    _web_client = None


# ═══════════════════════════════════════════════════════
#  Web Search — Bing (primary, works in China)
# ═══════════════════════════════════════════════════════

async def web_search(query: str, max_results: int = 10) -> list:
    """Search the web using Bing's HTML interface.

    Bing works well in China without VPN. DuckDuckGo is often blocked.
    Returns a list of search results:
    [{"title": str, "url": str, "snippet": str}, ...]

    The LLM can call this to search for ANY topic — company news,
    industry reports, macro data, regulations, etc.
    """
    if not query or not query.strip():
        return []

    query = query.strip()
    logger.info(f"Web search: {query}")

    # ── 相关性守卫（批次评分制）─────────────────────────────
    # 反爬投毒特征：整批结果与查询无关（如财务查询返回"拼"字字典词条），
    # 或标题原样回声查询串。旧守卫"任意一条命中即放行"会被一条正常结果
    # 蒙混过关；升级为批次评分：命中结果占比 < _MIN_BATCH_RELEVANCE 判为
    # 投毒批次，继续回退；全部源都被投毒才返回 placeholder。
    q_norm = re.sub(r"\s+", "", query)

    def _relevance_ratio(rs):
        tokens = [t for t in re.findall(r"[一-鿿]{2,}|[A-Za-z0-9]{3,}", query)
                  if t not in ("最新", "分析", "预测", "怎么样", "如何")]
        if not tokens or not rs:
            return 1.0 if not tokens else 0.0
        need = min(2, len(tokens))  # 单 token 查询放宽到 1
        hits = 0
        for r in rs:
            title = r.get("title", "") or ""
            snippet = r.get("snippet", "") or ""
            # 标题整串回声查询 → 只信摘要（头条/视频源常见投毒手法）
            blob = snippet if (q_norm and q_norm in re.sub(r"\s+", "", title)) else title + snippet
            if sum(1 for t in tokens if t in blob) >= need:
                hits += 1
        return hits / len(rs)

    # 多源回退链（按 2026-08-16 本环境实测质量重排，3 个财务查询探针）：
    #   搜狗  — 财务查询质量最高（hits 8/8、3/3、8/8），摘要含真实数字，~1-2s
    #   360   — 财务查询稳定（7/7 全中），摘要信息量大，~1.3s
    #   百度  — 财务查询良好（7/7、6/7），~1.1-1.6s
    #   Bing  — 快（~0.5s）但财务查询易被反爬投毒（字典词条/门户页），降权
    #   头条  — 命中率虚高（标题回声查询+视频结果），仅作兜底
    #   DDG   — 本环境必超时（被墙），短超时垫底
    results = []
    for name, fn in (
        ("Sogou", _sogou_search),
        ("360", _so360_search),
        ("Baidu", _baidu_search),
        ("Bing", _bing_search),
        ("Toutiao", _toutiao_search),
        ("DuckDuckGo", _ddg_search),
    ):
        try:
            results = await fn(query, max_results)
        except Exception as e:
            logger.debug(f"{name} search error: {e}")
            results = []
        if results:
            ratio = _relevance_ratio(results)
            if ratio >= _MIN_BATCH_RELEVANCE:
                if name != "Sogou":
                    logger.info(f"Web search via fallback: {name} (relevance {ratio:.0%})")
                break
            logger.info(f"{name} batch rejected (relevance {ratio:.0%} < {_MIN_BATCH_RELEVANCE:.0%}), trying next source")
            results = []

    if not results:
        logger.warning(f"No web search results for: {query}")
        return [{
            "title": "搜索无结果",
            "url": "",
            "snippet": f"未能找到关于 '{query}' 的搜索结果。可能是网络问题或搜索词需要调整。",
            "_placeholder": True,
        }]

    logger.info(f"Web search found {len(results)} results for '{query}'")
    return results


async def _sogou_search(query: str, max_results: int = 10) -> list:
    """搜狗网页搜索(国内可用) — HTML 解析回退源"""
    client = await _get_client()
    resp = await asyncio.wait_for(
        client.get("https://www.sogou.com/web", params={"query": query},
                   headers={"Referer": "https://www.sogou.com/"}),
        timeout=8.0,
    )
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".vrwrap, .rb")[:max_results]:
        t = item.select_one("h3 a, .vr-title a")
        sn = item.select_one(".fz-mid, .space-txt, .text-layout")
        title = t.get_text(strip=True) if t else ""
        url = t.get("href", "") if t else ""
        snippet = sn.get_text(strip=True) if sn else ""
        if url.startswith("/"):
            url = "https://www.sogou.com" + url
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


async def _so360_search(query: str, max_results: int = 10) -> list:
    """360 搜索(国内可用) — HTML 解析回退源"""
    client = await _get_client()
    resp = await asyncio.wait_for(
        client.get("https://www.so.com/s", params={"q": query},
                   headers={"Referer": "https://www.so.com/"}),
        timeout=8.0,
    )
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".res-list")[:max_results]:
        t = item.select_one("h3 a")
        sn = item.select_one(".res-desc")
        title = t.get_text(strip=True) if t else ""
        url = t.get("href", "") if t else ""
        snippet = sn.get_text(strip=True) if sn else ""
        if title and url and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


async def _baidu_search(query: str, max_results: int = 10) -> list:
    """百度网页搜索(国内可用) — HTML 解析回退源

    实测(2026-08-16)财务查询质量良好(7/7 命中)，摘要含真实财报数字。
    rn 参数控制每页条数，取稍大值给守卫留出筛选余量。
    """
    client = await _get_client()
    resp = await asyncio.wait_for(
        client.get("https://www.baidu.com/s",
                   params={"wd": query, "rn": str(min(20, max_results + 5))},
                   headers={"Referer": "https://www.baidu.com/"}),
        timeout=8.0,
    )
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".result, .result-op")[:max_results]:
        t = item.select_one("h3 a")
        sn = item.select_one(".c-abstract, .cosc-source-text, [class*='content-right']")
        title = t.get_text(strip=True) if t else ""
        url = t.get("href", "") if t else ""
        snippet = sn.get_text(strip=True) if sn else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


async def _toutiao_search(query: str, max_results: int = 10) -> list:
    """头条搜索(国内可用) — HTML 解析兜底源

    实测(2026-08-16)命中率虚高：部分结果标题原样回声查询串且偏视频内容，
    相关性守卫的回声检测会过滤这类结果，仅作倒数第二兜底。
    """
    client = await _get_client()
    resp = await asyncio.wait_for(
        client.get("https://so.toutiao.com/search",
                   params={"keyword": query, "pd": "synthesis"},
                   headers={"Referer": "https://so.toutiao.com/"}),
        timeout=8.0,
    )
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".result-content, [class*='result-card'], [class*='result']")[:max_results]:
        a = item.select_one("a")
        if not a:
            continue
        title = a.get_text(strip=True)[:80]
        url = a.get("href", "")
        snippet = item.get_text(strip=True)[:300]
        if url.startswith("/"):
            url = "https://so.toutiao.com" + url
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


async def _ddg_search(query: str, max_results: int = 10) -> list:
    """DuckDuckGo HTML(中国常被阻断,短超时垫底)"""
    client = await _get_client()
    resp = await asyncio.wait_for(
        client.get("https://html.duckduckgo.com/html/", params={"q": query, "kl": "cn-zh"}),
        timeout=8.0,
    )
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".result")[:max_results]:
        title_elem = item.select_one(".result__a")
        snippet_elem = item.select_one(".result__snippet")
        title = title_elem.get_text(strip=True) if title_elem else ""
        url = title_elem.get("href", "") if title_elem else ""
        snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
        if url and "//duckduckgo.com/l/?uddg=" in url:
            import urllib.parse
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            url = parsed.get("uddg", [url])[0]
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


async def _bing_search(query: str, max_results: int = 10) -> list:
    """Search using Bing HTML interface (primary, works in China)."""
    results = []
    try:
        client = await _get_client()
        # Use cn.bing.com for better Chinese results
        resp = await client.get(
            "https://cn.bing.com/search",
            params={"q": query, "count": str(max_results), "setlang": "zh-CN"},
        )
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        # Bing results use .b_algo class
        for item in soup.select(".b_algo")[:max_results]:
            title_elem = item.select_one("h2 a")
            snippet_elem = item.select_one(".b_caption p") or item.select_one("p")

            title = title_elem.get_text(strip=True) if title_elem else ""
            url = title_elem.get("href", "") if title_elem else ""
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})

        # Fallback: try alternative Bing selectors
        if not results:
            for li in soup.select("li.b_algo, li.b_ans")[:max_results]:
                a = li.select_one("h2 a") or li.select_one("a")
                if a:
                    title = a.get_text(strip=True)
                    url = a.get("href", "")
                    p = li.select_one("p")
                    snippet = p.get_text(strip=True) if p else ""
                    if title and url:
                        results.append({"title": title, "url": url, "snippet": snippet})

    except Exception as e:
        logger.debug(f"Bing search failed: {e}")

    return results


# ═══════════════════════════════════════════════════════
#  Read Web Page — fetch any URL and extract clean text
# ═══════════════════════════════════════════════════════

# SSRF 防护配置
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 响应体上限 2MB
_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_PORTS = (None, 80, 443)  # None = 协议默认端口


async def _validate_url(url: str) -> Optional[str]:
    """SSRF 校验：URL 不允许时返回错误信息，允许时返回 None。

    只允许 http/https、80/443 端口；DNS 解析后拒绝私有/回环/链路本地/
    保留/组播地址（所有解析结果都必须通过校验）。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "URL 解析失败"
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"不允许的协议: {parsed.scheme or '(缺失)'}"
    host = parsed.hostname
    if not host:
        return "URL 缺少主机名"
    try:
        port = parsed.port
    except ValueError:
        return "非法端口号"
    if port not in _ALLOWED_PORTS:
        return f"不允许的端口: {port}"

    # DNS 解析并校验全部结果，防止指向内网/回环地址
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host, port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror:
        return f"DNS 解析失败: {host}"
    except Exception as e:
        return f"DNS 解析异常: {e}"

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return f"非法 IP 地址: {ip_str}"
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return f"目标地址为内网/保留地址 ({ip_str})，已拒绝"
    return None


async def _fetch_html(client: httpx.AsyncClient, url: str) -> tuple:
    """抓取 URL：SSRF 校验 + 手动处理一跳重定向（重新校验）+ 2MB 响应体上限。

    注意（残余 TOCTOU 窗口）：DNS 校验与 TCP 连接之间目标域名可能被重解析到
    内网地址（DNS rebinding）。彻底修复需在连接层钉住已校验 IP；当前校验 +
    逐跳重校验已覆盖绝大多数场景，接受该残余风险并记录于此。

    Returns: (status_code, headers, body_bytes)
    Raises: ValueError — URL 被拒绝 / 响应体过大 / 重定向过多
    """
    current = url
    for _hop in range(2):  # 原始请求 + 最多一跳重定向
        err = await _validate_url(current)
        if err:
            raise ValueError(err)
        async with client.stream("GET", current, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if not location:
                    return resp.status_code, resp.headers, b""
                current = urljoin(current, location)
                continue  # 下一跳会重新走 SSRF 校验
            content_length = resp.headers.get("content-length", "")
            if content_length.isdigit() and int(content_length) > _MAX_RESPONSE_BYTES:
                raise ValueError("响应体超过 2MB 上限，已拒绝")
            chunks = []
            size = 0
            async for chunk in resp.aiter_bytes(65536):
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise ValueError("响应体超过 2MB 上限，已拒绝")
                chunks.append(chunk)
            return resp.status_code, resp.headers, b"".join(chunks)
    raise ValueError("重定向次数过多")


async def read_webpage(url: str, max_chars: int = 8000) -> dict:
    """Fetch a web page and extract clean, readable text content.

    Uses trafilatura for high-quality article extraction.
    Falls back to BeautifulSoup if trafilatura fails.

    Returns:
    {
        "url": str,
        "title": str,
        "content": str (clean text, truncated to max_chars),
        "word_count": int,
    }

    The LLM can call this to read ANY web page — annual reports,
    news articles, research papers, etc.
    """
    if not url or not url.strip():
        return {"url": "", "error": "URL is required"}

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # E8: defense in depth — clamp max_chars here too (the API layer clamps
    # as well) so direct tool calls cannot request unbounded page text.
    max_chars = max(100, min(int(max_chars), 20000))

    logger.info(f"Reading webpage: {url}")

    # SSRF 预校验（重定向目标在 _fetch_html 内逐跳重新校验）
    err = await _validate_url(url)
    if err:
        return {"url": url, "error": f"URL 被拒绝: {err}"}

    try:
        client = await _get_client()
        status_code, headers, raw = await _fetch_html(client, url)

        if status_code != 200:
            return {
                "url": url,
                "error": f"HTTP {status_code}",
                "status_code": status_code,
            }

        html = raw.decode("utf-8", errors="replace")
        content_type = headers.get("content-type", "")

        # Extract title
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        if soup.title:
            title = soup.title.string.strip() if soup.title.string else ""

        # Try trafilatura for high-quality text extraction
        text = ""
        try:
            import trafilatura
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            ) or ""
        except Exception as e:
            logger.debug(f"Trafilatura extraction failed: {e}")

        # Fallback: use BeautifulSoup to extract text
        if not text:
            # Remove script, style, nav, footer tags
            for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            # Get remaining text
            text = soup.get_text(separator="\n", strip=True)
            # Clean up excessive whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r" {2,}", " ", text)

        if not text:
            return {
                "url": url,
                "title": title,
                "error": "Could not extract text content from page",
                "content_type": content_type,
            }

        # Truncate to max_chars
        original_length = len(text)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... 内容已截断，原文共 {original_length} 字符 ...]"

        word_count = len(text)

        result = {
            "url": url,
            "title": title,
            "content": text,
            "word_count": word_count,
            "content_type": content_type,
        }

        logger.info(f"Read webpage: {title[:50]}... ({word_count} chars)")
        return result

    except httpx.TimeoutException:
        return {"url": url, "error": "请求超时 (20s)"}
    except httpx.ConnectError as e:
        return {"url": url, "error": f"连接失败: {e}"}
    except Exception as e:
        logger.error(f"Read webpage failed for {url}: {e}")
        return {"url": url, "error": str(e)}


# ═══════════════════════════════════════════════════════
#  Convenience: search + read top result in one call
# ═══════════════════════════════════════════════════════

async def search_and_read(query: str, read_top: int = 2, max_chars_per_page: int = 4000) -> dict:
    """Search the web and read the top N results.

    Convenience function for the LLM: search + read in one call.
    Returns search results plus content of top pages.
    """
    search_results = await web_search(query, max_results=10)

    pages = []
    for result in search_results[:read_top]:
        if result.get("url") and not result.get("_placeholder"):
            page = await read_webpage(result["url"], max_chars=max_chars_per_page)
            pages.append(page)

    return {
        "query": query,
        "search_results": search_results,
        "pages_read": pages,
        "summary": f"搜索 '{query}' 找到 {len(search_results)} 条结果，已读取 {len(pages)} 个页面",
    }
