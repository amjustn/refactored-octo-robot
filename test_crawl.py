#!/usr/bin/env python3
"""Test direct site crawling."""
import sys, asyncio
sys.path.insert(0, "/home/q/ai_berkshire_web")

async def test():
    from app.tools.site_crawler import crawl_site, crawl_industry_news

    # Test individual sites
    for sid in ["ithome", "ifanr", "qianzhan", "gg_lb"]:
        articles = await crawl_site(sid, max_articles=5)
        name = articles[0]["source"] if articles else sid
        print(f"\n{'='*50}")
        print(f"📡 {name}: {len(articles)} 篇文章")
        print(f"{'='*50}")
        for a in articles[:3]:
            print(f"  {a['title'][:45]}")
            print(f"  URL: {a['url'][:70]}")

    # Test multi-site crawl
    print(f"\n{'='*50}")
    print(f"📡 多源爬取: 锂电池新能源")
    print(f"{'='*50}")
    result = await crawl_industry_news("锂电池新能源", max_per_site=3)
    print(f"总文章: {result['total_articles']}")
    print(f"来源: {result['sites_crawled']}")
    for a in result["articles"][:8]:
        print(f"  [{a['source']}] {a['title'][:45]}")

    from app.tools.web_search import close_web_client
    await close_web_client()
    print("\n✅ Done!")

asyncio.run(test())
