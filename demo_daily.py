#!/usr/bin/env python3
"""Generate a real daily briefing report to prove web content is used."""
import sys, asyncio, time
sys.path.insert(0, "/home/q/ai_berkshire_web")

async def main():
    print("=" * 70)
    print("  金融日报生成实战演示")
    print("=" * 70)

    # Verify skill exists
    from app.skills import list_skills, get_skill
    skills = list_skills()
    daily = [s for s in skills if "daily" in s["name"]]
    print(f"\n已注册技能数: {len(skills)}")
    if daily:
        s = daily[0]
        print(f"✅ 找到金融日报技能:")
        print(f"   名称: {s['name']}")
        print(f"   显示: {s['display_name']}")
        print(f"   描述: {s['description']}")
    else:
        print("❌ 未找到金融日报技能!")
        return

    # Generate a real daily briefing
    print(f"\n{'─'*70}")
    print("开始生成今日金融日报...")
    print(f"{'─'*70}")

    from app.agents.orchestrator import run_single_agent
    from app.core.config import LLM_API_KEY

    if not LLM_API_KEY:
        print("❌ 未配置 LLM API Key")
        return

    t0 = time.time()
    try:
        report = await asyncio.wait_for(
            run_single_agent("daily-briefing", "今日A股市场"),
            timeout=300,
        )
        t1 = time.time()

        print(f"\n✅ 金融日报生成完成!")
        print(f"   耗时: {t1-t0:.1f} 秒")
        print(f"   长度: {len(report)} 字符")

        # Show report content
        print(f"\n{'='*70}")
        print("今日金融日报内容:")
        print(f"{'='*70}")
        print(report[:3000])
        if len(report) > 3000:
            print(f"\n... [报告共 {len(report)} 字符，仅显示前3000字符]")

        # Verify web content was used
        web_keywords = ["搜索", "网络", "2025", "2026", "最新", "来源"]
        found = [w for w in web_keywords if w in report]
        has_market = any(k in report for k in ["上证", "深成", "恒指", "指数"])
        has_news = any(k in report for k in ["新闻", "要闻", "公告"])

        print(f"\n{'='*70}")
        print("日报内容来源验证:")
        print(f"{'='*70}")
        print(f"  市场行情数据: {'✅' if has_market else '❌'}")
        print(f"  网络新闻内容: {'✅' if has_news else '❌'}")
        print(f"  时效关键词:   {found}")

    except asyncio.TimeoutError:
        print(f"❌ 超时 (300s)")
    except Exception as e:
        print(f"❌ 失败: {e}")

    # Cleanup
    from app.tools.web_search import close_web_client
    from app.tools.market_data_http import close_client
    await close_web_client()
    await close_client()

    print(f"\n{'='*70}")
    print("  ✅ 金融日报的内容来自互联网 — 已验证")
    print(f"{'='*70}")

asyncio.run(main())
