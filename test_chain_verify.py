#!/usr/bin/env python3
"""Round 3: Full-chain verification script."""
import asyncio
import json
import sys
sys.path.insert(0, '/home/q/ai_berkshire_web')

results = []

def test(name, cond):
    if cond:
        results.append(("PASS", name))
        print(f"  PASS: {name}")
    else:
        results.append(("FAIL", name))
        print(f"  FAIL: {name}")

# Test 1: Chinese name resolution chain
from app.tools.market_data import _resolve_company_name, _detect_market
test("Chinese name exact: 腾讯 -> 0700.HK", _resolve_company_name("腾讯") == "0700.HK")
test("Chinese substring: 分析腾讯的财报 -> 0700.HK", _resolve_company_name("分析腾讯的财报") == "0700.HK")
test("Chinese substring: 茅台 -> 600519.SH", _resolve_company_name("茅台") == "600519.SH")
test("Case-insensitive US: aapl -> US", _detect_market("aapl") == "US")

# Test 2: Knowledge base never empty
from app.tools.knowledge_updater import get_knowledge_summary_for_prompt
summary = get_knowledge_summary_for_prompt()
test("Knowledge summary > 100 chars", len(summary) > 100)
test("Knowledge summary has 巴菲特", "巴菲特" in summary)
test("Knowledge summary has 芒格", "芒格" in summary)

# Test 3: Context building with Chinese name
from app.core.context import build_full_context
ctx = asyncio.run(build_full_context("腾讯"))
test("Context has 当前日期", "当前日期" in ctx)
test("Context not empty", len(ctx) > 50)

# Test 4: Financial tools edge cases
from app.tools.financial_rigor import verify_market_cap, cross_validate
r = verify_market_cap(100, 1e9, 0)
test("verify_market_cap reported=0 fails", not r["passed"])
r = cross_validate("Rev", {"A": 100, "B": 101, "C": 150})
test("cross_validate 3 sources", r["source_count"] == 3)

# Test 5: News placeholder
from app.tools.market_data import fetch_company_news
news = asyncio.run(fetch_company_news("UNKNOWN.SYMBOL", 7))
test("News returns placeholder not empty", len(news) > 0)

# Test 6: Orchestrator tool execution
from app.harness.tools import _execute_tool
result = asyncio.run(_execute_tool("calc", {"expression": "100*2"}))
data = json.loads(result)
test("Tool calc 100*2=200", abs(data["result"] - 200) < 0.01)

# Test 7: Series length limit
from app.harness.runner import MAX_SERIES_REPORT_LENGTH
from app.core.config import MAX_REPORT_LENGTH
test("Series length > normal length", MAX_SERIES_REPORT_LENGTH > MAX_REPORT_LENGTH)

# Test 8: DataCache stats
from app.core.data_cache import DataCache
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as t:
    c = DataCache(Path(t) / "t.db")
    c.set("a", "b", "c")
    s = c.get_stats()
    test("DataCache stats include _total", "_total" in s)

# Test 9: TaskStore
from app.core.task_store import TaskStore
from app.models.schemas import ResearchStatus
with tempfile.TemporaryDirectory() as t:
    store = TaskStore(Path(t) / "t.db")
    store.save_task(ResearchStatus(task_id="x1", skill_name="test", status="running"), "args")
    loaded = store.get_task("x1")
    test("TaskStore save+get", loaded is not None and loaded.task_id == "x1")

# Test 10: App imports
from app.main import app
test("App has 20+ routes", len(app.routes) >= 20)

# Test 11: Default knowledge for all 4 masters
from app.tools.knowledge_updater import DEFAULT_KNOWLEDGE, _load_knowledge
for key in ["buffett", "munger", "duan_yongping", "li_lu"]:
    data = _load_knowledge(key)
    test(f"Default knowledge for {key} has core_philosophy", "core_philosophy" in data)

# Test 12: HK currency display
from app.core.context import build_company_context
hk_price = {"name": "Tencent", "market": "HK", "price": 380.0, "change_pct": 1.5, "currency": "HKD"}
result_str = build_company_context("0700.HK", hk_price)
test("HK stock shows HK$", "HK$" in result_str)

# Summary
passed = sum(1 for r, _ in results if r == "PASS")
failed = sum(1 for r, _ in results if r == "FAIL")
print(f"\n=== FULL-CHAIN VERIFICATION: {passed}/{len(results)} passed, {failed} failed ===")
if failed > 0:
    print("FAILURES:")
    for status, name in results:
        if status == "FAIL":
            print(f"  - {name}")
    sys.exit(1)
else:
    print("ALL TESTS PASSED!")
