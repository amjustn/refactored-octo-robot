"""Bug fix verification tests — Round 2.

Tests bugs found in the second round of analysis:
1. Bug #3: _resolve_company_name substring match is now case-insensitive
2. Bug #4: Single-letter false positives (A股, B股) no longer matched as tickers
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PASS = 0
FAIL = 0
ERRORS = []

def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  ✗ {name} — {detail}")


print("=" * 70)
print("Bug Fix Verification — Round 2")
print("=" * 70)

# ==================== Bug #3: Case-insensitive substring matching ====================
print("\n🐛 [Bug #3] _resolve_company_name substring match is case-insensitive")

from app.tools.market_data import _resolve_company_name

# Chinese names should still work (case doesn't apply)
test(
    "Chinese name '腾讯' resolves",
    _resolve_company_name("腾讯") == "0700.HK",
    f"got: {_resolve_company_name('腾讯')}"
)
test(
    "Chinese name in sentence '分析腾讯的财报' resolves",
    _resolve_company_name("分析腾讯的财报") == "0700.HK",
    f"got: {_resolve_company_name('分析腾讯的财报')}"
)

# English names should now match case-insensitively
test(
    "English name 'apple' (lowercase) in sentence resolves to AAPL",
    _resolve_company_name("分析apple的财报") == "AAPL",
    f"got: {_resolve_company_name('分析apple的财报')}"
)
test(
    "English name 'APPLE' (uppercase) in sentence resolves to AAPL",
    _resolve_company_name("分析APPLE的财报") == "AAPL",
    f"got: {_resolve_company_name('分析APPLE的财报')}"
)
test(
    "English name 'Apple' (mixed case) in sentence resolves to AAPL",
    _resolve_company_name("分析Apple的财报") == "AAPL",
    f"got: {_resolve_company_name('分析Apple的财报')}"
)
test(
    "English name 'nvidia' (lowercase) resolves to NVDA",
    _resolve_company_name("分析nvidia") == "NVDA",
    f"got: {_resolve_company_name('分析nvidia')}"
)
test(
    "English name 'tesla' (lowercase) resolves to TSLA",
    _resolve_company_name("看tesla") == "TSLA",
    f"got: {_resolve_company_name('看tesla')}"
)
test(
    "English name 'meta' (lowercase) resolves to META",
    _resolve_company_name("分析meta") == "META",
    f"got: {_resolve_company_name('分析meta')}"
)

# Longer names should match first (priority test)
test(
    "'腾讯控股' matches before '腾讯' (longer name priority)",
    _resolve_company_name("腾讯控股") == "0700.HK",
    f"got: {_resolve_company_name('腾讯控股')}"
)

# No match returns None
test(
    "Unknown company returns None",
    _resolve_company_name("不存在的公司xyz") is None,
    f"got: {_resolve_company_name('不存在的公司xyz')}"
)


# ==================== Bug #4: Single-letter false positives filtered ====================
print("\n🐛 [Bug #4] Single-letter false positives (A股, B股) no longer matched as tickers")

from app.core.context import build_full_context

# Test: "A股" should not resolve "A" as a stock symbol
async def test_a_share_context():
    ctx = await build_full_context("分析A股银行板块")
    return ctx

try:
    ctx = asyncio.run(test_a_share_context())
    test(
        "Context for 'A股银行' is non-empty",
        bool(ctx) and len(ctx) > 50,
        f"context too short"
    )
    # The context should NOT contain company data for symbol "A"
    test(
        "Context for 'A股银行' does NOT contain Agilent (symbol A) company data",
        "Agilent" not in ctx and "实时行情" not in ctx,
        f"false positive: company data for 'A' was injected"
    )
except Exception as e:
    test("Context for 'A股银行' runs without error", False, str(e))

# Test: "H股" should not resolve "H" as a stock symbol
async def test_h_share_context():
    ctx = await build_full_context("H股房地产板块分析")
    return ctx

try:
    ctx = asyncio.run(test_h_share_context())
    test(
        "Context for 'H股房地产' does NOT contain false positive company data",
        "实时行情" not in ctx,
        f"false positive: company data for 'H' was injected"
    )
except Exception as e:
    test("Context for 'H股房地产' runs without error", False, str(e))

# Test: Normal stock symbol still works
async def test_normal_symbol_context():
    ctx = await build_full_context("分析腾讯")
    return ctx

try:
    ctx = asyncio.run(test_normal_symbol_context())
    test(
        "Context for '腾讯' is non-empty",
        bool(ctx) and len(ctx) > 100,
        f"context too short"
    )
    # Should contain time context at minimum
    test(
        "Context for '腾讯' contains time context",
        "当前时间" in ctx,
        "missing time context"
    )
except Exception as e:
    test("Context for '腾讯' runs without error", False, str(e))


# ==================== Regression: Existing functionality still works ====================
print("\n🔄 [Regression] Existing functionality still works")

# All 18 skills should have knowledge
from app.tools.skill_knowledge import DEFAULT_SKILL_KNOWLEDGE, get_skill_knowledge_for_prompt
from app.skills import list_skills

skills = list_skills()
test(
    f"All {len(skills)} skills have default knowledge",
    all(s["name"] in DEFAULT_SKILL_KNOWLEDGE for s in skills),
    f"missing: {[s['name'] for s in skills if s['name'] not in DEFAULT_SKILL_KNOWLEDGE]}"
)

for skill in skills:
    name = skill["name"]
    prompt = get_skill_knowledge_for_prompt(name)
    test(
        f"Skill '{name}' prompt injection is non-empty",
        bool(prompt) and len(prompt) > 50,
        f"empty prompt for {name}"
    )

# Company name map should have common companies
from app.tools.market_data import COMPANY_NAME_MAP
required_companies = ["腾讯", "茅台", "拼多多", "苹果", "美团", "宁德时代"]
for company in required_companies:
    test(
        f"Company '{company}' is in name map",
        company in COMPANY_NAME_MAP,
        f"missing {company} from map"
    )


# ==================== Summary ====================
print("\n" + "=" * 70)
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print("=" * 70)

if ERRORS:
    print("\n❌ FAILED TESTS:")
    for e in ERRORS:
        print(f"  - {e}")

# pytest 兼容 (2026-08-27): 模块导入(收集)时不退出 — 顶层脚本仅直接运行时生效
if __name__ == "__main__":
    sys.exit(0 if FAIL == 0 else 1)
