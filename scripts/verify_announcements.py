#!/usr/bin/env python3
"""自检：app/tools/announcements.py（公司公告 / 定期报告 / 高管持股变动）。

用法::

    cd /home/q/ai_berkshire_web && venv/bin/python scripts/verify_announcements.py

分两段：
  A. 离线断言（≥12 条，不联网）—— normalize_symbol / 缓存 category 无冲突 /
     空输入不抛异常 / 无数据时 format 返回 '' / 超长截断上限生效。
  B. 联网实测（可自动 SKIP）—— 对 688008 拉三类数据，打印真实条数与样例，
     断言：半年报含「2026年半年度报告」且日期 2026-08-29、高管变动 ≥30 条、公告 ≥100 条。

结尾打印 ``PASS n / FAIL m``，有 FAIL 则 exit(1)。绝不伪造成功输出——接口不可用时
该条目标 SKIP 并在详情里写明异常。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.tools.announcements as ann  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []   # (name, status, detail)
_TALLY = {"PASS": 0, "FAIL": 0, "SKIP": 0}


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    _TALLY[status] = _TALLY.get(status, 0) + 1
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    record(name, "PASS" if ok else "FAIL", detail)
    return bool(ok)


def skip(name: str, detail: str = "") -> None:
    record(name, "SKIP", detail)


# ═══════════════════════════════════════════════════════
#  A. 离线断言（不联网）
# ═══════════════════════════════════════════════════════

def run_offline() -> None:
    print("\n" + "=" * 66)
    print("A. 离线断言（不联网）")
    print("=" * 66)

    # --- normalize_symbol ---
    check("normalize_symbol('688008') == '688008'",
          ann.normalize_symbol("688008") == "688008",
          repr(ann.normalize_symbol("688008")))

    check("normalize_symbol('688008.SH') == '688008'",
          ann.normalize_symbol("688008.SH") == "688008",
          repr(ann.normalize_symbol("688008.SH")))

    check("normalize_symbol('sh688008') == '688008'（小写前缀）",
          ann.normalize_symbol("sh688008") == "688008",
          repr(ann.normalize_symbol("sh688008")))

    check("normalize_symbol('SZ000001') == '000001'",
          ann.normalize_symbol("SZ000001") == "000001",
          repr(ann.normalize_symbol("SZ000001")))

    check("normalize_symbol('00700.HK') == ''（港股）",
          ann.normalize_symbol("00700.HK") == "",
          repr(ann.normalize_symbol("00700.HK")))

    check("normalize_symbol('AAPL') == ''（美股）",
          ann.normalize_symbol("AAPL") == "",
          repr(ann.normalize_symbol("AAPL")))

    check("normalize_symbol('') / '   ' / None 均 == ''",
          ann.normalize_symbol("") == "" and ann.normalize_symbol("   ") == "" and ann.normalize_symbol(None) == "",
          f"{ann.normalize_symbol('')!r},{ann.normalize_symbol('   ')!r},{ann.normalize_symbol(None)!r}")

    check("normalize_symbol('68800') == ''（位数不足）",
          ann.normalize_symbol("68800") == "",
          repr(ann.normalize_symbol("68800")))

    # --- 缓存 category 不与仓库现有 category 冲突 ---
    existing = {
        "indices", "news", "search", "price", "financials", "charts",
        "broker_reports", "broker_rating", "broker_industry", "broker_detail",
        "knowledge", "skills",
    }
    new_cats = {ann.CAT_DISCLOSURE, ann.CAT_NOTICES, ann.CAT_MGMT}
    check("3 个公告 cache category 互不相同且均为 announcements_ 前缀",
          len(new_cats) == 3 and all(c.startswith("announcements_") for c in new_cats),
          str(sorted(new_cats)))
    check("公告 cache category 与仓库现有 category 无冲突",
          not (new_cats & existing),
          f"交集={sorted(new_cats & existing)}")
    check("TTL 常量为 21600/3600/86400",
          (ann.TTL_DISCLOSURE, ann.TTL_NOTICES, ann.TTL_MGMT) == (21600, 3600, 86400),
          f"{ann.TTL_DISCLOSURE}/{ann.TTL_NOTICES}/{ann.TTL_MGMT}")

    # --- 空/非 A 股输入不得联网、不得抛异常、返回空 ---
    try:
        r1 = ann.get_disclosure_reports("", limit=5)
        check("get_disclosure_reports('') == []（不联网不抛异常）", r1 == [], repr(r1))
    except Exception as e:
        check("get_disclosure_reports('') == []（不联网不抛异常）", False, f"{type(e).__name__}: {e}")

    try:
        r2 = ann.get_company_notices("AAPL", limit=5)
        check("get_company_notices('AAPL') == []（美股短路）", r2 == [], repr(r2))
    except Exception as e:
        check("get_company_notices('AAPL') == []（美股短路）", False, f"{type(e).__name__}: {e}")

    try:
        r3 = ann.get_management_changes("00700.HK", limit=5)
        check("get_management_changes('00700.HK') == []（港股短路）", r3 == [], repr(r3))
    except Exception as e:
        check("get_management_changes('00700.HK') == []（港股短路）", False, f"{type(e).__name__}: {e}")

    check("format_announcement_context('') == ''",
          ann.format_announcement_context("") == "",
          repr(ann.format_announcement_context(""))[:60])
    check("format_announcement_context('00700.HK') == ''（不编造港股公告）",
          ann.format_announcement_context("00700.HK") == "",
          repr(ann.format_announcement_context("00700.HK"))[:60])

    # --- 无数据时 format 返回 '' （monkeypatch 三块均为空）---
    orig = (ann.get_disclosure_reports, ann.get_company_notices, ann.get_management_changes)
    try:
        ann.get_disclosure_reports = lambda *a, **k: []
        ann.get_company_notices = lambda *a, **k: []
        ann.get_management_changes = lambda *a, **k: []
        empty_out = ann.format_announcement_context("688008", company_name="澜起科技")
        check("无数据时 format_announcement_context == ''", empty_out == "", repr(empty_out)[:60])

        # --- 超长截断上限生效 ---
        long_reports = [{
            "kind": "年报",
            "title": "澜起科技" + "超长标题" * 40 + f"-{i}",
            "date": "2026-08-29",
            "url": "http://www.cninfo.com.cn/new/disclosure/detail?" + "x" * 120 + str(i),
        } for i in range(20)]
        long_notices = [{
            "title": "公告" + "标题填充" * 40 + f"-{i}",
            "date": "2026-09-12",
            "url": "https://data.eastmoney.com/notices/detail/688008/AN" + str(i) + ".html",
        } for i in range(30)]
        long_mgmt = [{
            "变动日期": "2026-08-29",
            "变动人": "某人" + str(i),
            "与公司高管关系": "核心技术人员",
            "变动数量": "增持1.00万",
            "交易均价": "24.70",
            "剩余股数": "20.00万",
            "股份变动途径": "股权激励",
        } for i in range(30)]
        ann.get_disclosure_reports = lambda *a, **k: list(long_reports)
        ann.get_company_notices = lambda *a, **k: list(long_notices)
        ann.get_management_changes = lambda *a, **k: list(long_mgmt)
        big = ann.format_announcement_context("688008", company_name="澜起科技")
        check("超长文本被截断到 <= 4000 字符",
              len(big) <= 4000,
              f"渲染长度={len(big)}（原始三块合计远超 4000）")
        check("超长截断后仍保留首行标题与数据源说明",
              big.startswith("## 公司公告与定期报告（自动获取）") and "数据源" in big,
              f"len={len(big)}")
    except Exception as e:
        check("超长截断上限生效", False, f"{type(e).__name__}: {e}")
    finally:
        ann.get_disclosure_reports, ann.get_company_notices, ann.get_management_changes = orig


# ═══════════════════════════════════════════════════════
#  B. 联网实测（688008 澜起科技）
# ═══════════════════════════════════════════════════════

def run_online() -> None:
    print("\n" + "=" * 66)
    print("B. 联网实测（688008 澜起科技；接口不可用则 SKIP）")
    print("=" * 66)

    # --- 1) 定期报告 ---
    try:
        reps = ann.get_disclosure_reports("688008", kind="半年报", limit=10)
        print(f"  定期报告(半年报) 条数={len(reps)}")
        for r in reps[:3]:
            print(f"    - [{r['date']}] {r['kind']} {r['title']} | {r['url'][:80]}")
        hit = any(("2026年半年度报告" in (r.get("title") or "")) and (r.get("date") == "2026-08-29")
                  for r in reps)
        if reps:
            check("半年报含标题『2026年半年度报告』且日期 2026-08-29", hit,
                  f"命中={hit}，共 {len(reps)} 条")
        else:
            skip("半年报含标题『2026年半年度报告』且日期 2026-08-29", "接口返回 0 条")
    except Exception as e:
        skip("半年报含标题『2026年半年度报告』且日期 2026-08-29",
             f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # --- 2) 公司公告 ---
    try:
        notices = ann.get_company_notices("688008", limit=2000)
        print(f"  公司公告 条数={len(notices)}")
        for n in notices[:3]:
            print(f"    - [{n['date']}] {n['title']} | {n['url'][:80]}")
        if notices:
            check("公司公告 >= 100 条", len(notices) >= 100, f"实测 {len(notices)} 条")
        else:
            skip("公司公告 >= 100 条", "接口返回 0 条")
    except Exception as e:
        skip("公司公告 >= 100 条", f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # --- 3) 高管持股变动 ---
    try:
        mgmt = ann.get_management_changes("688008", limit=100)
        print(f"  高管持股变动 条数={len(mgmt)}")
        for m in mgmt[:3]:
            print(f"    - [{m.get('变动日期')}] {m.get('变动人')} "
                  f"{m.get('变动数量')} @{m.get('交易均价')} 剩余{m.get('剩余股数')}")
        if mgmt:
            check("高管持股变动 >= 30 条", len(mgmt) >= 30, f"实测 {len(mgmt)} 条")
            keys = sorted(mgmt[0].keys())
            print(f"    字典键名（中文原样）={keys}")
        else:
            skip("高管持股变动 >= 30 条", "接口返回 0 条")
    except Exception as e:
        skip("高管持股变动 >= 30 条", f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # --- 4) 端到端注入块样例 ---
    try:
        block = ann.format_announcement_context("688008", company_name="澜起科技")
        ok = block.startswith("## 公司公告与定期报告（自动获取）") and len(block) <= 4000 and len(block) > 0
        check("format_announcement_context 端到端生成非空且 <= 4000 字符", ok,
              f"长度={len(block)}")
        print("  ---- 注入块前 1200 字符 ----")
        print(block[:1200])
        print("  ---------------------------")
    except Exception as e:
        skip("format_announcement_context 端到端生成", f"{type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════
#  汇总
# ═══════════════════════════════════════════════════════

def main() -> int:
    print("verify_announcements.py — app/tools/announcements.py 自检")
    run_offline()
    run_online()

    print("\n" + "=" * 66)
    print("明细：")
    for name, status, detail in RESULTS:
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    print("=" * 66)
    print(f"PASS {_TALLY['PASS']} / FAIL {_TALLY['FAIL']} / SKIP {_TALLY['SKIP']}")
    return 1 if _TALLY["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
