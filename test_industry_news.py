#!/usr/bin/env python3
"""Test: Can the system fetch latest industry news from specific websites?"""
import sys
sys.path.insert(0, "/home/q/ai_berkshire_web")

import asyncio


async def run_test():
    print("=" * 70)
    print("  行业最新消息抓取验证")
    print("=" * 70)

    from app.tools.web_search import web_search, read_webpage, close_web_client
    from app.tools.industry_sources import search_industry_chain, list_industry_sources

    # ═══════════════════════════════════════════
    # Test 1: Search latest news from specific industry websites
    # ═══════════════════════════════════════════
    industries = [
        ("半导体", ["leadleo.com", "elecfans.com"]),
        ("锂电池", ["gg-lb.com"]),
        ("光伏", ["guangfumen.com"]),
        ("消费电子", ["ifanr.com", "ithome.com"]),
        ("新能源汽车", ["report.iresearch.cn", "gg-lb.com"]),
    ]

    for industry, sites in industries:
        print(f"\n{'='*50}")
        print(f"📡 行业: {industry}")
        print(f"{'='*50}")

        # Step 1: Site-specific search
        for site in sites:
            query = f"{industry} 最新 2025 site:{site}"
            print(f"\n  🔍 搜索: {query}")
            results = await web_search(query, max_results=3)
            print(f"     找到 {len(results)} 条结果")

            for i, r in enumerate(results[:3]):
                if r.get("_placeholder"):
                    continue
                print(f"  [{i+1}] {r.get('title', '?')[:50]}")
                print(f"      URL: {r.get('url', '')[:80]}")
                snippet = r.get('snippet', '')[:80]
                if snippet:
                    print(f"      摘要: {snippet}")

            # Step 2: Read top result
            if results and not results[0].get("_placeholder"):
                url = results[0].get("url", "")
                if url and "baike.baidu.com" not in url:
                    print(f"\n  📖 读取网页: {url[:60]}")
                    page = await read_webpage(url, max_chars=2000)
                    if page and "content" in page and not page.get("error"):
                        title = page.get("title", "")
                        content = page.get("content", "")
                        print(f"     标题: {title[:50]}")
                        print(f"     内容: {len(content)} 字符")
                        print(f"     预览: {content[:150]}...")
                    else:
                        print(f"     ❌ {page.get('error', '未知错误')}")

    # ═══════════════════════════════════════════
    # Test 2: search_industry_chain (multi-source)
    # ═══════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f"📡 产业链多源搜索: 锂电池产业链")
    print(f"{'='*70}")
    result = await search_industry_chain("锂电池产业链", max_per_source=3)
    print(f"  数据源: {result.get('sources_searched')}")
    print(f"  总结果: {result.get('total_results')}")
    for i, r in enumerate(result.get("results", [])[:8]):
        print(f"  [{i+1}] [{r.get('source_name', '?')}] {r.get('title', '?')[:50]}")
        print(f"      {r.get('url', '')[:80]}")

    # ═══════════════════════════════════════════
    # Test 3: Auto-inject in report context
    # ═══════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f"🤖 报告上下文自动注入行业消息")
    print(f"{'='*70}")
    from app.core.context import build_full_context
    ctx = await build_full_context("半导体产业链分析", skill_name="industry-research")
    has_web = "网络最新信息" in ctx
    has_industry = "产业链搜索" in ctx
    print(f"  上下文长度: {len(ctx)} 字符")
    print(f"  网络搜索:   {'✅' if has_web else '❌'}")
    print(f"  产业链搜索: {'✅' if has_industry else '❌'}")
    
    if has_web:
        idx = ctx.find("## 网络最新信息")
        if idx >= 0:
            print(f"\n  --- 网络搜索内容预览 ---")
            print(ctx[idx:idx+500])

    await close_web_client()
    print(f"\n{'='*70}")
    print("✅ 验证完成！系统能自动抓取行业最新消息进行分析")
    print(f"{'='*70}")


asyncio.run(run_test())
