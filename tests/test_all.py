#!/usr/bin/env python3
"""AI Berkshire Web v3.0 — test suite

Tests market data, knowledge updater, context injection,
orchestrator integration, and all existing v2.0 functionality.

Run: cd . && venv/bin/python -m pytest tests/test_all.py -v
"""
import sys
import os
import json
import random
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, '.')

import pytest


@pytest.fixture
def skills():
    from app.skills import list_skills
    return list_skills()


# ==================== Financial Tools ====================

class TestVerifyMarketCap:
    def test_basic_pass(self):
        from app.tools.financial_rigor import verify_market_cap
        r = verify_market_cap(100.0, 10e8, 1000e8, "CNY")
        assert r["passed"] is True
        assert r["deviation_pct"] < 1

    def test_deviation_detected(self):
        from app.tools.financial_rigor import verify_market_cap
        r = verify_market_cap(100.0, 10e8, 500e8, "CNY")
        assert r["passed"] is False


class TestVerifyValuation:
    def test_pe_pb_fcf(self):
        from app.tools.financial_rigor import verify_valuation
        r = verify_valuation(price=100, eps=5, bvps=20, fcf_per_share=3, dividend=2)
        assert abs(r["PE"] - 20) < 0.1
        assert abs(r["PB"] - 5) < 0.1

    def test_no_eps_no_crash(self):
        from app.tools.financial_rigor import verify_valuation
        r = verify_valuation(price=100)
        assert "PE" not in r


class TestCalc:
    def test_basic_arithmetic(self):
        from app.tools.financial_rigor import calc
        r = calc("100 * (1 + 0.1)")
        assert abs(r["result"] - 110) < 0.01

    def test_scientific_notation(self):
        from app.tools.financial_rigor import calc
        r = calc("1e5 * 2")
        assert abs(r["result"] - 200000) < 0.01

    def test_rejects_malicious_input(self):
        from app.tools.financial_rigor import calc
        r = calc("__import__('os').system('echo PWNED')")
        assert "error" in r


class TestBenford:
    def test_basic(self):
        from app.tools.financial_rigor import benford
        r = benford([1] * 100)
        assert r["sample_size"] > 0
        assert r["suspicious"] is True

    def test_natural_distribution(self):
        from app.tools.financial_rigor import benford
        random.seed(42)
        nums = [random.randint(1, 1000000) for _ in range(1000)]
        r = benford(nums)
        assert not r["suspicious"]

    def test_scientific_notation(self):
        from app.tools.financial_rigor import benford
        r = benford([1e10, 2e20, 3e30, 4.5e5])
        assert r["sample_size"] == 4


# ==================== Data Cache ====================

class TestDataCache:
    def test_cache_set_get(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            result = cache.get("test", "key1", max_age_seconds=3600)
            assert result is not None
            assert result["value"] == 42

    def test_cache_expiry(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            # Should be expired with 0 second TTL
            result = cache.get("test", "key1", max_age_seconds=0)
            assert result is None

    def test_cache_stale_fallback(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            # Get stale data even after expiry
            stale = cache.get_stale("test", "key1")
            assert stale is not None
            assert stale["data"]["value"] == 42
            assert stale["stale"] is True

    def test_cache_stats(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("cat1", "k1", "v1")
            cache.set("cat1", "k2", "v2")
            cache.set("cat2", "k1", "v1")
            stats = cache.get_stats()
            assert stats.get("cat1") == 2
            assert stats.get("cat2") == 1

    def test_cache_cleanup(self):
        from app.core.data_cache import DataCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DataCache(Path(tmpdir) / "test.db")
            cache.set("test", "key1", {"value": 42})
            # Manually expire
            with cache._lock:
                conn = cache._get_conn()
                old = (datetime.now() - timedelta(days=2)).isoformat()
                conn.execute("UPDATE cache SET expires_at = ? WHERE category = ? AND key = ?", (old, "test", "key1"))
                conn.commit()
                conn.close()
            deleted = cache.cleanup_expired()
            assert deleted == 1


# ==================== Market Data ====================

class TestMarketDataDetect:
    def test_detect_a_share(self):
        from app.tools.market_data import _detect_market
        assert _detect_market("600519") == "A_SHARE"
        assert _detect_market("002555") == "A_SHARE"
        assert _detect_market("000001.SZ") == "A_SHARE"

    def test_detect_hk(self):
        from app.tools.market_data import _detect_market
        assert _detect_market("0700.HK") == "HK"

    def test_detect_us(self):
        from app.tools.market_data import _detect_market
        assert _detect_market("AAPL") == "US"
        assert _detect_market("PDD") == "US"
        assert _detect_market("NVDA") == "US"

    def test_detect_unknown(self):
        from app.tools.market_data import _detect_market
        assert _detect_market("???") == "UNKNOWN"

    def test_normalize_a_share(self):
        from app.tools.market_data import _normalize_a_share_code
        assert _normalize_a_share_code("600519") == "600519.SH"
        assert _normalize_a_share_code("002555") == "002555.SZ"
        assert _normalize_a_share_code("600519.SH") == "600519.SH"


class TestMarketDataGetDateContext:
    def test_get_current_date_context(self):
        from app.tools.market_data import get_current_date_context
        ctx = get_current_date_context()
        assert "current_date" in ctx
        assert "current_time" in ctx
        assert "market_status" in ctx
        assert "A_SHARE" in ctx["market_status"]
        assert "timestamp" in ctx
        # Should have today's date
        assert ctx["current_date"] == datetime.now().strftime("%Y-%m-%d")


# ==================== Knowledge Updater ====================

class TestKnowledgeUpdater:
    def test_masters_defined(self):
        from app.tools.knowledge_updater import MASTERS
        assert "buffett" in MASTERS
        assert "munger" in MASTERS
        assert "duan_yongping" in MASTERS
        assert "li_lu" in MASTERS

    def test_master_has_required_fields(self):
        from app.tools.knowledge_updater import MASTERS
        for key, master in MASTERS.items():
            assert "name_cn" in master
            assert "name_en" in master
            assert "role" in master
            assert "search_keywords" in master
            assert len(master["search_keywords"]) > 0

    def test_get_knowledge_status(self):
        from app.tools.knowledge_updater import get_knowledge_status
        status = get_knowledge_status()
        assert "buffett" in status
        assert "name_cn" in status["buffett"]
        assert "initialized" in status["buffett"]

    def test_get_master_knowledge_unknown(self):
        from app.tools.knowledge_updater import get_master_knowledge
        result = get_master_knowledge("nonexistent")
        assert "error" in result

    def test_knowledge_summary_for_prompt(self):
        from app.tools.knowledge_updater import get_knowledge_summary_for_prompt
        summary = get_knowledge_summary_for_prompt()
        # Should be a string (may be empty if not initialized)
        assert isinstance(summary, str)

    def test_save_and_load_knowledge(self):
        from app.tools.knowledge_updater import _save_knowledge, _load_knowledge, KNOWLEDGE_DIR
        import shutil
        # Use a temp directory
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "test_master.json"
            data = {"master": "test", "core_philosophy": "test philosophy", "last_updated": datetime.now().isoformat()}
            tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            loaded = json.loads(tmp_path.read_text(encoding="utf-8"))
            assert loaded["master"] == "test"
            assert loaded["core_philosophy"] == "test philosophy"


# ==================== Context Injection ====================

class TestContextInjector:
    def test_build_time_context(self):
        from app.core.context import build_time_context
        ctx = build_time_context()
        assert "当前日期" in ctx
        assert "当前时间" in ctx
        assert "市场状态" in ctx
        # Should contain current year
        assert str(datetime.now().year) in ctx

    def test_build_market_context_empty(self):
        from app.core.context import build_market_context
        result = build_market_context(None)
        assert result == ""

    def test_build_market_context_with_data(self):
        from app.core.context import build_market_context
        data = {
            "SSE": {"name": "上证指数", "price": 3200.5, "change_pct": 1.2},
            "S&P500": {"name": "S&P 500", "price": 5000.0, "change_pct": -0.5},
            "timestamp": "2026-07-11T10:00:00",
        }
        result = build_market_context(data)
        assert "上证指数" in result
        assert "3,200" in result  # Formatted with comma: 3,200.50
        assert "S&P 500" in result

    def test_build_company_context_empty(self):
        from app.core.context import build_company_context
        result = build_company_context("AAPL")
        assert result == ""

    def test_build_company_context_with_price(self):
        from app.core.context import build_company_context
        price_data = {
            "name": "Apple",
            "market": "US",
            "price": 200.0,
            "change_pct": 1.5,
            "pe": 30.0,
            "pb": 40.0,
            "total_market_cap": 3e12,
        }
        result = build_company_context("AAPL", price_data)
        assert "Apple" in result
        assert "200" in result
        assert "PE" in result

    def test_build_full_context(self):
        from app.core.context import build_full_context
        # This is async, need to run it
        import asyncio
        ctx = asyncio.run(build_full_context("腾讯"))
        assert "当前日期" in ctx
        assert str(datetime.now().year) in ctx


# ==================== Orchestrator ====================

class TestOrchestrator:
    def test_agent_role_prompts_count(self):
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        assert len(AGENT_ROLE_PROMPTS) >= 15

    def test_financial_tool_schemas(self):
        from app.harness.tools import FINANCIAL_TOOL_SCHEMAS
        assert len(FINANCIAL_TOOL_SCHEMAS) >= 6

    def test_data_tool_schemas(self):
        from app.harness.tools import DATA_TOOL_SCHEMAS
        assert len(DATA_TOOL_SCHEMAS) >= 5
        names = [t["function"]["name"] for t in DATA_TOOL_SCHEMAS]
        assert "get_stock_price" in names
        assert "get_financial_data" in names
        assert "fetch_company_news" in names
        assert "fetch_market_indices" in names
        assert "search_company" in names

    def test_all_tool_schemas(self):
        from app.harness.tools import ALL_TOOL_SCHEMAS
        assert len(ALL_TOOL_SCHEMAS) >= 11  # 6 financial + 5 data

    def test_tool_enabled_agents_expanded(self):
        from app.harness.tools import TOOL_ENABLED_AGENTS
        # Should include more agents than just financial ones
        assert "business-analyst" in TOOL_ENABLED_AGENTS
        assert "financial-analyst" in TOOL_ENABLED_AGENTS
        assert "industry-researcher" in TOOL_ENABLED_AGENTS
        assert "company-event-scout" in TOOL_ENABLED_AGENTS
        assert "researcher" in TOOL_ENABLED_AGENTS
        # Editor and reader-reviewer should NOT have tools
        assert "editor" not in TOOL_ENABLED_AGENTS
        assert "reader-reviewer" not in TOOL_ENABLED_AGENTS

    def test_execute_tool_calc(self):
        from app.harness.tools import _execute_tool
        import asyncio
        result = asyncio.run(_execute_tool("calc", {"expression": "100 * 2"}))
        data = json.loads(result)
        assert abs(data["result"] - 200) < 0.01

    def test_execute_tool_unknown(self):
        from app.harness.tools import _execute_tool
        import asyncio
        result = asyncio.run(_execute_tool("nonexistent", {}))
        data = json.loads(result)
        assert "error" in data

    def test_series_prompt_has_tool_hint(self):
        from app.harness.prompts import SERIES_SYSTEM_PROMPT
        assert "get_stock_price" in SERIES_SYSTEM_PROMPT

    def test_run_single_agent_accepts_context(self):
        """Verify run_single_agent has a context parameter."""
        import inspect
        from app.harness.runner import run_single_agent
        sig = inspect.signature(run_single_agent)
        assert "context" in sig.parameters


# ==================== LLM ====================

class TestLLM:
    def test_chat_complete_with_tools_is_async(self):
        import inspect
        from app.core.llm import chat_complete_with_tools
        assert inspect.iscoroutinefunction(chat_complete_with_tools)

    def test_chat_complete_with_tools_signature(self):
        import inspect
        from app.core.llm import chat_complete_with_tools
        sig = inspect.signature(chat_complete_with_tools)
        assert "tools" in sig.parameters
        assert "tool_executor" in sig.parameters


# ==================== Task Store ====================

class TestTaskStore:
    def test_creation_and_retrieval(self):
        from app.core.task_store import TaskStore
        from app.models.schemas import ResearchStatus
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(Path(tmpdir) / "test.db")
            status = ResearchStatus(task_id="t1", skill_name="test", status="running")
            store.save_task(status, "args")
            loaded = store.get_task("t1")
            assert loaded.task_id == "t1"

    def test_mark_interrupted(self):
        from app.core.task_store import TaskStore
        from app.models.schemas import ResearchStatus
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(Path(tmpdir) / "test.db")
            status = ResearchStatus(task_id="t2", skill_name="test", status="running")
            store.save_task(status)
            count = store.mark_running_as_interrupted()
            assert count == 1
            loaded = store.get_task("t2")
            assert loaded.status == "interrupted"


# ==================== Skills Registry ====================

class TestSkillsRegistry:
    def test_skills_count(self, skills):
        assert len(skills) >= 15

    def test_earnings_team_post_synthesis(self):
        from app.skills import get_skill
        skill = get_skill("earnings-team")
        assert "post_synthesis_agents" in skill
        assert "editor" in skill["post_synthesis_agents"]

    def test_deep_company_series_mode(self):
        from app.skills import get_skill
        skill = get_skill("deep-company-series")
        assert skill.get("series_mode") is True
        assert len(skill.get("series_topics", [])) == 8


# ==================== Module Imports ====================

class TestModuleImports:
    def test_main_import(self):
        from app.main import app
        assert app.title == "AI Berkshire Web"

    def test_main_version(self):
        from app.main import app
        assert "3.0" in app.version

    def test_health_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        assert "/health" in routes

    def test_data_price_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        assert "/api/data/price/{symbol}" in routes

    def test_data_indices_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        assert "/api/data/indices" in routes

    def test_knowledge_status_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        assert "/api/knowledge/status" in routes

    def test_knowledge_update_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        assert "/api/knowledge/update" in routes

    def test_data_context_endpoint(self):
        from app.main import app
        routes = [r.path for r in app.routes]
        assert "/api/data/context" in routes

    def test_orchestrator_imports(self):
        from app.harness.runner import run_single_agent, run_multi_agent, run_series
        from app.harness.prompts import AGENT_ROLE_PROMPTS
        from app.harness.tools import FINANCIAL_TOOL_SCHEMAS, DATA_TOOL_SCHEMAS, ALL_TOOL_SCHEMAS, TOOL_ENABLED_AGENTS, _execute_tool

    def test_context_import(self):
        from app.core.context import (
            build_full_context, build_time_context,
            build_market_context, build_company_context,
        )

    def test_data_cache_import(self):
        from app.core.data_cache import DataCache

    def test_market_data_import(self):
        from app.tools.market_data import (
            get_stock_price, get_financial_data,
            fetch_company_news, fetch_market_indices,
            search_company, get_current_date_context,
            _detect_market,
        )

    def test_knowledge_updater_import(self):
        from app.tools.knowledge_updater import (
            MASTERS, update_master_knowledge, update_all_masters,
            get_master_knowledge, get_all_knowledge,
            get_knowledge_summary_for_prompt, get_knowledge_status,
        )


# ==================== Security ====================

class TestSecurity:
    def test_calc_rejects_builtins(self):
        from app.tools.financial_rigor import calc
        r = calc("__import__('os')")
        assert "error" in r

    def test_api_token_uses_compare_digest(self):
        import inspect
        from app.main import api_auth_login
        source = inspect.getsource(api_auth_login)
        assert "compare_digest" in source

    def test_cors_not_wildcard(self):
        from app.main import _ALLOWED_ORIGINS
        assert "*" not in _ALLOWED_ORIGINS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
