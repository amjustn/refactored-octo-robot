"""Industry Chain Research — Pre-configured industry data sources.

Integrates the industry research websites from the Douyin video:
- 艾瑞咨询 (report.iresearch.cn) — Internet/e-commerce/new energy chains
- 199it (a.199it.com) — Multi-source report aggregation
- 头豹研究院 (leadleo.com) — Emerging industry deep-dives
- 前瞻产业研究院 (qianzhan.com) — Manufacturing industry research
- 赛迪顾问 (ccidconsulting.com) — Industry consulting
- 爱范儿 (ifanr.com) — Consumer electronics
- 电子发烧友网 (elecfans.com) — Hardware technology
- 高工锂电 (gg-lb.com) — Lithium battery chain
- 光伏們 (guangfumen.com) — Solar PV chain
- IT之家 (ithome.com) — Tech news
- iFixit (ifixit.com) — Hardware teardowns

The LLM can call search_industry_chain() to get industry-specific data
from these curated sources, instead of generic web search.
"""
import logging

from .web_search import read_webpage, web_search

logger = logging.getLogger("ai_berkshire.industry_sources")

# ═══════════════════════════════════════════════════════
#  Industry source registry — curated websites by domain
# ═══════════════════════════════════════════════════════

INDUSTRY_SOURCES = {
    # 宏观行业产业链
    "iresearch": {
        "name": "艾瑞咨询",
        "domain": "report.iresearch.cn",
        "search_site": "report.iresearch.cn",
        "categories": ["互联网", "电商", "新能源", "消费", "营销"],
        "description": "自研产业链图谱，标注上下游环节与核心企业",
    },
    "199it": {
        "name": "199it互联网数据中心",
        "domain": "a.199it.com",
        "search_site": "199it.com",
        "categories": ["互联网", "电商", "餐饮", "数据", "行业报告"],
        "description": "整合多机构报告，细分赛道产业链分析",
    },
    "leadleo": {
        "name": "头豹研究院",
        "domain": "leadleo.com",
        "search_site": "leadleo.com",
        "categories": ["半导体", "AI", "新兴产业", "产业链", "竞争格局"],
        "description": "聚焦新兴产业深度产业链报告",
    },
    "qianzhan": {
        "name": "前瞻产业研究院",
        "domain": "qianzhan.com",
        "search_site": "qianzhan.com",
        "categories": ["制造业", "产业链", "行业趋势", "市场规模"],
        "description": "制造业产业链研究与市场预测",
    },
    "ccid": {
        "name": "赛迪顾问",
        "domain": "ccidconsulting.com",
        "search_site": "ccidconsulting.com",
        "categories": ["ICT", "制造业", "产业政策"],
        "description": "工信部下属产业研究机构",
    },
    # 消费电子
    "ifanr": {
        "name": "爱范儿",
        "domain": "ifanr.com",
        "search_site": "ifanr.com",
        "categories": ["消费电子", "数码", "新品", "技术"],
        "description": "消费电子产品资讯与深度分析",
    },
    "elecfans": {
        "name": "电子发烧友网",
        "domain": "elecfans.com",
        "search_site": "elecfans.com",
        "categories": ["电子", "半导体", "硬件", "电路"],
        "description": "硬件技术与电子元器件",
    },
    # 新能源
    "gg_lb": {
        "name": "高工锂电",
        "domain": "gg-lb.com",
        "search_site": "gg-lb.com",
        "categories": ["锂电池", "新能源", "电池", "电动汽车"],
        "description": "锂电池产业链专业平台",
    },
    "guangfumen": {
        "name": "光伏們",
        "domain": "guangfumen.com",
        "search_site": "guangfumen.com",
        "categories": ["光伏", "太阳能", "新能源"],
        "description": "光伏产业链专业平台",
    },
    # 科技媒体
    "ithome": {
        "name": "IT之家",
        "domain": "ithome.com",
        "search_site": "ithome.com",
        "categories": ["科技", "新品", "拆解", "软件"],
        "description": "科技新闻与产品拆解",
    },
    # 硬件拆解
    "ifixit": {
        "name": "iFixit",
        "domain": "ifixit.com",
        "search_site": "ifixit.com",
        "categories": ["拆解", "维修", "手机", "电脑", "硬件"],
        "description": "全球权威电子设备拆解库",
    },
    "chongdiantou": {
        "name": "充电头网",
        "domain": "chongdiantou.com",
        "search_site": "chongdiantou.com",
        "categories": ["充电器", "电源", "配件", "拆解"],
        "description": "电源配件拆解与芯片方案分析",
    },
}

# Category → relevant sources mapping
CATEGORY_SOURCES = {
    "互联网": ["iresearch", "199it", "ithome"],
    "电商": ["iresearch", "199it"],
    "新能源": ["iresearch", "gg_lb", "guangfumen"],
    "锂电池": ["gg_lb"],
    "光伏": ["guangfumen"],
    "半导体": ["leadleo", "elecfans"],
    "AI": ["leadleo", "199it"],
    "制造业": ["qianzhan", "ccid"],
    "消费电子": ["ifanr", "elecfans", "ithome", "ifixit"],
    "拆解": ["ifixit", "chongdiantou", "ithome"],
    "电源": ["chongdiantou"],
    "硬件": ["elecfans", "ifixit", "chongdiantou"],
}


def get_sources_for_query(query: str) -> list:
    """Match query keywords to relevant industry sources."""
    relevant = set()
    for category, source_ids in CATEGORY_SOURCES.items():
        if category in query:
            for sid in source_ids:
                relevant.add(sid)
    # If no specific match, return general research sources
    if not relevant:
        relevant = {"iresearch", "199it", "leadleo", "qianzhan"}
    return [INDUSTRY_SOURCES[sid] for sid in relevant if sid in INDUSTRY_SOURCES]


# ═══════════════════════════════════════════════════════
#  Search industry chain — uses curated sources
# ═══════════════════════════════════════════════════════

async def search_industry_chain(query: str, max_per_source: int = 3) -> dict:
    """Search for industry chain information from curated sources.

    Performs site-specific searches on the industry research websites
    that are most relevant to the query.

    Returns:
    {
        "query": str,
        "sources_searched": list of source names,
        "results": list of {title, url, snippet, source_name},
    }
    """
    if not query or not query.strip():
        return {"query": "", "error": "查询词不能为空"}

    query = query.strip()
    logger.info(f"Industry chain search: {query}")

    # Get relevant sources
    sources = get_sources_for_query(query)
    source_names = [s["name"] for s in sources]
    logger.info(f"Matched sources: {source_names}")

    all_results = []

    # Search each relevant source using site-specific search
    for source in sources:
        site = source["search_site"]
        site_query = f"{query} site:{site}"

        try:
            results = await web_search(site_query, max_results=max_per_source)
            for r in results:
                r["source_name"] = source["name"]
                r["source_domain"] = source["domain"]
                all_results.append(r)
        except Exception as e:
            logger.debug(f"Search on {site} failed: {e}")

    # Also do a general search for broader coverage
    try:
        general_results = await web_search(f"{query} 产业链", max_results=5)
        for r in general_results:
            r["source_name"] = "通用搜索"
            r["source_domain"] = ""
            all_results.append(r)
    except Exception as e:
        logger.debug(f"General search failed: {e}")

    # Deduplicate by URL
    seen_urls = set()
    unique_results = []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(r)

    return {
        "query": query,
        "sources_searched": source_names,
        "total_results": len(unique_results),
        "results": unique_results[:20],  # Limit to top 20
    }


# ═══════════════════════════════════════════════════════
#  Fetch industry report — read a specific report page
# ═══════════════════════════════════════════════════════

async def fetch_industry_report(url: str, max_chars: int = 10000) -> dict:
    """Fetch and extract content from an industry research report.

    Uses the same read_webpage function but with larger char limit
    for detailed reports.
    """
    return await read_webpage(url, max_chars=max_chars)


# ═══════════════════════════════════════════════════════
#  List available sources — for the LLM to know what's available
# ═══════════════════════════════════════════════════════

def list_industry_sources(category: str = "") -> list:
    """List available industry data sources, optionally filtered by category."""
    if not category:
        return [
            {
                "id": sid,
                "name": s["name"],
                "domain": s["domain"],
                "categories": s["categories"],
                "description": s["description"],
            }
            for sid, s in INDUSTRY_SOURCES.items()
        ]

    # Filter by category
    return [
        {
            "id": sid,
            "name": s["name"],
            "domain": s["domain"],
            "categories": s["categories"],
            "description": s["description"],
        }
        for sid, s in INDUSTRY_SOURCES.items()
        if category in s["categories"]
    ]
