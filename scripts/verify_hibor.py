#!/usr/bin/env python3
"""慧博投研数据源自检脚本 — 离线断言 + 联网实测 + 详情页可得性。

运行（必须用仓库 venv）：
    cd /home/q/ai_berkshire_web && venv/bin/python scripts/verify_hibor.py

结构：
  A. 离线断言（≥12 条，不联网）：频道映射、日期转换（含闰年/边界）、机构名拆分、
     去重逻辑、空输入不抛异常、format 无数据返回 ''、长度上限、Wind 判定。
  B. 联网实测：港美研究 / 公司调研 两频道真实条数 + 前 5 条样例；
     断言 港美研究 ≥50、公司调研 ≥50，url 均为 https 且指向 /data/。
     并在 港美研究 里找 腾讯 / 苹果 / 英伟达（找到就打印，找不到如实说找不到）。
  C. 详情页实测：任取一条 url 调 get_report_detail，打印 available 真值与实测证据。

结尾输出 'PASS n / FAIL m'，有 FAIL 则 exit(1)。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tools import hibor_reports as H  # noqa: E402
from app.tools.hibor_reports import (  # noqa: E402
    HIBOR_CHANNELS,
    _convert_date,
    _date_from_title,
    _dedupe_reports,
    _extract_ncid,
    _parse_list_page,
    _split_institution,
    fetch_hibor_for_symbol,
    fetch_hibor_reports,
    format_hibor_context,
    get_report_detail,
    wind_available,
    wind_reports,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))
    return bool(cond)


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


REQUIRED_CHANNELS = {
    "精选研报",
    "公司调研",
    "行业分析",
    "宏观研究",
    "港美研究",
    "投资策略",
    "新股研究",
    "晨会早刊",
}


# ═══════════════════════════════════════════════════════
#  A. 离线断言
# ═══════════════════════════════════════════════════════
section("A. 离线断言（不联网）")
print(f"  repo root = {ROOT}")
print(f"  python    = {sys.version.split()[0]}")

# A1 频道映射完整性
missing = REQUIRED_CHANNELS - set(HIBOR_CHANNELS)
check("A1 频道映射包含全部必需频道", not missing, f"missing={sorted(missing)}")

# A2 路径格式
bad_paths = {k: v for k, v in HIBOR_CHANNELS.items() if not (v.startswith("/") and v.endswith(".html"))}
check("A2 所有频道路径以 '/' 开头且以 '.html' 结尾", not bad_paths, f"bad={bad_paths}")

# A3 路径唯一
dups = len(HIBOR_CHANNELS) != len(set(HIBOR_CHANNELS.values()))
check("A3 频道路径无重复", not dups)

# A4-A10 日期转换（含闰年/边界）
check("A4 日期 '260913' -> '2026-09-13'", _convert_date("260913") == "2026-09-13",
      f"got={_convert_date('260913')!r}")
check("A5 闰年 '240229' -> '2024-02-29'（2024 闰年）", _convert_date("240229") == "2024-02-29",
      f"got={_convert_date('240229')!r}")
check("A6 非闰年 '260229' -> ''（2026 非闰年，非法日期）", _convert_date("260229") == "",
      f"got={_convert_date('260229')!r}")
check("A7 边界 '991231' -> '1999-12-31'（yy>=70 视为 19xx）", _convert_date("991231") == "1999-12-31",
      f"got={_convert_date('991231')!r}")
check("A8 边界 '000101' -> '2000-01-01'", _convert_date("000101") == "2000-01-01",
      f"got={_convert_date('000101')!r}")
check("A9 越界 '261340' -> ''（月份非法）", _convert_date("261340") == "",
      f"got={_convert_date('261340')!r}")
check("A10 空串 '' -> ''", _convert_date("") == "", f"got={_convert_date('')!r}")

# A11-A12 标题取日期
t_full = "国信证券-港股市场速览：医药与互联网带动市场显著下行-260913"
check("A11 标题尾部日期解析 '…-260913' -> '2026-09-13'",
      _date_from_title(t_full) == "2026-09-13", f"got={_date_from_title(t_full)!r}")
check("A12 无日期标题 -> ''", _date_from_title("无日期的标题") == "",
      f"got={_date_from_title('无日期的标题')!r}")

# A13-A15 机构名拆分
check("A13 机构名前缀拆分 '国信证券-…' -> '国信证券'",
      _split_institution(t_full) == "国信证券", f"got={_split_institution(t_full)!r}")
check("A14 无 '-' 标题 -> ''", _split_institution("没有连字符的标题") == "",
      f"got={_split_institution('没有连字符的标题')!r}")
check("A15 纯数字前缀（日期式）拒绝 -> ''", _split_institution("2026-09-13 晨报") == "",
      f"got={_split_institution('2026-09-13 晨报')!r}")

# A16 去重（按 url）
dup_input = [
    {"url": "https://www.hibor.com.cn/data/a.html", "title": "T1"},
    {"url": "https://www.hibor.com.cn/data/a.html", "title": "T1-dup"},
    {"url": "https://www.hibor.com.cn/data/b.html", "title": "T2"},
]
dedup = _dedupe_reports(dup_input)
check("A16 按 url 去重（3 条 -> 2 条，保留首个）",
      len(dedup) == 2 and dedup[0]["title"] == "T1" and dedup[1]["title"] == "T2",
      f"len={len(dedup)}")

# A17 列表页解析：同一 URL 的「标题链接 + [详细] 摘要链接」应合并且保留含日期的标题
sample_html = """
<html><body><table>
<tr><td><span class="tab_lta">
  <a href="/data/aaa.html" target="_blank">国信证券-港股市场速览：显著下行-260913</a>
</span></td></tr>
<tr><td class="tab_tdcolor">　　核心观点 股价表现：医药消费科技均显著回撤……
  <a href="/data/aaa.html" target="_blank">[详细]</a></td></tr>
</table></body></html>
"""
parsed = _parse_list_page(sample_html)
check("A17 列表页同 URL 去重且保留含日期标题",
      len(parsed) == 1 and parsed[0]["url"].endswith("/data/aaa.html")
      and parsed[0]["title"].endswith("-260913"),
      f"parsed={parsed}")

# A18 ncid 抽取（详情页 AJAX 依赖）
check("A18 详情页 ncid 抽取",
      _extract_ncid("var newContentUrl = '/x';  var ncid = 'abc-123';") == "abc-123")

# A19 未知频道返回 []（不抛异常、不发网络请求）
try:
    r_unknown = fetch_hibor_reports(channel="不存在的频道")
    check("A19 未知频道返回 [] 且不抛异常", r_unknown == [], f"got={r_unknown!r}")
except Exception as e:  # noqa: BLE001
    check("A19 未知频道返回 [] 且不抛异常", False, f"raised {type(e).__name__}: {e}")

# A20 空输入不抛异常
try:
    r_sym_empty = fetch_hibor_for_symbol(symbol="", company_name="")
    r_ctx_empty = format_hibor_context()
    check("A20 空输入返回空（symbol/company 空 -> []；format 空 -> ''）",
          r_sym_empty == [] and r_ctx_empty == "",
          f"sym={r_sym_empty!r} ctx={r_ctx_empty!r}")
except Exception as e:  # noqa: BLE001
    check("A20 空输入返回空（symbol/company 空 -> []；format 空 -> ''）", False,
          f"raised {type(e).__name__}: {e}")

# A21 format 长度上限（用假数据离线验证截断）
_orig_sym = H.fetch_hibor_for_symbol
try:
    fake = [
        {
            "title": f"某证券-某某行业深度研究报告：这是第{i}篇非常长的标题用于测试长度上限-260913",
            "institution": "某证券",
            "date": "2026-09-13",
            "url": f"https://www.hibor.com.cn/data/{i:032d}.html",
            "channel": "公司调研",
        }
        for i in range(60)
    ]
    H.fetch_hibor_for_symbol = lambda *a, **k: fake  # type: ignore[assignment]
    ctx_long = format_hibor_context(symbol="600519", limit=60)
    check("A21 format 输出总长 ≤3000 字符",
          bool(ctx_long) and len(ctx_long) <= 3000,
          f"len={len(ctx_long)}")
finally:
    H.fetch_hibor_for_symbol = _orig_sym  # type: ignore[assignment]

# A22 format 结构（首行标题 / 分隔符 / 来源+抓取时间）
try:
    H.fetch_hibor_for_symbol = lambda *a, **k: fake  # type: ignore[assignment]
    ctx_struct = format_hibor_context(symbol="600519", limit=3)
    ok_struct = (
        ctx_struct.splitlines()[0] == "## 机构研报（慧博投研）"
        and all("|" in ln for ln in ctx_struct.splitlines()[1:4])
        and "来源" in ctx_struct and "抓取时间" in ctx_struct
    )
    check("A22 format 首行标题 + '机构名 | 标题 | 日期 | 链接' + 来源/时间",
          ok_struct, f"first={ctx_struct.splitlines()[0]!r}")
finally:
    H.fetch_hibor_for_symbol = _orig_sym  # type: ignore[assignment]

# A23 Wind 可用性判定
try:
    w_ok, w_reason = wind_available()
    check("A23 wind_available() 返回 False 且 reason 非空",
          w_ok is False and isinstance(w_reason, str) and len(w_reason.strip()) > 0,
          f"-> ({w_ok}, {w_reason[:70]}...)")
except Exception as e:  # noqa: BLE001
    check("A23 wind_available() 返回 False 且 reason 非空", False,
          f"raised {type(e).__name__}: {e}")

# A24 Wind 不可用时不返回数据
try:
    wr = wind_reports("600519")
    check("A24 wind_reports() 不可用时 available=False 且 reports 为空、reason 非空",
          wr.get("available") is False and wr.get("reports") == [] and bool(wr.get("reason")),
          f"keys={sorted(wr)}")
except Exception as e:  # noqa: BLE001
    check("A24 wind_reports() 不可用时 available=False 且 reports 为空、reason 非空", False,
          f"raised {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════
#  B. 联网实测
# ═══════════════════════════════════════════════════════
section("B. 联网实测：港美研究 / 公司调研")

gm = fetch_hibor_reports(channel="港美研究", limit=300)
cm = fetch_hibor_reports(channel="公司调研", limit=300)

print(f"\n  ▌港美研究（/microns_9.html）实测条数 = {len(gm)}")
for i, r in enumerate(gm[:5], 1):
    print(f"    {i}. [{r['date']}] {r['institution']} | {r['title'][:56]} | {r['url']}")

print(f"\n  ▌公司调研（/microns_1.html）实测条数 = {len(cm)}")
for i, r in enumerate(cm[:5], 1):
    print(f"    {i}. [{r['date']}] {r['institution']} | {r['title'][:56]} | {r['url']}")

check("B1 港美研究 条数 ≥50", len(gm) >= 50, f"got={len(gm)}")
check("B2 公司调研 条数 ≥50", len(cm) >= 50, f"got={len(cm)}")


def _urls_ok(rows: list) -> bool:
    return bool(rows) and all(
        r.get("url", "").startswith("https://www.hibor.com.cn/data/")
        and r.get("url", "").endswith(".html")
        for r in rows
    )


check("B3 港美研究 url 均为 https 且指向 /data/*.html", _urls_ok(gm))
check("B4 公司调研 url 均为 https 且指向 /data/*.html", _urls_ok(cm))
check("B5 港美研究 条目字段齐全（title/institution/date/url/channel）",
      all(all(k in r for k in ("title", "institution", "date", "url", "channel")) for r in gm))

# 香港/美股关键词覆盖
print("\n  ▌港美研究 关键词覆盖检查：")
kw_found_any = False
for kw in ("腾讯", "苹果", "英伟达"):
    hits = [r for r in gm if kw in r["title"]]
    if hits:
        kw_found_any = True
        print(f"    '{kw}': 找到 {len(hits)} 条")
        for r in hits[:3]:
            print(f"        - [{r['date']}] {r['title'][:70]}")
    else:
        print(f"    '{kw}': 找不到（当前港美研究频道列表页快照内无此关键词）")
check("B6 港美研究 至少命中一个港美股关键词（腾讯/苹果/英伟达）", kw_found_any)

# 跨频道按代码/公司名检索
sym_hits = fetch_hibor_for_symbol(symbol="300498", limit=5)
print(f"\n  ▌fetch_hibor_for_symbol(symbol='300498') -> {len(sym_hits)} 条")
for r in sym_hits[:3]:
    print(f"    - [{r['date']}] {r['title'][:70]}")
check("B7 fetch_hibor_for_symbol(300498) 命中（温氏股份）",
      any("300498" in r["title"] for r in sym_hits), f"n={len(sym_hits)}")


# ═══════════════════════════════════════════════════════
#  C. 详情页可得性实测
# ═══════════════════════════════════════════════════════
section("C. 详情页可得性实测（get_report_detail）")

if not gm:
    check("C1 详情页实测（需要至少一条港美研究条目）", False, "港美研究列表为空")
else:
    test_url = gm[0]["url"]
    print(f"  测试 url = {test_url}")
    det = get_report_detail(test_url)
    print(f"  title       = {det.get('title')!r}")
    print(f"  date        = {det.get('date')!r}")
    print(f"  institution = {det.get('institution')!r}")
    print(f"  pages       = {det.get('pages')!r}")
    print(f"  available   = {det.get('available')!r}")
    print(f"  content 长度 = {len(det.get('content') or '')}")
    print(f"  blocked_by_login = {det.get('blocked_by_login')!r}")
    print(f"  note        = {det.get('note')}")
    body = (det.get("content") or "")
    if body:
        print(f"  content 前 260 字：\n    {body[:260].replace(chr(10), ' / ')}")

    check("C1 get_report_detail 返回必需字段",
          all(k in det for k in ("url", "title", "content", "available", "note")))
    check("C2 返回的 url 与请求一致", det.get("url") == test_url)
    check("C3 note 非空且写明实测证据/原因", bool((det.get("note") or "").strip()))
    check("C4 available 语义自洽（False 时 note 说明阻塞原因）",
          det.get("available") is True or ("未取到" in (det.get("note") or "")
                                           or "摘要" in (det.get("note") or "")
                                           or "登录" in (det.get("note") or "")
                                           or "HTTP" in (det.get("note") or "")))
    if det.get("available"):
        print("\n  ➜ 结论：匿名可拿到正文")
    else:
        print("\n  ➜ 结论：未拿到完整正文（匿名仅摘要预览 / 被登录墙拦截），note 已记录实测证据")
    print(f"  实测证据词命中：{det.get('note')}")


# ═══════════════════════════════════════════════════════
#  汇总
# ═══════════════════════════════════════════════════════
section("汇总")
print(f"  PASS {PASS} / FAIL {FAIL}")
if FAIL:
    print("  ❌ 存在失败断言")
    sys.exit(1)
print("  ✅ 全部通过")
sys.exit(0)
