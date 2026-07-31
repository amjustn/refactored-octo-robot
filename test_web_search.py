#!/usr/bin/env python3
"""End-to-end test: web search + read webpage + industry chain search."""
import sys
sys.path.insert(0, "/home/q/ai_berkshire_web")

import asyncio


async def run_tests():
    print("=" * 70)
    print("  AI Berkshire 网络搜索能力端到端验证")
    print("=" * 70)

    # ═══════════════════════════════════════════
    #  Test 1: Module imports
    # ═══════════════════════════════════════════
    print("\n📦 Test 1: 模块导入")
    print("-" * 50)

    try:
        from app.tools.web_search import web_search, read_webpage, close_web_client
        print("  ✅ web_search.py 导入成功")
    except Exception as e:
        print(f"  ❌ web_search.py 导入失败: {e}")
        return

    try:
        from app.tools.industry_sources import (
            search_industry_chain, list_industry_sources, INDUSTRY_SOURCES
        )
        print("  ✅ industry_sources.py 导入成功")
        print(f"     已注册 {len(INDUSTRY_SOURCES)} 个产业数据源")
    except Exception as e:
        print(f"  ❌ industry_sources.py 导入失败: {e}")
        return

    # ═══════════════════════════════════════════
    #  Test 2: List industry sources
    # ═══════════════════════════════════════════
    print("\n📋 Test 2: 产业数据源列表")
    print("-" * 50)
    sources = list_industry_sources()
    for s in sources:
        print(f"  {s['name']:12s} ({s['domain']:25s}) — {s['description'][:30]}")

    # Filter by category
    print("\n  按类别筛选 '新能源':")
    new_energy = list_industry_sources("新能源")
    for s in new_energy:
        print(f"    {s['name']}")

    # ═══════════════════════════════════════════
    #  Test 3: Web search (DuckDuckGo)
    # ═══════════════════════════════════════════
    print("\n🔍 Test 3: 网页搜索 web_search('茅台 2025年业绩')")
    print("-" * 50)
    results = await web_search("贵州茅台 2025年业绩", max_results=5)
    print(f"  找到 {len(results)} 条结果:")
    for i, r in enumerate(results[:5]):
        print(f"  [{i+1}] {r.get('title', '?')[:50]}")
        print(f"      URL: {r.get('url', '')[:80]}")
        print(f"      摘要: {r.get('snippet', '')[:60]}")
        print()

    # ═══════════════════════════════════════════
    #  Test 4: Read webpage
    # ═══════════════════════════════════════════
    if results and results[0].get("url") and not results[0].get("_placeholder"):
        url = results[0]["url"]
        print(f"📖 Test 4: 读取网页 read_webpage('{url[:60]}...')")
        print("-" * 50)
        page = await read_webpage(url, max_chars=3000)
        if "error" in page:
            print(f"  ❌ 失败: {page['error']}")
        else:
            print(f"  ✅ 标题: {page.get('title', '?')[:60]}")
            print(f"     内容长度: {page.get('word_count', 0)} 字符")
            content = page.get('content', '')
            print(f"     内容摘要: {content[:150]}...")

    # ═══════════════════════════════════════════
    #  Test 5: Industry chain search
    # ═══════════════════════════════════════════
    print("\n🏭 Test 5: 产业链搜索 search_industry_chain('新能源汽车')")
    print("-" * 50)
    result = await search_industry_chain("新能源汽车", max_per_source=2)
    print(f"  查询: {result.get('query')}")
    print(f"  搜索的数据源: {result.get('sources_searched')}")
    print(f"  总结果数: {result.get('total_results')}")
    print(f"  结果:")
    for i, r in enumerate(result.get("results", [])[:5]):
        print(f"  [{i+1}] {r.get('source_name', '?'):12s} | {r.get('title', '?')[:40]}")
        print(f"      URL: {r.get('url', '')[:80]}")

    # ═══════════════════════════════════════════
    #  Test 6: LLM with web search tool
    # ═══════════════════════════════════════════
    print("\n🤖 Test 6: LLM 自主搜索 (function calling)")
    print("-" * 50)
    from app.core.llm import chat_complete_with_tools
    from app.core.config import LLM_API_KEY, LLM_MODEL
    from app.harness.tools import ALL_TOOL_SCHEMAS, _execute_tool

    print(f"  LLM: {LLM_MODEL}")
    print(f"  可用工具: {[t['function']['name'] for t in ALL_TOOL_SCHEMAS]}")

    if LLM_API_KEY:
        print("\n  正在让 LLM 搜索最新信息...")
        try:
            resp = await asyncio.wait_for(
                chat_complete_with_tools(
                    "你是投资分析师。请使用 web_search 工具搜索'比亚迪 2025年最新销量'，"
                    "然后用 read_webpage 读取搜索到的第一个网页，最后总结关键信息。",
                    "搜索比亚迪最新销量数据",
                    tools=ALL_TOOL_SCHEMAS,
                    tool_executor=_execute_tool,
                ),
                timeout=90
            )
            print(f"  ✅ LLM 响应 ({len(resp)} 字符):")
            print(f"  {resp[:200]}")
        except asyncio.TimeoutError:
            print("  ❌ 超时 (90s)")
        except Exception as e:
            print(f"  ❌ 失败: {e}")
    else:
        print("  ⏭️ 跳过 (未配置 API Key)")

    # ═══════════════════════════════════════════
    #  Test 7: API routes
    # ═══════════════════════════════════════════
    print("\n🌐 Test 7: API 路由")
    print("-" * 50)
    from app.main import app
    routes = [r.path for r in app.routes]
    new_routes = [
        "/api/data/web-search",
        "/api/data/read-url",
        "/api/data/industry-search",
        "/api/data/industry-sources",
        "/api/health/data-sources",
    ]
    for r in new_routes:
        status = "✅" if r in routes else "❌"
        print(f"  {status} {r}")
    print(f"  总路由数: {len(routes)}")

    # Cleanup
    await close_web_client()

    # ═══════════════════════════════════════════
    #  Summary
    # ═══════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  总结")
    print("=" * 70)
    print(f"""
  网页搜索 (web_search):
    - DuckDuckGo 搜索引擎 (无需 API Key)
    - LLM 可搜索任何主题的最新信息

  网页读取 (read_webpage):
    - trafilatura 高质量文本提取
    - 可读取任意 URL 的网页内容

  产业链搜索 (search_industry_chain):
    - {len(INDUSTRY_SOURCES)} 个专业产业研究数据源
    - 艾瑞咨询/头豹/前瞻/赛迪/高工锂电/光伏們等
    - 自动匹配查询词到相关数据源
    - 站内搜索 + 通用搜索结合

  LLM 工具调用:
    - {'✅ 已测试' if LLM_API_KEY else '⏭️ 需 API Key'}
    - LLM 可自主决定搜索什么、读什么网页

  API 端点:
    - /api/data/web-search        网页搜索
    - /api/data/read-url          读取网页
    - /api/data/industry-search   产业链搜索
    - /api/data/industry-sources   数据源列表
    - /api/health/data-sources     健康检查
""")


asyncio.run(run_tests())
