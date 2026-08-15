"""Direct industry website crawler — fetch latest articles from each site.

Instead of relying on Bing site: search (which often returns irrelevant results
for Chinese niche websites), this module directly fetches each industry
website's article listing page and extracts the latest articles.

This gives us REAL latest news from:
- 艾瑞咨询 (iresearch.cn) — Internet/e-commerce reports
- 头豹研究院 (leadleo.com) — Emerging industry reports
- 前瞻产业研究院 (qianzhan.com) — Manufacturing industry
- 高工锂电 (gg-lb.com) — Lithium battery news
- 光伏們 (guangfumen.com) — Solar PV news
- IT之家 (ithome.com) — Tech news
- 爱范儿 (ifanr.com) — Consumer electronics
- 电子发烧友 (elecfans.com) — Hardware/electronics
"""
import logging

from bs4 import BeautifulSoup

from .web_search import _fetch_html, _get_client

logger = logging.getLogger("ai_berkshire.site_crawler")

# ═══════════════════════════════════════════════════════
#  Site crawl configurations — URL + parsing rules
# ═══════════════════════════════════════════════════════

SITE_CONFIGS = {
    "iresearch": {
        "name": "艾瑞咨询",
        "list_url": "https://www.iresearch.com.cn/",
        "article_selector": ".report-list .report-item, .list-item, .article-item, ul li, .news-list li",
        "title_selector": "a, .title, h3, h4",
        "link_selector": "a",
        "date_selector": ".date, .time, time",
        "categories": ["互联网", "电商", "新能源", "消费", "营销"],
    },
    "qianzhan": {
        "name": "前瞻产业研究院",
        "list_url": "https://bg.qianzhan.com/",
        "article_selector": ".list-item, .news-item, .article-item, .news_list li, ul li",
        "title_selector": "a, .title, h3",
        "link_selector": "a",
        "date_selector": ".date, .time, time, span",
        "categories": ["制造业", "产业链", "行业趋势"],
    },
    "gg_lb": {
        "name": "高工锂电",
        "list_url": "https://www.gg-lb.com/",
        "article_selector": ".news-list li, .list-item, article, ul li, li",
        "title_selector": "a, .title, h3",
        "link_selector": "a",
        "date_selector": ".date, .time, time, span",
        "categories": ["锂电池", "新能源", "电池", "电动汽车"],
    },
    "ithome": {
        "name": "IT之家",
        "list_url": "https://www.ithome.com/",
        "article_selector": ".lst > li, .news_list li, .block a, ul.blk li",
        "title_selector": "a, .title, h2, h3",
        "link_selector": "a",
        "date_selector": ".date, .time, time, span",
        "categories": ["科技", "新品", "拆解", "软件"],
    },
    "ifanr": {
        "name": "爱范儿",
        "list_url": "https://www.ifanr.com/",
        "article_selector": "article, .post-item, .article-item, .card",
        "title_selector": "h2 a, h3 a, .title a, a",
        "link_selector": "a[href]",
        "date_selector": ".date, .time, time, span",
        "categories": ["消费电子", "数码", "新品", "技术"],
    },
    "elecfans": {
        "name": "电子发烧友",
        "list_url": "https://www.elecfans.com/",
        "article_selector": ".news-list li, .article-item, .list-item, li",
        "title_selector": "a, .title, h3",
        "link_selector": "a",
        "date_selector": ".date, .time, time, span",
        "categories": ["电子", "半导体", "硬件", "电路"],
    },
    "leadleo": {
        "name": "头豹研究院",
        "list_url": "https://www.leadleo.com/wiki/index.html",
        "article_selector": ".report-item, .list-item, .card, li",
        "title_selector": "a, .title, h3",
        "link_selector": "a",
        "date_selector": ".date, .time, time, span",
        "categories": ["半导体", "AI", "新兴产业", "产业链"],
    },
    "guangfumen": {
        "name": "光伏們",
        "list_url": "https://www.guangfumen.com/",
        "article_selector": ".news-list li, .article-item, .list-item, article, li",
        "title_selector": "a, .title, h3",
        "link_selector": "a",
        "date_selector": ".date, .time, time, span",
        "categories": ["光伏", "太阳能", "新能源"],
    },
}

# Category → site mapping
CATEGORY_SITES = {
    "半导体": ["leadleo", "elecfans"],
    "电子": ["elecfans", "ithome"],
    "锂电池": ["gg_lb"],
    "新能源": ["gg_lb", "guangfumen", "iresearch"],
    "光伏": ["guangfumen"],
    "消费电子": ["ifanr", "ithome"],
    "科技": ["ithome", "ifanr"],
    "互联网": ["iresearch"],
    "电商": ["iresearch"],
    "制造业": ["qianzhan"],
    "产业链": ["qianzhan", "leadleo", "iresearch"],
}


# ═══════════════════════════════════════════════════════
#  Crawl a single site — fetch and parse article list
# ═══════════════════════════════════════════════════════

async def crawl_site(site_id: str, max_articles: int = 10) -> list:
    """Fetch latest articles from a specific industry website.

    Directly visits the site's article listing page and extracts
    article titles, URLs, and dates.

    Returns:
    [{"title": str, "url": str, "date": str, "source": str}, ...]
    """
    config = SITE_CONFIGS.get(site_id)
    if not config:
        return []

    url = config["list_url"]
    name = config["name"]
    logger.info(f"Crawling {name}: {url}")

    articles = []
    try:
        # E8: route through the SSRF-checked, size-capped fetcher instead of a
        # raw client.get (list URLs come from SITE_CONFIGS, but defense in
        # depth costs nothing here).
        client = await _get_client()
        status_code, _headers, raw = await _fetch_html(client, url)

        if status_code != 200:
            logger.debug(f"{name} returned {status_code}")
            return []

        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")

        # Try configured selectors first
        items = soup.select(config["article_selector"])

        # Fallback: find all links with meaningful text
        if not items:
            items = soup.find_all("a")
            items = [a for a in items if len(a.get_text(strip=True)) > 8][:max_articles * 2]

        for item in items[:max_articles * 2]:
            title_elem = item
            # Try to find title within item
            for sel in config["title_selector"].split(", "):
                found = item.select_one(sel.strip()) if hasattr(item, 'select_one') else None
                if found:
                    title_elem = found
                    break

            title = title_elem.get_text(strip=True) if hasattr(title_elem, 'get_text') else str(title_elem)

            # Find URL
            link_elem = item if item.name == "a" else item.select_one("a")
            href = link_elem.get("href", "") if link_elem and hasattr(link_elem, 'get') else ""

            # Make absolute URL
            if href and not href.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                href = urljoin(url, href)

            # Skip non-article links
            skip_patterns = ["javascript:", "#", "mailto:", ".css", ".js", ".png", ".jpg"]
            if not href or any(p in href for p in skip_patterns):
                continue

            # Skip navigation/utility links
            if len(title) < 6:
                continue
            if title in ["首页", "登录", "注册", "更多", "返回顶部", "关于我们", "联系"]:
                continue

            # Find date
            date = ""
            for sel in config["date_selector"].split(", "):
                date_elem = item.select_one(sel.strip()) if hasattr(item, 'select_one') else None
                if date_elem:
                    date = date_elem.get_text(strip=True)
                    break

            # Deduplicate
            if any(a["url"] == href for a in articles):
                continue

            articles.append({
                "title": title,
                "url": href,
                "date": date,
                "source": name,
            })

            if len(articles) >= max_articles:
                break

    except ValueError as ve:
        # E8: URL rejected by SSRF validation / response too large
        logger.debug(f"Crawl {name} rejected: {ve}")
        return []
    except Exception as e:
        logger.error(f"Crawl {name} failed: {e}")

    logger.info(f"{name}: found {len(articles)} articles")
    return articles


# ═══════════════════════════════════════════════════════
#  Crawl multiple sites by category
# ═══════════════════════════════════════════════════════

async def crawl_industry_news(query: str, max_per_site: int = 5) -> dict:
    """Crawl latest industry news from relevant websites.

    Matches the query to relevant industry sources and directly
    fetches their latest articles (no search engine needed).

    Returns:
    {
        "query": str,
        "sites_crawled": list,
        "articles": list of {title, url, date, source},
    }
    """
    relevant_sites = set()
    # Match query keywords to site categories
    for category, site_ids in CATEGORY_SITES.items():
        if category in query:
            for sid in site_ids:
                relevant_sites.add(sid)

    # If no specific match, crawl general sites
    if not relevant_sites:
        relevant_sites = {"ithome", "ifanr", "qianzhan"}

    site_names = [SITE_CONFIGS[sid]["name"] for sid in relevant_sites if sid in SITE_CONFIGS]
    logger.info(f"Crawling {len(relevant_sites)} sites for '{query}': {site_names}")

    all_articles = []

    for site_id in relevant_sites:
        if site_id not in SITE_CONFIGS:
            continue
        articles = await crawl_site(site_id, max_articles=max_per_site)
        all_articles.extend(articles)

    # Filter articles by query keywords
    if all_articles:
        query_words = set(query.replace("产业链", "").replace("行业", "").split())
        query_words = {w for w in query_words if len(w) >= 2}
        if query_words:
            filtered = []
            for a in all_articles:
                title = a.get("title", "")
                if any(w in title for w in query_words):
                    filtered.append(a)
            if len(filtered) >= 3:
                all_articles = filtered

    return {
        "query": query,
        "sites_crawled": site_names,
        "total_articles": len(all_articles),
        "articles": all_articles[:20],
    }


# ═══════════════════════════════════════════════════════
#  Build context string for report generation
# ═══════════════════════════════════════════════════════

async def build_industry_news_context(query: str) -> str:
    """Build a context string with latest industry news from direct crawling.

    This complements web_search by providing articles directly from
    industry-specific websites, not just search engine results.
    """
    result = await crawl_industry_news(query, max_per_site=5)

    if not result["articles"]:
        return ""

    parts = ["### 行业网站最新文章（直接爬取）"]
    parts.append(f"*来源: {', '.join(result['sites_crawled'])}*")
    parts.append("")

    for i, article in enumerate(result["articles"][:10]):
        title = article.get("title", "")
        url = article.get("url", "")
        date = article.get("date", "")
        source = article.get("source", "")
        parts.append(f"- [{source}] {title}")
        if date:
            parts.append(f"  日期: {date}")
        parts.append(f"  URL: {url[:80]}")
    parts.append("")

    return "\n".join(parts)
