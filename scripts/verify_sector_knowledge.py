#!/usr/bin/env python
"""板块知识库验收脚本（2026-09-22）。

覆盖：
  A. 注册表与默认知识完整性（31 板块 × 4 视角，离线）
  B. prompt 格式化（离线）
  C. 板块识别器：别名 / 行业映射 / LLM 兜底 三通道（部分联网+LLM）
  D. 注入门控：白名单注入、非白名单不注入、无板块不注入（联网）
  E. token 预算归类（离线）
  F. API 端点（可选，--base http://127.0.0.1:8001）

用法：
  venv/bin/python scripts/verify_sector_knowledge.py
  venv/bin/python scripts/verify_sector_knowledge.py --base http://127.0.0.1:8001
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.sector_knowledge import (  # noqa: E402
    DEFAULT_SECTOR_KNOWLEDGE, PERSPECTIVE_STANCES, SECTORS,
    _ensure_all_default_sector_knowledge, _load_sector_knowledge,
    get_sector_knowledge_for_prompt, list_sectors,
)
from app.tools.sector_router import _alias_match, _industry_of_symbol, detect_sectors  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


# ── A. 注册表与默认知识 ─────────────────────────────────────────
def test_registry():
    check("A1 板块数=31（申万一级）", len(SECTORS) == 31, f"实际 {len(SECTORS)}")
    no_alias = [k for k, v in SECTORS.items() if not v.get("aliases")]
    check("A2 每板块均有路由别名", not no_alias, f"缺别名: {no_alias}")
    no_default = [k for k in SECTORS if k not in DEFAULT_SECTOR_KNOWLEDGE]
    check("A3 每板块均有默认知识", not no_default, f"缺默认: {no_default}")
    bad = []
    for k, persp in DEFAULT_SECTOR_KNOWLEDGE.items():
        if len(persp) != 4:
            bad.append(f"{k}(视角数{len(persp)})")
            continue
        for p in persp:
            if not (p.get("representative") and p.get("method") and p.get("signals")):
                bad.append(f"{k}(字段空)")
                break
    check("A4 默认知识均 4 视角且字段齐全", not bad, ",".join(bad))
    _ensure_all_default_sector_knowledge()
    d = _load_sector_knowledge("dianzi")
    stances = [p.get("stance") for p in d.get("perspectives", [])]
    check("A5 落盘知识 stance 顺序正确", stances == PERSPECTIVE_STANCES, str(stances))
    check("A6 板块清单含申万一级名", all(s.get("sw_level1") for s in list_sectors()))


# ── B. prompt 格式化 ─────────────────────────────────────────
def test_prompt_format():
    one = get_sector_knowledge_for_prompt(["dianzi"])
    check("B1 单板块格式化含标题与4视角",
          "板块观察知识库" in one and one.count("**") >= 8 and "电子" in one)
    two = get_sector_knowledge_for_prompt(["dianzi", "shipin"])
    check("B2 两板块格式化", "电子" in two and "食品饮料" in two)
    check("B3 去重保序", get_sector_knowledge_for_prompt(["dianzi", "dianzi"]).count("### 板块") == 1)
    check("B4 未知key返回空", get_sector_knowledge_for_prompt(["nonexist"]) == "")
    check("B5 空列表返回空", get_sector_knowledge_for_prompt([]) == "")
    check("B6 含非真实语录声明", "非该人物/机构的真实语录" in one)


# ── C. 识别器三通道 ─────────────────────────────────────────
def test_router_offline():
    cases = [
        ("分析半导体板块的投资机会", "dianzi"),
        ("贵州茅台还值得买吗", "shipin"),
        ("从机器人行业筛出3家最值得配置的公司", "jixie"),
        ("创新药出海主线怎么挖", "yiyao"),
        ("券商板块什么时候启动", "feiyin"),
        ("寻找AI算力产业链中的物理瓶颈环节", "tongxin"),
    ]
    ok = all(_alias_match(t) and _alias_match(t)[0][0] == k for t, k in cases)
    check("C1 别名通道 6 用例", ok,
          "; ".join(f"{t[:10]}→{_alias_match(t)[0][0] if _alias_match(t) else '无'}" for t, k in cases))
    check("C2 单字别名不参与自动匹配", not any(len(a) < 2 for a, _ in
          __import__("app.tools.sector_router", fromlist=["_ALIAS_PAIRS"])._ALIAS_PAIRS))
    check("C3 「综合」不参与自动匹配", not _alias_match("四大师综合分析一家公司"))


async def test_router_online():
    # 行业映射（联网：巨潮公司资料）
    m1 = await asyncio.to_thread(_industry_of_symbol, "600519")
    check("C4 行业映射 600519→shipin", m1 == "shipin", f"实际 {m1 or '空'}")
    m2 = await asyncio.to_thread(_industry_of_symbol, "601318")
    check("C5 行业映射 601318→feiyin", m2 == "feiyin", f"实际 {m2 or '空'}")
    m3 = await asyncio.to_thread(_industry_of_symbol, "300750")
    check("C6 电气机械刻意不映射（交LLM兜底）", m3 == "", f"实际 {m3 or '空'}")
    # LLM 兜底（真实 LLM 调用）
    r = await detect_sectors(arguments="000333", symbol="000333")
    check("C7 LLM兜底 000333(美的)→jiadian", "jiadian" in r["sectors"],
          f"实际 {r['sectors']} ({r['source']})")
    r2 = await detect_sectors(arguments="600519", symbol="600519")
    check("C8 端到端 600519→shipin（industry通道）",
          r2["sectors"] == ["shipin"] and r2["source"] == "industry",
          f"实际 {r2['sectors']} ({r2['source']})")


# ── D. 注入门控（联网） ─────────────────────────────────────────
async def test_injection():
    from app.harness.context import ContextBuilder
    b1 = await ContextBuilder().build("从半导体行业筛出3家最值得配置的公司", skill_name="industry-funnel")
    check("D1 白名单技能+板块目标 → 注入", "板块观察知识库" in b1.context,
          f"sector tokens={b1.token_counts.get('sector')}")
    b2 = await ContextBuilder().build("从半导体行业筛出3家最值得配置的公司", skill_name="daily-briefing")
    check("D2 非白名单技能 → 不注入", "板块观察知识库" not in b2.context)
    b3 = await ContextBuilder().build("帮我审视一下当前持仓的心态问题", skill_name="thesis-tracker")
    check("D3 白名单+无板块目标 → 不注入", "板块观察知识库" not in b3.context)


# ── E. token 预算归类 ─────────────────────────────────────────
def test_budget():
    from app.harness.context import BUDGET, _categorize
    check("E1 sector 预算已登记", BUDGET.get("sector") == 1600, f"实际 {BUDGET.get('sector')}")
    cat = _categorize("板块观察知识库（知名机构/人物方法论蒸馏）", "x")
    check("E2 板块块归类为 sector（不被 master 吞掉）", cat == "sector", f"实际 {cat}")


# ── F. API（可选） ─────────────────────────────────────────
def test_api(base: str):
    import json
    import urllib.request
    from app.core.config import BERKSHIRE_API_TOKEN
    from app.core.jwt_utils import create_token
    tok = create_token(BERKSHIRE_API_TOKEN) if BERKSHIRE_API_TOKEN else ""

    def get(path):
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {tok}"})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

    try:
        st = get("/api/sector-knowledge/status")
        check("F1 API status 31 板块", len(st.get("sectors", {})) == 31)
        ls = get("/api/sector-knowledge/list")
        check("F2 API list 含别名", bool(ls.get("sectors")) and bool(ls["sectors"][0]["aliases"]))
        d = get("/api/sector-knowledge/yinhang")
        check("F3 API 单板块 4 视角", len(d.get("perspectives", [])) == 4)
    except Exception as e:
        check("F0 API 可达性", False, f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="如 http://127.0.0.1:8001 则加测 API")
    ap.add_argument("--offline", action="store_true", help="只跑离线检查（A/B/C部分/E）")
    args = ap.parse_args()

    test_registry()
    test_prompt_format()
    test_router_offline()
    if not args.offline:
        asyncio.run(test_router_online())
        asyncio.run(test_injection())
    test_budget()
    if args.base:
        test_api(args.base)

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n===== {passed}/{total} PASS =====")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
