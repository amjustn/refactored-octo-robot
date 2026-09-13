#!/usr/bin/env python3
"""批1（P0）自检：标的解析修复 + 搜索词清洗 + 兜底抓取过门闸。

双重验证：
- 自下而上：离线单元断言（清洗/token 化/相关度/标的解析分层/索引规模）
- 自上而下：联网端到端（真实查询 → 上下文注入 → 历史任务回放 + HTTP 存活）

用法（仓库根目录）：
    venv/bin/python scripts/verify_batch1.py
退出码：有 FAIL 时为 1。
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
SKIP = 0
FAILED_NAMES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"  [FAIL] {name} {detail}")


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name} —— {why}")


# ═══════════════════════════════════════════════════════════
#  A. 自下而上：离线断言
# ═══════════════════════════════════════════════════════════

def part_a_offline() -> None:
    from app.tools.market_data import (
        _resolve_company_name,
        company_display_name,
        resolve_symbol,
    )
    from app.tools.text_relevance import (
        GENERIC_TOKENS,
        clean_search_keyword,
        relevance_ratio,
        relevance_tokens,
    )
    from app.tools import name_index

    print("\n=== A. 离线断言 ===")

    # A1-A6 搜索词清洗
    check("A1 清洗: '澜起科技你给我一个深度报告' -> '澜起科技'",
          clean_search_keyword("澜起科技你给我一个深度报告") == "澜起科技",
          repr(clean_search_keyword("澜起科技你给我一个深度报告")))
    check("A2 清洗: '002155你看一下还能能不能买' -> '002155'",
          clean_search_keyword("002155你看一下还能能不能买") == "002155",
          repr(clean_search_keyword("002155你看一下还能能不能买")))
    check("A3 清洗: '你看一下还能不能买002837' -> '002837'",
          clean_search_keyword("你看一下还能不能买002837") == "002837",
          repr(clean_search_keyword("你看一下还能不能买002837")))
    check("A4 清洗: 纯主题词原样保留 '商业航天'",
          clean_search_keyword("商业航天") == "商业航天",
          repr(clean_search_keyword("商业航天")))
    check("A5 清洗: 只剩祈使短语 -> '' (调用方据此跳过搜索)",
          clean_search_keyword("给我一个深度报告") == "")
    check("A6 清洗: 空输入不抛异常", clean_search_keyword("") == "" and clean_search_keyword(None) == "")

    # A7-A9 相关性 token 化
    toks = relevance_tokens("澜起科技你给我一个深度报告 2026 最新")
    check("A7 长中文串拆 2-gram（不再整句一个 token）",
          all(len(t) <= 4 for t in toks) and "2026" in toks, str(toks))
    check("A8 短中文词整体保留（'半导体设备'）",
          relevance_tokens("半导体设备") == ["半导体设备"], str(relevance_tokens("半导体设备")))
    check("A9 空查询 token 为空", relevance_tokens("") == [])

    # A10-A11 相关度判定：真实结果放行 / 投毒批次拒收
    q = "澜起科技你给我一个深度报告 2026 最新"
    good = [{"title": f"澜起科技2026年半年度营收{i}亿元", "snippet": "澜起科技上半年归母净利润增长"} for i in range(5)]
    poison = [{"title": q, "snippet": "拼 拼音字典词条"} for _ in range(5)]
    check("A10 真实结果批次相关度 >= 0.5（放行）", relevance_ratio(good, q) >= 0.5,
          f"{relevance_ratio(good, q):.2f}")
    check("A11 投毒批次（标题回声整句+无关摘要）< 0.5（拒收）", relevance_ratio(poison, q) < 0.5,
          f"{relevance_ratio(poison, q):.2f}")

    # A12-A21 标的解析分层
    cases = [
        ("A12 代码紧贴中文: '688981分析一下是否可以买入'", "688981分析一下是否可以买入", "688981.SH", "explicit_code"),
        ("A13 代码在句尾: '能不能买300308'", "能不能买300308", "300308.SZ", "explicit_code"),
        ("A14 代码在句尾: '你看一下还能不能买002837'", "你看一下还能不能买002837", "002837.SZ", "explicit_code"),
        ("A15 表外公司名: '澜起科技你给我一个深度报告'", "澜起科技你给我一个深度报告", "688008.SH", "name_index"),
        ("A16 表外公司名: '研究长鑫科技'", "研究长鑫科技", "688825.SH", "name_index"),
        ("A17 显式港股: '0700.HK'", "0700.HK", "0700.HK", "explicit_code"),
        ("A18 显式美股(大写): '分析 AAPL 的估值'", "分析 AAPL 的估值", "AAPL", "explicit_code"),
        ("A19 硬编码表优先: '贵州茅台'", "贵州茅台", "600519.SH", "name_map"),
        ("A20 2026-08-27 修复不回归: '分析平安银行(000001)'", "分析平安银行(000001)", "000001.SZ", "explicit_code"),
    ]
    for name, text, want_sym, want_src in cases:
        got_sym, got_src = resolve_symbol(text)
        check(f"{name} -> {want_sym}", got_sym == want_sym and got_src == want_src,
              f"got={(got_sym, got_src)}")

    sym, src = resolve_symbol("分析 Rambus MRDIMM 市占率")
    check("A21 英文单词不被当 ticker（Rambus/MRDIMM）", sym is None, f"got={(sym, src)}")
    check("A22 主题查询不绑标的（'商业航天'）", resolve_symbol("商业航天") == (None, "none"))
    check("A23 bare ticker 小写也认（'aapl'）", resolve_symbol("aapl")[0] == "AAPL")

    # A24-A27 索引与反查
    st = name_index.stats()
    check("A24 名称索引规模 >= 5000", st["count"] >= 5000, str(st))
    check("A25 索引反查: 澜起科技 -> 688008.SH", name_index.lookup("澜起科技") == "688008.SH")
    check("A26 代码反查名称: 688008.SH -> 澜起科技", company_display_name("688008.SH") == "澜起科技")
    check("A27 索引不做简称匹配（'苹果折叠屏产业链' 不绑 A 股标的）",
          name_index.resolve_by_name("苹果折叠屏产业链") is None)
    check("A28 硬编码表 + 索引兜底串联（_resolve_company_name 兼容旧调用）",
          _resolve_company_name("澜起科技") == "688008.SH")

    # A29-A31 源码级回归（防止门闸被顺手放松）
    ws_src = (ROOT / "app" / "tools" / "web_search.py").read_text(encoding="utf-8")
    check("A29 门闸阈值未被放松（仍为 0.5）", "_MIN_BATCH_RELEVANCE = 0.5" in ws_src)
    check("A30 web_search 走共享 token 化", "_text_relevance.relevance_ratio" in ws_src)
    sc_src = (ROOT / "app" / "tools" / "site_crawler.py").read_text(encoding="utf-8")
    check("A31 兜底抓取不再'命中不足就全量注入'",
          "core_tokens = _tr.relevance_tokens" in sc_src and "全部不相关，不注入上下文" in sc_src)
    ctx_src = (ROOT / "app" / "core" / "context.py").read_text(encoding="utf-8")
    check("A32 context.py 旧 \\b 代码正则已移除",
          r"\b(\d{6}(?:\.SH|\.SZ" not in ctx_src and "resolve_symbol(arguments" in ctx_src)
    check("A33 text_relevance 泛化词表非空且含'报告'", "报告" in GENERIC_TOKENS)


# ═══════════════════════════════════════════════════════════
#  B. 自上而下：联网端到端
# ═══════════════════════════════════════════════════════════

def _noise_count(text: str) -> int:
    return sum(text.count(w) for w in ["iPhone", "快充", "折叠屏", "特斯拉", "小米", "软媒魔方"])


async def part_b_e2e() -> None:
    from app.core.context import build_full_context, build_web_search_context

    print("\n=== B. 联网端到端 ===")

    ctx = await build_web_search_context(arguments="澜起科技你给我一个深度报告",
                                        skill_name="management-deep-dive")
    kw = re.search(r"搜索关键词: (.+)", ctx or "")
    kw_txt = (kw.group(1).strip().rstrip("*") if kw else "")
    check("B1 搜索关键词已清洗（不含'给我'）", kw_txt and "给我" not in kw_txt and "澜起科技" in kw_txt,
          f"keyword={kw_txt!r}")
    check("B2 搜索结果命中目标公司（'澜起' >= 5 次）", (ctx or "").count("澜起") >= 5,
          f"count={ (ctx or '').count('澜起') }")
    check("B3 兜底抓取噪声已剔除（消费电子词 0 次）", _noise_count(ctx or "") == 0,
          f"noise={_noise_count(ctx or '')}")

    full = await build_full_context(arguments="澜起科技你给我一个深度报告",
                                    skill_name="management-deep-dive")
    check("B4 澜起科技上下文含 688008 行情", "688008" in full and "实时行情" in full)
    check("B5 澜起科技上下文含机构研报块", "机构研报" in full)
    check("B6 澜起科技上下文规模 >= 5000 字符（修复前该技能整体仅 3826 tokens）",
          len(full) >= 5000, f"len={len(full)}")
    check("B7 澜起科技上下文不再注入无关爬取块", "行业网站最新文章" not in full)

    full2 = await build_full_context(arguments="002155你看一下还能不能买",
                                     skill_name="investment-research")
    check("B8 002155 上下文绑到湖南黄金（代码紧贴中文场景）",
          "002155" in full2 and "湖南黄金" in full2)

    full3 = await build_full_context(arguments="商业航天", skill_name="industry-research")
    check("B9 主题查询不注入个股行情（不误绑标的）",
          "商业航天" in full3 and "实时行情" not in full3)
    check("B10 主题查询仍保留网络信息块", "网络最新信息" in full3 or "产业链" in full3)

    # 历史任务回放：137 个任务里"该有标的却没解析出来"的从 13 降到 0
    from akshare import stock_info_a_code_name  # noqa: F401  (仅确认依赖存在)
    db = ROOT / "data" / "tasks.db"
    if not db.exists():
        skip("B11 历史任务回放", "data/tasks.db 不存在")
    else:
        con = sqlite3.connect(str(db))
        rows = con.execute("select skill_name, arguments from tasks").fetchall()
        from app.tools.market_data import resolve_symbol as rs
        miss = []
        hit = 0
        for skill, args in rows:
            args = args or ""
            sym, _src = rs(args)
            if sym:
                hit += 1
                continue
            if re.search(r"(?<!\d)\d{6}(?!\d)", args):
                miss.append((skill, args[:40]))
        check("B11 历史任务回放：含代码的任务不再漏解析", len(miss) == 0,
              f"仍漏 {len(miss)} 个: {miss[:5]}")
        check("B12 历史任务回放：解析成功率 >= 55%", hit / max(len(rows), 1) >= 0.55,
              f"{hit}/{len(rows)} = {hit/max(len(rows),1):.0%}")


# ═══════════════════════════════════════════════════════════
#  C. HTTP 存活（服务级回归）
# ═══════════════════════════════════════════════════════════

def part_c_http() -> None:
    import urllib.request

    print("\n=== C. HTTP 服务回归 ===")
    try:
        with urllib.request.urlopen("http://localhost:8001/app", timeout=10) as r:
            body = r.read(2000)
        check("C1 /app 返回 200", r.status == 200, f"status={r.status}")
        check("C2 /app 返回 HTML", b"<html" in body.lower() or b"<!doctype" in body.lower())
    except Exception as e:
        check("C1 /app 返回 200", False, f"{type(e).__name__}: {e}")


def main() -> int:
    t0 = time.time()
    part_a_offline()
    asyncio.run(part_b_e2e())
    part_c_http()
    print("\n" + "=" * 56)
    print(f"PASS {PASS} / FAIL {FAIL} / SKIP {SKIP}   耗时 {time.time()-t0:.1f}s")
    if FAILED_NAMES:
        print("失败项：" + "; ".join(FAILED_NAMES))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
