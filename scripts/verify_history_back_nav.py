#!/usr/bin/env python3
"""历史报告「返回回到原处」改造 — 自底向上静态 + 资产下发验证

用法:
    venv/bin/python scripts/verify_history_back_nav.py
    venv/bin/python scripts/verify_history_back_nav.py --base http://127.0.0.1:8001

覆盖：
  * 前端逻辑：记账函数 / backToHome 分流 / loadReport 捕获时机 / data-name / 高亮
  * 台账一致性：研究路径必须清零 _reportOrigin（否则新报告返回会跑回历史列表）
  * 资产下发：/app 页面引用新版本号；服务端下发的 report-actions.js 含新代码（不是旧缓存）
  * 语法：node --check 全部改动脚本

退出码：有 FAIL → 1。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[str, str, str, str]] = []


def record(cid: str, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((cid, name, "PASS" if ok else "FAIL", detail))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


def http(url: str, timeout: int = 10) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def body_of(src: str, func: str) -> str:
    """粗略截取 function <func>(...) { ... } 的函数体（按大括号配平）。"""
    m = re.search(r"function\s+" + re.escape(func) + r"\s*\(", src)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i < 0:
        return ""
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    return src[i:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    args = ap.parse_args()

    ra = read("static/js/report-actions.js")
    res = read("static/js/research.js")
    html = read("templates/index.html")
    css = read("static/css/style.css")

    # F1 记账/还原函数齐备
    need = ["_captureHistoryView", "_restoreHistoryView", "_highlightHistoryItem"]
    missing = [f for f in need if "function " + f not in ra]
    record("F1", "记账/还原/高亮函数齐备", not missing,
           f"缺失={missing or '无'}；状态变量 _historyView={'var _historyView' in ra} "
           f"_reportOrigin={'var _reportOrigin' in ra}")

    # F2 backToHome 分流：历史来源回列表，其它回主页面
    bh = body_of(ra, "backToHome")
    branch = "_reportOrigin === 'history'" in bh and "_restoreHistoryView(_historyView)" in bh
    fallback = "empty-state').style.display = 'block'" in bh
    record("F2", "backToHome 按来源分流（历史→原处，其它→主页面）", branch and fallback,
           f"历史分支={branch} 主页面分支={fallback}")

    # F3 loadReport 在隐藏列表之前记账（顺序错了就记到 0 滚动位置）
    lr = body_of(ra, "loadReport")
    i_cap = lr.find("_captureHistoryView(filename)")
    i_hide = lr.find("history-area').style.display = 'none'")
    order_ok = i_cap >= 0 and i_hide >= 0 and i_cap < i_hide
    record("F3", "loadReport 在隐藏列表前捕获视图", order_ok,
           f"捕获位置={i_cap} 隐藏位置={i_hide}（捕获必须更早）")

    # F3b 对比入口同样必须先记账再隐藏（同一个坑：隐藏后 scrollTop 被清零）
    cs = body_of(ra, "compareSelected")
    i_cap_c = cs.find("_captureHistoryView('')")
    i_hide_c = cs.find("history-area').style.display = 'none'")
    record("F3b", "compareSelected 先记账再隐藏列表",
           i_cap_c >= 0 and i_hide_c >= 0 and i_cap_c < i_hide_c,
           f"记账位置={i_cap_c} 隐藏位置={i_hide_c}")

    # F4 列表条目带 data-name，高亮才能定位
    rh = body_of(ra, "renderHistory")
    _q, _sq = '"', "'"
    needle = "data-name=" + _q + _sq + " + safeName + " + _sq + _q
    has_anchor = needle in rh
    record("F4", "历史条目带 data-name 锚点", has_anchor,
           "history-item 含 " + needle if has_anchor else "未找到 history-item 的 data-name 锚点")

    # F5 研究路径必须清零来源标记（否则新报告「返回」会误回历史列表）
    zero = res.count("_reportOrigin = ''")
    record("F5", "研究路径清零 _reportOrigin", zero >= 3,
           f"research.js 中清零点={zero}（期望≥3：开始研究/流式出报告/收尾出报告）")

    # F6 高亮样式
    record("F6", "高亮样式 .history-item.last-opened 落 CSS",
           ".history-item.last-opened" in css,
           "含 .history-item.last-opened 规则" if ".history-item.last-opened" in css else "CSS 缺高亮规则")

    # F7 页面引用新版本号：断言「已从历史旧值 bump」（不锁死具体日期，
    # 否则同仓库其他会话改别的脚本时会把这条误判成 FAIL）
    OLD_VERS = {"20260911e", "20260911", "20260917", "20260903", "20260801h"}
    checks = []
    for label, fname in (("style.css", "style.css"),
                         ("report-actions.js", "report-actions.js"),
                         ("research.js", "research.js")):
        m = re.search(re.escape(fname) + r"\?v=([0-9a-z]+)", html)
        v = m.group(1) if m else "-"
        checks.append((label, v, v not in OLD_VERS))
    record("F7", "index.html 缓存版本号已 bump", all(c[2] for c in checks),
           " ".join(f"{c[0]}={c[1]}{'' if c[2] else '(仍是旧值!)'}" for c in checks))

    # F8 node --check 语法
    bad = []
    for f in ["static/js/report-actions.js", "static/js/research.js",
              "scripts/verify_history_back_nav_e2e.js"]:
        r = subprocess.run(["node", "--check", str(ROOT / f)], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f + ": " + r.stderr.strip().splitlines()[-1][:80])
    record("F8", "node --check 语法通过", not bad, f"失败={bad or '无'}")

    # F9 端到端资产下发：服务端返回的页面与 JS 是新版本（不是旧缓存/未重启）
    try:
        st, page = http(args.base + "/app")
        m = re.search(r"report-actions\.js\?v=([0-9a-z]+)", page)
        page_ver = m.group(1) if m else "-"
        st2, js = http(args.base + "/static/js/report-actions.js?v=" + page_ver)
        served_ok = "_restoreHistoryView" in js and "_captureHistoryView" in js
        record("F9", "/app 下发的 report-actions.js 含新代码", st == 200 and served_ok,
               f"页面HTTP={st} 版本={page_ver} JS HTTP={st2} "
               f"含_restoreHistoryView={'_restoreHistoryView' in js}")
    except Exception as e:  # noqa: BLE001
        record("F9", "/app 下发的 report-actions.js 含新代码", False, f"请求失败：{e}")

    width = max(len(r[1]) for r in RESULTS)
    print("=" * 78)
    print("历史报告返回原处 — 自底向上验证")
    print("=" * 78)
    for cid, name, status, detail in RESULTS:
        print(f"[{status}] {cid} {name.ljust(width)} | {detail}")
    fails = [r for r in RESULTS if r[2] == "FAIL"]
    print("-" * 78)
    print(f"合计 {len(RESULTS)} 项，PASS {len(RESULTS) - len(fails)}，FAIL {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
