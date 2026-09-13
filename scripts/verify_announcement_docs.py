#!/usr/bin/env python3
"""第 1 步自检：定期报告正文摘录（巨潮原文 PDF → 董监高/持股/薪酬章节）。

起因（静态审计第 1 条）：原来的 read_webpage 验收只断言"返回内容非空"，
而实测东财公告详情页只有 51 字符通用免责声明、巨潮详情页只有 264 字符
{{stockName}} 占位符 —— 公告正文根本没读到。本步改为走巨潮原文 PDF 抽取。

双重验证：
- 自下而上：离线断言（PDF 链接拼接、章节锚点容错、缓存键、接线）
- 自上而下：联网实测（688008 年报 → 董监高章节；缓存命中；工具经网关可用）

用法（仓库根目录）:
    venv/bin/python scripts/verify_announcement_docs.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILED: list = []

CNINFO_LINK = ("http://www.cninfo.com.cn/new/disclosure/detail?stockCode=688008"
               "&announcementId=1225057736&orgId=9900039002&announcementTime=2026-03-31")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def part_a_offline() -> None:
    from app.tools import announcement_docs as D

    print("\n=== A. 离线断言 ===")
    check("A1 从巨潮链接取 announcementId",
          D.parse_announcement_id(CNINFO_LINK) == "1225057736",
          D.parse_announcement_id(CNINFO_LINK))
    check("A2 从巨潮链接取公告日期",
          D.parse_announcement_date(CNINFO_LINK) == "2026-03-31",
          D.parse_announcement_date(CNINFO_LINK))
    check("A3 拼出静态 PDF 地址",
          D.build_pdf_url(CNINFO_LINK) == "http://static.cninfo.com.cn/finalpage/2026-03-31/1225057736.PDF",
          D.build_pdf_url(CNINFO_LINK))
    check("A4 缺 announcementId → ''",
          D.build_pdf_url("http://www.cninfo.com.cn/new/disclosure/detail?stockCode=688008") == "")
    check("A5 缺日期 → ''",
          D.build_pdf_url("http://www.cninfo.com.cn/x?announcementId=123") == "")
    check("A6 非 .cninfo 链接不抛异常且返回 ''", D.build_pdf_url("https://x.com/a") == "")

    # 章节锚点：PDF 抽文本会按行断字，锚点必须容错空白/换行
    split_text = "前言\n五、董事、监事、高级\n管理人员情况\n姓名 职务 持股\n"
    anchor, ex = D.find_section(split_text)
    check("A7 锚点容错换行（'董事、监事、高级\\n管理人员情况'）",
          anchor == "董事、监事、高级管理人员情况" and "姓名 职务 持股" in ex,
          f"anchor={anchor!r}")

    kcb = "…四、公司治理\n五、董事和高级管理人员的情况\n(一)持股变动及薪酬情况\n杨崇和 董事长\n"
    anchor2, ex2 = D.find_section(kcb)
    check("A8 科创板模板锚点（'董事和高级管理人员的情况'）命中",
          anchor2 == "董事和高级管理人员的情况" and "杨崇和" in ex2, f"anchor={anchor2!r}")

    fallback = "某章节\n现任及报告期内离任董事、高级管理人员和核心技术人员持股变动及薪酬情况\n数据…\n"
    anchor3, ex3 = D.find_section(fallback)
    check("A9 回退锚点（持股变动及薪酬情况整句）命中", bool(anchor3) and bool(ex3), f"anchor={anchor3!r}")

    check("A10 未命中任何锚点 → ('','')",
          D.find_section("这是一份没有相关章节的文档，只有正文段落。") == ("", ""))
    check("A11 空文本不抛异常", D.find_section("") == ("", ""))
    a4, ex4 = D.find_section(kcb * 30, max_chars=120)
    check("A12 尊重 max_chars 上限", len(ex4) <= 120, f"len={len(ex4)}")
    check("A13 摘下标的调用不抛异常（返回 ok=False + note）",
          D.get_management_excerpt("0700.HK").get("ok") is False
          and "仅覆盖A股" in D.get_management_excerpt("0700.HK").get("note", ""))
    check("A14 空标的 → ok=False", D.get_management_excerpt("").get("ok") is False)
    check("A15 无数据时 build_doc_context 返回 ''（不编造）",
          D.build_doc_context("0700.HK") == "")

    src = (ROOT / "app" / "tools" / "announcement_docs.py").read_text(encoding="utf-8")
    check("A16 缓存键不含 max_chars（避免换长度重复下载 3MB PDF）",
          "|mgmt\"" in src and "|mgmt|" not in src)
    check("A17 缓存 TTL 为 30 天", "CACHE_TTL = 30 * 86400" in src)
    check("A18 PDF 大小上限与超时参数存在",
          "MAX_PDF_BYTES" in src and "DEFAULT_TIMEOUT" in src)

    ctx_src = (ROOT / "app" / "core" / "context.py").read_text(encoding="utf-8")
    check("A19 注入白名单里含管理层/财务精读类技能",
          "DOC_EXCERPT_SKILLS" in ctx_src and '"management-deep-dive"' in ctx_src)
    tools_src = (ROOT / "app" / "harness" / "tools.py").read_text(encoding="utf-8")
    check("A20 工具注册齐备（schema + 执行器 + ToolDef）",
          all(k in tools_src for k in
              ('"name": "fetch_announcement_fulltext"',
               '"fetch_announcement_fulltext": _fetch_announcement_fulltext',
               '"fetch_announcement_fulltext": ToolDef')))


async def part_b_live() -> None:
    from app.tools import announcement_docs as D

    print("\n=== B. 联网实测（688008 澜起科技）===")
    t0 = time.time()
    r = D.get_management_excerpt("688008.SH", "澜起科技", max_chars=2500)
    dt = time.time() - t0
    print(f"      ok={r.get('ok')} cached={r.get('cached')} 耗时 {dt:.1f}s | {r.get('title')} | {r.get('date')}")
    check("B1 取到年报并命中董监高章节", bool(r.get("ok")) and bool(r.get("excerpt")), str(r.get("note")))
    check("B2 报告标题为年度报告", "年度报告" in (r.get("title") or ""), r.get("title"))
    check("B3 公告日期为 2026-03-31（年报披露日）", r.get("date") == "2026-03-31", str(r.get("date")))
    check("B4 命中锚点含'董事和高级管理人员的情况'",
          "董事" in (r.get("anchor") or ""), str(r.get("anchor")))
    ex = r.get("excerpt") or ""
    check("B5 摘录含真实人名（杨崇和）", "杨崇和" in ex)
    check("B6 摘录含薪酬/持股字段", ("薪酬" in ex) and ("持股" in ex))
    check("B7 摘录长度不超过请求上限", len(ex) <= 2500, f"len={len(ex)}")

    t1 = time.time()
    r2 = D.get_management_excerpt("688008.SH", "澜起科技", max_chars=800)
    dt2 = time.time() - t1
    check("B8 第二次调用命中缓存且 <0.5s", bool(r2.get("cached")) and dt2 < 0.5, f"cached={r2.get('cached')} {dt2:.2f}s")
    check("B9 缓存命中时按新 max_chars 截断", len(r2.get("excerpt") or "") <= 800)

    ctx = D.build_doc_context("688008.SH", "澜起科技", max_chars=1200)
    check("B10 注入块含来源与 PDF 链接",
          "巨潮资讯" in ctx and "static.cninfo.com.cn" in ctx, ctx[:80])
    check("B11 注入块长度受限", 0 < len(ctx) <= 2000, f"len={len(ctx)}")

    # 经网关调用（与报告链路同一入口）
    from app.harness.tools import get_gateway
    res = await get_gateway().execute(
        "fetch_announcement_fulltext", {"symbol": "688008", "company_name": "澜起科技", "max_chars": 1200}
    )
    d = json.loads(res)
    check("B12 工具经网关可用且返回 _source=cninfo-pdf",
          d.get("_source") == "cninfo-pdf" and d.get("ok") is True, str(d.get("note")))
    check("B13 工具返回章节名与摘录", bool(d.get("section")) and bool(d.get("excerpt")))

    from app.tools.web_search import read_webpage
    em = await read_webpage(
        "https://data.eastmoney.com/notices/detail/688008/AN202609101829231605.html", max_chars=1200)
    check("B14 对比：公告详情页被标注为非正文（quality != ok）",
          em.get("quality") in ("shell", "thin"), f"quality={em.get('quality')} len={len(em.get('content') or '')}")

    # 注入白名单在真实上下文里生效（第1步）：管理层技能拿到年报正文摘录块，
    # 非白名单技能不注入（避免给全部技能加首次下载成本）
    from app.core.context import build_full_context
    ctx_mgmt = await build_full_context(
        arguments="澜起科技你给我一个深度报告", skill_name="management-deep-dive")
    check("B15 管理层技能上下文含年报正文摘录块", "定期报告正文摘录" in ctx_mgmt,
          f"len={len(ctx_mgmt)}")
    check("B16 摘录块带董监高章节锚点与来源",
          "董事和高级管理人员的情况" in ctx_mgmt and "static.cninfo.com.cn" in ctx_mgmt)
    ctx_ind = await build_full_context(
        arguments="澜起科技你给我一个深度报告", skill_name="industry-research")
    check("B17 非白名单技能不注入该块（成本只在需要处付）",
          "定期报告正文摘录" not in ctx_ind, f"len={len(ctx_ind)}")


def main() -> int:
    part_a_offline()
    asyncio.run(part_b_live())
    print("\n" + "=" * 56)
    print(f"PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
