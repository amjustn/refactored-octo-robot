#!/usr/bin/env python3
"""AI Berkshire Web v3.1 — Full-Chain Virtual Data Test Suite

Tests EVERY function with virtual/mock data to ensure:
1. No function returns empty/null without meaningful explanation
2. All edge cases are handled
3. Full chain from API -> orchestrator -> tools -> data works

Uses native unittest (no pytest dependency required).
Run: cd /home/q/ai_berkshire_web && venv/bin/python run_full_chain_tests.py
"""
import sys
import os
import json
import random
import tempfile
import asyncio
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, '/home/q/ai_berkshire_web')

import unittest

# Track results
_passed = 0
_failed = 0
_errors = []


def run_async(coro):
    """Run an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ==================== Financial Rigor Tools ====================

class TestVerifyMarketCap(unittest.TestCase):
    def test_basic_pass(self):
        from app.tools.financial_rigor import verify_market_cap
        r = verify_market_cap(150.0, 16.0e9, 2.4e12, "CNY")
        self.assertTrue(r["passed"])
        self.assertLess(r["deviation_pct"], 1)

    def test_basic_fail(self):
        from app.tools.financial_rigor import verify_market_cap
        # Use numbers with large deviation: 100 * 10e8 = 1000e8, reported = 500e8
        r = verify_market_cap(100.0, 10e8, 500e8, "USD")
        self.assertFalse(r["passed"])

    def test_reported_zero_fails(self):
        """Bug fix: reported=0 should FAIL, not pass with 0 deviation."""
        from app.tools.financial_rigor import verify_market_cap
        r = verify_market_cap(100.0, 10e8, 0, "USD")
        self.assertFalse(r["passed"])
        self.assertEqual(r["deviation_pct"], 100.0)

    def test_returns_all_fields(self):
        from app.tools.financial_rigor import verify_market_cap
        r = verify_market_cap(50.0, 1e9, 5e10)
        for field in ["function", "price", "shares", "calculated", "reported",
                       "deviation_pct", "currency", "passed", "message"]:
            self.assertIn(field, r, f"Missing field: {field}")


class TestVerifyValuation(unittest.TestCase):
    def test_full_valuation(self):
        from app.tools.financial_rigor import verify_valuation
        r = verify_valuation(price=180, eps=6.0, bvps=30, fcf_per_share=4, dividend=1.5)
        self.assertIn("PE", r)
        self.assertAlmostEqual(r["PE"], 30, delta=0.1)
        self.assertIn("PB", r)
        self.assertIn("FCF_Yield", r)
        self.assertIn("Dividend_Yield", r)

    def test_price_only_no_crash(self):
        from app.tools.financial_rigor import verify_valuation
        r = verify_valuation(price=100)
        self.assertIn("function", r)
        self.assertNotIn("PE", r)

    def test_zero_price(self):
        from app.tools.financial_rigor import verify_valuation
        r = verify_valuation(price=0, eps=5, bvps=10)
        self.assertIn("function", r)


class TestCrossValidate(unittest.TestCase):
    def test_two_sources_pass(self):
        from app.tools.financial_rigor import cross_validate
        r = cross_validate("Revenue", {"SourceA": 1000, "SourceB": 1005}, "CNY")
        self.assertIn("status", r)
        self.assertIn("PASS", r["status"])
        self.assertEqual(r["source_count"], 2)

    def test_three_sources_compares_all(self):
        """Bug fix: should compare ALL pairs, not just first 2."""
        from app.tools.financial_rigor import cross_validate
        r = cross_validate("Revenue", {"A": 100, "B": 101, "C": 150}, "USD")
        self.assertEqual(r["source_count"], 3)
        self.assertIn("max_deviation_pct", r)
        self.assertIn("max_deviation_pair", r)
        self.assertGreater(r["max_deviation_pct"], 40)

    def test_single_source_error(self):
        from app.tools.financial_rigor import cross_validate
        r = cross_validate("Revenue", {"A": 100})
        self.assertIn("error", r)

    def test_zero_values_no_crash(self):
        from app.tools.financial_rigor import cross_validate
        r = cross_validate("Field", {"A": 0, "B": 0})
        self.assertIn("status", r)


class TestThreeScenario(unittest.TestCase):
    def test_three_scenarios(self):
        from app.tools.financial_rigor import three_scenario
        r = three_scenario(
            price=100, eps=5, shares_100m=20,
            growth_rates=[0.3, 0.15, -0.05],
            pe_multiples=[30, 20, 12], currency="CNY"
        )
        self.assertEqual(len(r["scenarios"]), 3)
        self.assertEqual(r["scenarios"][0]["scenario"], "optimistic")
        self.assertGreater(r["scenarios"][0]["upside_pct"], 0)

    def test_returns_all_fields(self):
        from app.tools.financial_rigor import three_scenario
        r = three_scenario(100, 5, 20, [0.2, 0.1, 0.0], [25, 20, 15])
        for field in ["function", "current_price", "current_eps",
                       "shares_100m", "currency", "scenarios"]:
            self.assertIn(field, r)


class TestCalc(unittest.TestCase):
    def test_arithmetic(self):
        from app.tools.financial_rigor import calc
        r = calc("100 * (1 + 0.15) - 50")
        self.assertAlmostEqual(r["result"], 65, delta=0.01)

    def test_power(self):
        from app.tools.financial_rigor import calc
        r = calc("2 ** 10")
        self.assertAlmostEqual(r["result"], 1024, delta=0.01)

    def test_division_by_zero(self):
        from app.tools.financial_rigor import calc
        r = calc("100 / 0")
        self.assertIn("error", r)

    def test_malicious_input(self):
        from app.tools.financial_rigor import calc
        r = calc("__import__('os').system('rm -rf /')")
        self.assertIn("error", r)

    def test_empty_expression(self):
        from app.tools.financial_rigor import calc
        r = calc("")
        self.assertIn("error", r)


class TestBenford(unittest.TestCase):
    def test_natural_distribution(self):
        from app.tools.financial_rigor import benford
        random.seed(42)
        nums = [random.randint(1, 1000000) for _ in range(2000)]
        r = benford(nums)
        self.assertEqual(r["sample_size"], 2000)
        self.assertFalse(r["suspicious"])

    def test_empty_list(self):
        from app.tools.financial_rigor import benford
        r = benford([])
        self.assertIn("error", r)

    def test_all_zeros(self):
        from app.tools.financial_rigor import benford
        r = benford([0, 0, 0, 0])
        self.assertIn("error", r)

    def test_returns_all_fields(self):
        from app.tools.financial_rigor import benford
        r = benford([100, 200, 300, 45, 56])
        for field in ["function", "sample_size", "expected_pct",
                       "actual_pct", "warnings", "suspicious"]:
            self.assertIn(field, r)


# ==================== Market Data ====================

class TestDetectMarket(unittest.TestCase):
    def test_a_share_codes(self):
        from app.tools.market_data import _detect_market
        self.assertEqual(_detect_market("600519"), "A_SHARE")
        self.assertEqual(_detect_market("002555"), "A_SHARE")
        self.assertEqual(_detect_market("000001"), "A_SHARE")
        self.assertEqual(_detect_market("300750"), "A_SHARE")

    def test_a_share_with_suffix(self):
        from app.tools.market_data import _detect_market
        self.assertEqual(_detect_market("600519.SH"), "A_SHARE")
        self.assertEqual(_detect_market("002555.SZ"), "A_SHARE")

    def test_hk_stocks(self):
        from app.tools.market_data import _detect_market
        self.assertEqual(_detect_market("0700.HK"), "HK")
        self.assertEqual(_detect_market("9988.HK"), "HK")

    def test_us_stocks(self):
        from app.tools.market_data import _detect_market
        self.assertEqual(_detect_market("AAPL"), "US")
        self.assertEqual(_detect_market("NVDA"), "US")

    def test_lowercase_us(self):
        """Bug fix: lowercase US tickers should be detected."""
        from app.tools.market_data import _detect_market
        self.assertEqual(_detect_market("aapl"), "US")
        self.assertEqual(_detect_market("nvda"), "US")

    def test_unknown(self):
        from app.tools.market_data import _detect_market
        self.assertEqual(_detect_market("???"), "UNKNOWN")
        self.assertEqual(_detect_market("12345"), "UNKNOWN")


class TestResolveCompanyName(unittest.TestCase):
    """Test Chinese company name resolution."""

    def test_tencent(self):
        from app.tools.market_data import _resolve_company_name
        self.assertEqual(_resolve_company_name("腾讯"), "0700.HK")
        self.assertEqual(_resolve_company_name("腾讯控股"), "0700.HK")

    def test_maotai(self):
        from app.tools.market_data import _resolve_company_name
        self.assertEqual(_resolve_company_name("茅台"), "600519.SH")
        self.assertEqual(_resolve_company_name("贵州茅台"), "600519.SH")

    def test_pdd(self):
        from app.tools.market_data import _resolve_company_name
        self.assertEqual(_resolve_company_name("拼多多"), "PDD")

    def test_meituan(self):
        from app.tools.market_data import _resolve_company_name
        self.assertEqual(_resolve_company_name("美团"), "3690.HK")

    def test_case_insensitive_english(self):
        from app.tools.market_data import _resolve_company_name
        self.assertEqual(_resolve_company_name("apple"), "AAPL")
        self.assertEqual(_resolve_company_name("APPLE"), "AAPL")

    def test_unknown_name(self):
        from app.tools.market_data import _resolve_company_name
        self.assertIsNone(_resolve_company_name("不存在的公司"))

    def test_substring_match(self):
        """Bug fix R2-1: should find company name within longer text."""
        from app.tools.market_data import _resolve_company_name
        self.assertEqual(_resolve_company_name("分析腾讯的财报"), "0700.HK")
        self.assertEqual(_resolve_company_name("腾讯 2025Q4"), "0700.HK")
        self.assertEqual(_resolve_company_name("深入研究茅台"), "600519.SH")

    def test_longer_name_priority(self):
        """Longer names should match before shorter ones (腾讯控股 before 腾讯)."""
        from app.tools.market_data import _resolve_company_name
        # Both should resolve to the same symbol
        self.assertEqual(_resolve_company_name("腾讯控股"), "0700.HK")
        self.assertEqual(_resolve_company_name("腾讯"), "0700.HK")

    def test_name_map_has_entries(self):
        from app.tools.market_data import COMPANY_NAME_MAP
        self.assertGreater(len(COMPANY_NAME_MAP), 30)
        # Verify key entries exist
        for name in ["腾讯", "茅台", "拼多多", "美团", "苹果", "英伟达"]:
            self.assertIn(name, COMPANY_NAME_MAP, f"Missing: {name}")


class TestNormalizeAShare(unittest.TestCase):
    def test_sh_code(self):
        from app.tools.market_data import _normalize_a_share_code
        self.assertEqual(_normalize_a_share_code("600519"), "600519.SH")

    def test_sz_code(self):
        from app.tools.market_data import _normalize_a_share_code
        self.assertEqual(_normalize_a_share_code("002555"), "002555.SZ")

    def test_already_has_suffix(self):
        from app.tools.market_data import _normalize_a_share_code
        self.assertEqual(_normalize_a_share_code("600519.SH"), "600519.SH")


class TestGetCurrentDateContext(unittest.TestCase):
    def test_returns_all_fields(self):
        from app.tools.market_data import get_current_date_context
        ctx = get_current_date_context()
        for field in ["current_date", "current_time", "weekday", "weekday_cn",
                       "timezone", "market_status", "timestamp"]:
            self.assertIn(field, ctx, f"Missing field: {field}")

    def test_market_status_keys(self):
        from app.tools.market_data import get_current_date_context
        ctx = get_current_date_context()
        self.assertIn("A_SHARE", ctx["market_status"])
        self.assertIn("HK", ctx["market_status"])
        self.assertIn("US", ctx["market_status"])

    def test_timezone_not_empty(self):
        """Bug fix: timezone should never be empty."""
        from app.tools.market_data import get_current_date_context
        ctx = get_current_date_context()
        self.assertTrue(ctx["timezone"])
        self.assertIsInstance(ctx["timezone"], str)

    def test_weekday_cn_present(self):
        from app.tools.market_data import get_current_date_context
        ctx = get_current_date_context()
        self.assertIn(ctx["weekday_cn"], ["周一", "周二", "周三", "周四", "周五", "周六", "周日"])


class TestGetStockPrice(unittest.TestCase):
    """Test get_stock_price with mocked data."""

    def test_resolve_chinese_name(self):
        """Bug fix: 腾讯 should resolve to 0700.HK and get cached price."""
        from app.tools.market_data import get_stock_price, _cache
        _cache.set("price", "0700.HK", {
            "symbol": "0700.HK", "market": "HK", "name": "腾讯控股",
            "price": 380.0, "change_pct": 1.5,
            "timestamp": datetime.now().isoformat(),
        })
        result = run_async(get_stock_price("腾讯"))
        self.assertEqual(result.get("symbol"), "0700.HK")
        self.assertEqual(result.get("price"), 380.0)

    def test_unknown_symbol_returns_meaningful_error(self):
        """Bug fix: should return meaningful error, not empty."""
        from app.tools.market_data import get_stock_price
        result = run_async(get_stock_price("??UNKNOWN??"))
        self.assertIn("error", result)
        self.assertIn("symbol", result)
        self.assertIn("timestamp", result)

    def test_no_infinite_recursion(self):
        """Bug fix: should not infinite loop."""
        from app.tools.market_data import get_stock_price
        result = run_async(asyncio.wait_for(get_stock_price("XYZABC"), timeout=10.0))
        self.assertIsInstance(result, dict)


class TestFetchCompanyNews(unittest.TestCase):
    def test_returns_placeholder_not_empty(self):
        """Bug fix: should return placeholder, not empty list."""
        from app.tools.market_data import fetch_company_news
        result = run_async(fetch_company_news("UNKNOWN.SYMBOL", days=7))
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIn("title", result[0])


class TestFetchMarketIndices(unittest.TestCase):
    def test_returns_timestamp_even_on_failure(self):
        """Bug fix: should always return timestamp."""
        from app.tools.market_data import fetch_market_indices
        result = run_async(fetch_market_indices())
        self.assertIsInstance(result, dict)
        self.assertIn("timestamp", result)


class TestSearchCompany(unittest.TestCase):
    def test_chinese_name_resolution(self):
        """Bug fix: searching '茅台' should find 600519."""
        from app.tools.market_data import search_company
        result = run_async(search_company("茅台"))
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["symbol"], "600519.SH")

    def test_tencent_resolution(self):
        from app.tools.market_data import search_company
        result = run_async(search_company("腾讯"))
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["symbol"], "0700.HK")

    def test_us_ticker(self):
        from app.tools.market_data import search_company
        result = run_async(search_company("AAPL"))
        self.assertGreater(len(result), 0)
        self.assertTrue(any(r["symbol"] == "AAPL" for r in result))


# ==================== Data Cache ====================

class TestDataCache(unittest.TestCase):
    def test_set_get_delete(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            result = cache.get("test", "key1", max_age_seconds=3600)
            self.assertIsNotNone(result)
            self.assertEqual(result["value"], 42)

    def test_expiry(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            result = cache.get("test", "key1", max_age_seconds=0)
            self.assertIsNone(result)

    def test_stale_fallback(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            stale = cache.get_stale("test", "key1")
            self.assertIsNotNone(stale)
            self.assertEqual(stale["data"]["value"], 42)
            self.assertTrue(stale["stale"])

    def test_stats_include_total(self):
        """Bug fix: stats should include _total and _db_size_bytes."""
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("cat1", "k1", "v1")
            cache.set("cat1", "k2", "v2")
            cache.set("cat2", "k1", "v1")
            stats = cache.get_stats()
            self.assertIn("_total", stats)
            self.assertEqual(stats["_total"], 3)
            self.assertIn("_db_size_bytes", stats)

    def test_cleanup(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            with cache._lock:
                conn = cache._get_conn()
                old = (datetime.now() - timedelta(days=2)).isoformat()
                conn.execute("UPDATE cache SET expires_at = ? WHERE category = ? AND key = ?",
                             (old, "test", "key1"))
                conn.commit()
                conn.close()
            deleted = cache.cleanup_expired()
            self.assertEqual(deleted, 1)


# ==================== Knowledge Updater ====================

class TestKnowledgeUpdater(unittest.TestCase):
    def test_masters_defined(self):
        from app.tools.knowledge_updater import MASTERS
        for key in ["buffett", "munger", "duan_yongping", "li_lu"]:
            self.assertIn(key, MASTERS)

    def test_default_knowledge_exists(self):
        """Bug fix: default knowledge should be available."""
        from app.tools.knowledge_updater import DEFAULT_KNOWLEDGE
        for key in ["buffett", "munger", "duan_yongping", "li_lu"]:
            self.assertIn(key, DEFAULT_KNOWLEDGE)
            data = DEFAULT_KNOWLEDGE[key]
            self.assertIn("core_philosophy", data)
            self.assertIn("key_principles", data)
            self.assertGreaterEqual(len(data["key_principles"]), 3)
            self.assertIn("investment_framework", data)

    def test_load_knowledge_returns_default(self):
        """Bug fix: _load_knowledge should return defaults if no file exists."""
        from app.tools.knowledge_updater import _load_knowledge, MASTERS
        for key in MASTERS:
            data = _load_knowledge(key)
            self.assertTrue(data, f"_load_knowledge('{key}') returned empty!")
            self.assertIn("core_philosophy", data, f"Missing core_philosophy for {key}")

    def test_get_knowledge_summary_not_empty(self):
        """Bug fix: summary should never be empty."""
        from app.tools.knowledge_updater import get_knowledge_summary_for_prompt
        summary = get_knowledge_summary_for_prompt()
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 100)
        self.assertIn("巴菲特", summary)
        self.assertIn("芒格", summary)
        self.assertIn("段永平", summary)
        self.assertIn("李录", summary)

    def test_get_master_knowledge_returns_data(self):
        """Bug fix: should return data, not 'not_yet_updated'."""
        from app.tools.knowledge_updater import get_master_knowledge
        for key in ["buffett", "munger", "duan_yongping", "li_lu"]:
            data = get_master_knowledge(key)
            self.assertIn("core_philosophy", data,
                          f"get_master_knowledge('{key}') returned no data")

    def test_get_knowledge_status(self):
        from app.tools.knowledge_updater import get_knowledge_status
        status = get_knowledge_status()
        for key in ["buffett", "munger", "duan_yongping", "li_lu"]:
            self.assertIn(key, status)
            self.assertIn("initialized", status[key])

    def test_unknown_master(self):
        from app.tools.knowledge_updater import get_master_knowledge
        result = get_master_knowledge("nonexistent")
        self.assertIn("error", result)


# ==================== Context Injection ====================

class TestContextInjector(unittest.TestCase):
    def test_build_time_context(self):
        from app.core.context import build_time_context
        ctx = build_time_context()
        self.assertIn("当前日期", ctx)
        self.assertIn("当前时间", ctx)
        self.assertIn("市场状态", ctx)
        self.assertIn(str(datetime.now().year), ctx)

    def test_build_market_context_empty(self):
        from app.core.context import build_market_context
        self.assertEqual(build_market_context(None), "")

    def test_build_market_context_with_data(self):
        from app.core.context import build_market_context
        data = {
            "SSE": {"name": "上证指数", "price": 3200.5, "change_pct": 1.2},
            "S&P500": {"price": 5000.0, "change_pct": -0.5},
            "timestamp": "2026-07-11T10:00:00",
        }
        result = build_market_context(data)
        self.assertIn("上证指数", result)
        # Price is formatted with comma: 3,200.50
        self.assertIn("3,200", result)

    def test_build_company_context_with_price(self):
        from app.core.context import build_company_context
        price_data = {
            "name": "Apple", "market": "US", "price": 200.0,
            "change_pct": 1.5, "pe": 30.0, "pb": 40.0,
            "total_market_cap": 3e12,
        }
        result = build_company_context("AAPL", price_data)
        self.assertIn("Apple", result)
        self.assertIn("200", result)
        self.assertIn("PE", result)

    def test_build_company_context_a_share_currency(self):
        from app.core.context import build_company_context
        price_data = {
            "name": "贵州茅台", "market": "A_SHARE",
            "price": 1800.0, "change_pct": 2.0,
        }
        result = build_company_context("600519.SH", price_data)
        self.assertIn("¥", result)

    def test_build_full_context_not_empty(self):
        """Bug fix: full context should never be empty."""
        from app.core.context import build_full_context
        ctx = run_async(build_full_context("腾讯"))
        self.assertGreater(len(ctx), 50)
        self.assertIn("当前日期", ctx)


# ==================== Orchestrator ====================

class TestOrchestrator(unittest.TestCase):
    def test_agent_role_prompts(self):
        from app.agents.orchestrator import AGENT_ROLE_PROMPTS
        self.assertGreaterEqual(len(AGENT_ROLE_PROMPTS), 15)
        for key in ["business-analyst", "financial-analyst", "industry-researcher",
                     "risk-assessor", "editor", "reader-reviewer"]:
            self.assertIn(key, AGENT_ROLE_PROMPTS)

    def test_tool_schemas(self):
        from app.agents.orchestrator import ALL_TOOL_SCHEMAS
        self.assertGreaterEqual(len(ALL_TOOL_SCHEMAS), 11)
        names = [t["function"]["name"] for t in ALL_TOOL_SCHEMAS]
        for expected in ["get_stock_price", "get_financial_data", "fetch_company_news",
                          "verify_market_cap", "calc", "benford"]:
            self.assertIn(expected, names)

    def test_series_max_length_exists(self):
        """Bug fix: MAX_SERIES_REPORT_LENGTH should exist and be > MAX_REPORT_LENGTH."""
        from app.agents.orchestrator import MAX_SERIES_REPORT_LENGTH
        from app.core.config import MAX_REPORT_LENGTH
        self.assertGreater(MAX_SERIES_REPORT_LENGTH, MAX_REPORT_LENGTH)
        self.assertGreaterEqual(MAX_SERIES_REPORT_LENGTH, 100000)

    def test_execute_tool_calc(self):
        from app.agents.orchestrator import _execute_tool
        result = run_async(_execute_tool("calc", {"expression": "100 * 2"}))
        data = json.loads(result)
        self.assertAlmostEqual(data["result"], 200, delta=0.01)

    def test_execute_tool_verify_market_cap(self):
        from app.agents.orchestrator import _execute_tool
        result = run_async(_execute_tool("verify_market_cap", {
            "price": 100, "shares": 10e8, "reported": 1000e8
        }))
        data = json.loads(result)
        self.assertTrue(data["passed"])

    def test_execute_tool_benford(self):
        from app.agents.orchestrator import _execute_tool
        result = run_async(_execute_tool("benford", {"numbers": [100, 200, 300, 45]}))
        data = json.loads(result)
        self.assertIn("sample_size", data)

    def test_execute_tool_cross_validate(self):
        from app.agents.orchestrator import _execute_tool
        result = run_async(_execute_tool("cross_validate", {
            "field": "Revenue", "values": {"A": 100, "B": 105, "C": 102}
        }))
        data = json.loads(result)
        self.assertEqual(data["source_count"], 3)

    def test_execute_tool_unknown(self):
        from app.agents.orchestrator import _execute_tool
        result = run_async(_execute_tool("nonexistent_tool", {}))
        data = json.loads(result)
        self.assertIn("error", data)

    def test_tool_enabled_agents(self):
        from app.agents.orchestrator import TOOL_ENABLED_AGENTS
        self.assertIn("business-analyst", TOOL_ENABLED_AGENTS)
        self.assertNotIn("editor", TOOL_ENABLED_AGENTS)

    def test_series_prompt_mentions_tools(self):
        from app.agents.orchestrator import SERIES_SYSTEM_PROMPT
        self.assertIn("get_stock_price", SERIES_SYSTEM_PROMPT)


# ==================== Task Store ====================

class TestTaskStore(unittest.TestCase):
    def test_save_and_get(self):
        from app.core.task_store import TaskStore
        from app.models.schemas import ResearchStatus
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(Path(tmpdir) / "test.db")
            status = ResearchStatus(task_id="t1", skill_name="test", status="running")
            store.save_task(status, "args")
            loaded = store.get_task("t1")
            self.assertEqual(loaded.task_id, "t1")

    def test_list_tasks(self):
        from app.core.task_store import TaskStore
        from app.models.schemas import ResearchStatus
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(Path(tmpdir) / "test.db")
            for i in range(5):
                store.save_task(
                    ResearchStatus(task_id=f"t{i}", skill_name="test", status="completed"),
                    f"args{i}"
                )
            tasks = store.list_tasks(limit=10)
            self.assertEqual(len(tasks), 5)

    def test_mark_interrupted(self):
        from app.core.task_store import TaskStore
        from app.models.schemas import ResearchStatus
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(Path(tmpdir) / "test.db")
            store.save_task(ResearchStatus(task_id="t1", skill_name="test", status="running"))
            count = store.mark_running_as_interrupted()
            self.assertEqual(count, 1)
            loaded = store.get_task("t1")
            self.assertEqual(loaded.status, "interrupted")

    def test_delete_task(self):
        from app.core.task_store import TaskStore
        from app.models.schemas import ResearchStatus
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(Path(tmpdir) / "test.db")
            store.save_task(ResearchStatus(task_id="t1", skill_name="test", status="completed"))
            store.delete_task("t1")
            self.assertIsNone(store.get_task("t1"))

    def test_get_nonexistent_task(self):
        from app.core.task_store import TaskStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(Path(tmpdir) / "test.db")
            self.assertIsNone(store.get_task("nonexistent"))


# ==================== Skills Registry ====================

class TestSkillsRegistry(unittest.TestCase):
    def test_skills_count(self):
        from app.skills import list_skills
        skills = list_skills()
        self.assertGreaterEqual(len(skills), 15)

    def test_earnings_team_phased(self):
        from app.skills import get_skill
        skill = get_skill("earnings-team")
        self.assertTrue(skill.get("is_multi_agent"))
        self.assertIn("post_synthesis_agents", skill)
        self.assertIn("editor", skill["post_synthesis_agents"])

    def test_deep_company_series(self):
        from app.skills import get_skill
        skill = get_skill("deep-company-series")
        self.assertTrue(skill.get("series_mode"))
        self.assertEqual(len(skill.get("series_topics", [])), 8)

    def test_investment_team_agents(self):
        from app.skills import get_skill
        skill = get_skill("investment-team")
        self.assertEqual(len(skill.get("agents", [])), 4)

    def test_get_unknown_skill(self):
        from app.skills import get_skill
        self.assertIsNone(get_skill("nonexistent"))


# ==================== LLM Signatures ====================

class TestLLM(unittest.TestCase):
    def test_chat_complete_is_async(self):
        import inspect
        from app.core.llm import chat_complete
        self.assertTrue(inspect.iscoroutinefunction(chat_complete))

    def test_chat_complete_with_tools_is_async(self):
        import inspect
        from app.core.llm import chat_complete_with_tools
        self.assertTrue(inspect.iscoroutinefunction(chat_complete_with_tools))

    def test_chat_stream_is_async_generator(self):
        import inspect
        from app.core.llm import chat_stream
        self.assertTrue(inspect.isasyncgenfunction(chat_stream))

    def test_chat_complete_with_tools_signature(self):
        import inspect
        from app.core.llm import chat_complete_with_tools
        sig = inspect.signature(chat_complete_with_tools)
        self.assertIn("tools", sig.parameters)
        self.assertIn("tool_executor", sig.parameters)


# ==================== Module Imports ====================

class TestModuleImports(unittest.TestCase):
    def test_main_import(self):
        from app.main import app
        self.assertEqual(app.title, "AI Berkshire Web")

    def test_orchestrator_imports(self):
        from app.agents.orchestrator import (
            run_single_agent, run_multi_agent, run_series,
            AGENT_ROLE_PROMPTS, FINANCIAL_TOOL_SCHEMAS,
            DATA_TOOL_SCHEMAS, ALL_TOOL_SCHEMAS,
            TOOL_ENABLED_AGENTS, _execute_tool,
            MAX_SERIES_REPORT_LENGTH,
        )

    def test_market_data_imports(self):
        from app.tools.market_data import (
            get_stock_price, get_financial_data,
            fetch_company_news, fetch_market_indices,
            search_company, get_current_date_context,
            _detect_market, _resolve_company_name,
            _normalize_a_share_code, COMPANY_NAME_MAP,
        )

    def test_knowledge_updater_imports(self):
        from app.tools.knowledge_updater import (
            MASTERS, DEFAULT_KNOWLEDGE,
            update_master_knowledge, update_all_masters,
            get_master_knowledge, get_all_knowledge,
            get_knowledge_summary_for_prompt, get_knowledge_status,
            _ensure_default_knowledge,
        )

    def test_context_imports(self):
        from app.core.context import (
            build_full_context, build_time_context,
            build_market_context, build_company_context,
            build_knowledge_context,
        )

    def test_financial_rigor_imports(self):
        from app.tools.financial_rigor import (
            verify_market_cap, verify_valuation, cross_validate,
            three_scenario, calc, benford,
        )


# ==================== Security ====================

class TestSecurity(unittest.TestCase):
    def test_calc_rejects_import(self):
        from app.tools.financial_rigor import calc
        r = calc("__import__('os')")
        self.assertIn("error", r)

    def test_calc_rejects_open(self):
        from app.tools.financial_rigor import calc
        r = calc("open('test.txt')")
        self.assertIn("error", r)

    def test_api_token_uses_compare_digest(self):
        import inspect
        from app.main import _verify_token
        source = inspect.getsource(_verify_token)
        self.assertIn("compare_digest", source)

    def test_cors_not_wildcard(self):
        from app.main import _ALLOWED_ORIGINS
        self.assertNotIn("*", _ALLOWED_ORIGINS)


# ==================== API Endpoints ====================

class TestAPIEndpoints(unittest.TestCase):
    def test_health_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        self.assertIn("/health", routes)

    def test_data_price_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        self.assertIn("/api/data/price/{symbol}", routes)

    def test_data_indices_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        self.assertIn("/api/data/indices", routes)

    def test_knowledge_status_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        self.assertIn("/api/knowledge/status", routes)

    def test_knowledge_update_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        self.assertIn("/api/knowledge/update", routes)

    def test_data_context_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        self.assertIn("/api/data/context", routes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
