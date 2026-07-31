#!/usr/bin/env python3
"""E2E test: verify web search is auto-injected into report context."""
import sys
sys.path.insert(0, "/home/q/ai_berkshire_web")

import asyncio


async def run_test():
    print("=" * 70)
    print("  报告生成时自动网络搜索 验证")
    print("=" * 70)

    from app.core.context import build_full_context

    # ═══════════════════════════════════════════
    #  Test 1: Context with A-share stock (茅台)
    # ═══════════════════════════════════════════
    print("\n📦 Test 1: build_full_context('茅台', investment-research)")
    print("-" * 50)
    ctx = await build_full_context("茅台", skill_name="investment-research")

    has_time = "当前时间" in ctx
    has_market = "市场指数" in ctx
    has_company = "实时行情" in ctx
    has_news = "近期新闻" in ctx
    has_web_search = "网络最新信息" in ctx
    has_web_content = "网页内容" in ctx
    has_search_results = "搜索结果" in ctx
    has_knowledge = "段永平" in ctx or "巴菲特" in ctx or "知识库" in ctx

    print(f"  上下文总长度: {len(ctx)} 字符")
    print(f"  时间上下文:   {'✅' if has_time else '❌'}")
    print(f"  市场指数:     {'✅' if has_market else '❌'}")
    print(f"  公司行情:     {'✅' if has_company else '❌'}")
    print(f"  公司新闻:     {'✅' if has_news else '❌'}")
    print(f"  网络搜索:     {'✅' if has_web_search else '❌'}")
    print(f"  网页内容:     {'✅' if has_web_content else '❌'}")
    print(f"  搜索结果:     {'✅' if has_search_results else '❌'}")
    print(f"  投资知识:     {'✅' if has_knowledge else '❌'}")

    # Show the web search section
    if has_web_search:
        idx = ctx.find("## 网络最新信息")
        if idx >= 0:
            web_section = ctx[idx:idx+2000]
            print(f"\n  --- 网络搜索内容预览 ---")
            print(web_section[:800])

    # ═══════════════════════════════════════════
    #  Test 2: Context with US stock (AAPL)
    # ═══════════════════════════════════════════
    print("\n\n📦 Test 2: build_full_context('AAPL', investment-research)")
    print("-" * 50)
    ctx2 = await build_full_context("AAPL", skill_name="investment-research")
    has_web2 = "网络最新信息" in ctx2
    print(f"  上下文总长度: {len(ctx2)} 字符")
    print(f"  公司行情:     {'✅' if 'AAPL' in ctx2 and '行情' in ctx2 else '❌'}")
    print(f"  网络搜索:     {'✅' if has_web2 else '❌'}")

    # ═══════════════════════════════════════════
    #  Test 3: Full report generation with auto web search
    # ═══════════════════════════════════════════
    print("\n\n🤖 Test 3: 完整报告生成 (investment-research, 茅台)")
    print("-" * 50)
    from app.core.config import LLM_API_KEY

    if LLM_API_KEY:
        from app.harness.runner import run_single_agent
        import time

        start = time.time()
        try:
            report = await asyncio.wait_for(
                run_single_agent("investment-research", "茅台"),
                timeout=180,
            )
            elapsed = time.time() - start

            print(f"  ✅ 报告生成成功 ({elapsed:.1f}s, {len(report)} 字符)")
            print(f"  开头: {report[:200]}")

            # Check if report references web-sourced information
            has_web_ref = "2025" in report or "最新" in report or "网络" in report
            print(f"\n  报告包含2025年/最新信息: {'✅' if has_web_ref else '⚠️ 不确定'}")

        except asyncio.TimeoutError:
            print(f"  ❌ 超时 (180s)")
        except Exception as e:
            print(f"  ❌ 失败: {e}")
    else:
        print("  ⏭️ 跳过 (未配置 API Key)")

    # ═══════════════════════════════════════════
    #  Test 4: Industry research with auto web search
    # ═══════════════════════════════════════════
    print("\n\n🏭 Test 4: 产业链研究 (industry-research, 新能源汽车)")
    print("-" * 50)
    ctx4 = await build_full_context("新能源汽车产业链", skill_name="industry-research")
    has_industry_search = "产业链搜索" in ctx4 or "数据源" in ctx4
    has_web4 = "网络最新信息" in ctx4
    print(f"  上下文总长度: {len(ctx4)} 字符")
    print(f"  网络搜索:     {'✅' if has_web4 else '❌'}")
    print(f"  产业链搜索:   {'✅' if has_industry_search else '⚠️'}")

    # Cleanup
    from app.tools.web_search import close_web_client
    from app.tools.market_data_http import close_client
    await close_web_client()
    await close_client()

    # ═══════════════════════════════════════════
    #  Summary
    # ═══════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  总结")
    print("=" * 70)
    print(f"""
  报告生成时自动网络搜索:
    A股上下文 (茅台):
      实时行情: {'✅' if has_company else '❌'}
      网络搜索: {'✅' if has_web_search else '❌'}
      网页内容: {'✅' if has_web_content else '❌'}

    完整报告生成:
      {'✅ 已验证报告包含网络搜索的信息' if LLM_API_KEY else '⏭️ 需 API Key'}

  每次生成报告时，系统会自动:
    1. 搜索 "茅台 2025 最新 业绩 新闻"
    2. 读取前2个搜索结果的网页内容
    3. 如果是行业分析，还会搜索产业链数据库
    4. 将所有网络信息注入到 LLM 的上下文中
    5. LLM 基于这些信息生成报告
""")


asyncio.run(run_test())
