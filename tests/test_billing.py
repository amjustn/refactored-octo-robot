#!/usr/bin/env python3
"""计费修复（A 组）回归测试。

覆盖：
- A1: MODEL_PRICING 前缀匹配（最长前缀优先）、ollama 本地免费、
      未识别模型记 0 + priced=False；compute_cost_yuan 旧契约回落 default
- A2: _record_usage 按 model 归集 by_model；save_billing 明细计价求和 +
      usage_json 落库；accumulate 合并 per-model 明细；get_cost_stats
      by_model 使用明细数据
- A3: 取消的运行也按 partial tokens 计费
- B3: 同 task_id 重复 run 被拒绝（不重复注册）

Run: venv/bin/python -m pytest tests/test_billing.py -v
"""
import os
import sys

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import sqlite3

import pytest


@pytest.fixture
def tmp_repo(monkeypatch, tmp_path):
    import app.harness.persist as persist_mod
    from app.core.task_store import TaskStore

    repo = persist_mod.Repository(TaskStore(db_path=tmp_path / "tasks.db"))
    monkeypatch.setattr(persist_mod, "_repo", repo)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    monkeypatch.setattr(persist_mod, "REPORTS_DIR", reports_dir)
    return repo


# ==================== A1: 价目前缀匹配 ====================

class TestPricing:
    def test_prefix_match_exact_catalog_models(self):
        from app.harness.persist import compute_cost_detail
        for model in (
            "deepseek-v4-pro", "deepseek-v4-flash",
            "kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed",
            "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking-251104",
            "glm-5.2", "glm-5.1", "glm-5", "glm-4.7", "glm-4.6", "glm-4.5", "glm-4.5-air",
            "gpt-5.6", "gpt-5.5-pro", "gpt-5.4-pro", "gpt-5.1-codex-max", "o4-mini",
            "claude-opus-5", "claude-opus-4-7-20260416", "claude-sonnet-5",
            "claude-sonnet-4-6", "claude-opus-4-6-20260205",
            "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-pro",
            "gemini-2.5-flash", "gemini-2.0-flash",
            "qwen3-max", "qwen3-plus", "qwen3-turbo", "qwen-vl-max", "qwen-vl-plus",
            "ernie-4.5-turbo", "ernie-4.5", "ernie-4.0-turbo", "ernie-speed", "ernie-lite",
            "hunyuan-turbos", "hunyuan-t1", "hunyuan-a13b", "hunyuan-lite",
            "doubao-1.5-pro-256k", "doubao-1.5-lite-128k", "doubao-1.5-vision-pro",
        ):
            d = compute_cost_detail({"prompt_tokens": 1000, "completion_tokens": 1000}, model)
            assert d["priced"] is True, model

    def test_longest_prefix_wins(self):
        from app.harness.persist import MODEL_PRICING, compute_cost_detail
        d = compute_cost_detail({"prompt_tokens": 1_000_000, "completion_tokens": 0},
                                "kimi-k2.7-code-highspeed")
        assert d["cost_yuan"] == MODEL_PRICING["kimi-k2.7-code-highspeed"]["prompt"]
        # 退化前缀也命中
        d2 = compute_cost_detail({"prompt_tokens": 1_000_000, "completion_tokens": 0},
                                 "kimi-k2.7-code-highspeed-fast")
        assert d2["priced"] is True

    def test_ollama_local_is_free(self):
        from app.harness.persist import compute_cost_detail
        d = compute_cost_detail({"prompt_tokens": 10**9, "completion_tokens": 10**9}, "qwen3:14b")
        assert d == {"cost_yuan": 0.0, "priced": True}
        # deepseek-r1:8b 不得被 deepseek 前缀误吞
        d2 = compute_cost_detail({"prompt_tokens": 10**9, "completion_tokens": 0}, "deepseek-r1:8b")
        assert d2 == {"cost_yuan": 0.0, "priced": True}

    def test_unknown_model_zero_and_unpriced(self):
        from app.harness.persist import compute_cost_detail
        d = compute_cost_detail({"prompt_tokens": 10**9, "completion_tokens": 10**9}, "some-proxy-x")
        assert d == {"cost_yuan": 0.0, "priced": False}

    def test_legacy_compute_cost_yuan_falls_back_to_default(self):
        from app.harness.persist import compute_cost_yuan
        assert compute_cost_yuan({"prompt_tokens": 1_000_000, "completion_tokens": 0}) > 0
        assert compute_cost_yuan({"prompt_tokens": 1_000_000, "completion_tokens": 0},
                                 "unlisted-model") > 0


# ==================== A2: per-model 归集与计价 ====================

class TestPerModelUsage:
    def test_record_usage_by_model(self):
        from app.core import llm
        task_id = "ut-by-model"
        llm.set_usage_context(task_id)
        usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        llm._record_usage(usage(), "kimi-k3")
        llm._record_usage(usage(), "glm-4.7")
        llm._record_usage(usage(), "kimi-k3")
        totals = llm.get_usage(task_id)
        assert totals["prompt_tokens"] == 30
        assert totals["by_model"]["kimi-k3"]["prompt_tokens"] == 20
        assert totals["by_model"]["glm-4.7"]["completion_tokens"] == 5
        llm.clear_usage(task_id)
        assert llm.get_usage(task_id)["prompt_tokens"] == 0

    def test_save_billing_per_model_sum_and_column(self, tmp_repo):
        from app.models.schemas import ResearchStatus
        tmp_repo.save_task(ResearchStatus(task_id="bm1", skill_name="s", status="running"))
        usage = {
            "prompt_tokens": 2_000_000, "completion_tokens": 0, "total_tokens": 2_000_000,
            "by_model": {
                "kimi-k3": {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000},
                "glm-4.7": {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000},
            },
        }
        record = tmp_repo.save_billing("bm1", usage, 5.0, model="kimi-k3")
        # kimi-k3 4元/M + glm-4.7 2元/M = 6
        assert record["cost_yuan"] == pytest.approx(6.0)
        assert record["priced"] is True
        assert [r["model"] for r in record["by_model"]] == ["kimi-k3", "glm-4.7"]

        conn = sqlite3.connect(str(tmp_repo.store.db_path), timeout=10)
        raw = conn.execute("SELECT usage_json FROM tasks WHERE task_id='bm1'").fetchone()[0]
        conn.close()
        detail = json.loads(raw)
        assert {d["model"] for d in detail} == {"kimi-k3", "glm-4.7"}

    def test_save_billing_unknown_model_marks_unpriced(self, tmp_repo):
        from app.models.schemas import ResearchStatus
        tmp_repo.save_task(ResearchStatus(task_id="bm2", skill_name="s", status="running"))
        record = tmp_repo.save_billing(
            "bm2", {"prompt_tokens": 1000, "completion_tokens": 500}, 1.0, model="mystery-x")
        assert record["cost_yuan"] == 0.0
        assert record["priced"] is False

    def test_accumulate_merges_breakdown(self, tmp_repo):
        from app.models.schemas import ResearchStatus
        tmp_repo.save_task(ResearchStatus(task_id="bm3", skill_name="s", status="running"))
        u1 = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
              "by_model": {"kimi-k3": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}}
        tmp_repo.save_billing("bm3", u1, 5.0, model="kimi-k3")
        u2 = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
              "by_model": {"glm-4.7": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}}
        record = tmp_repo.save_billing("bm3", u2, 5.0, model="glm-4.7", accumulate=True)
        assert record["cost_yuan"] == pytest.approx(2.0)  # 本次费用
        assert {r["model"] for r in record["by_model"]} == {"kimi-k3", "glm-4.7"}  # 合并明细
        billing = tmp_repo.get_billing("bm3")
        assert billing["cost_yuan"] == pytest.approx(6.0)
        assert {r["model"] for r in billing["by_model"]} == {"kimi-k3", "glm-4.7"}

    def test_cost_stats_by_model_uses_detail(self, tmp_repo):
        from app.models.schemas import ResearchStatus
        st = ResearchStatus(task_id="cs1", skill_name="s", status="running")
        tmp_repo.save_task(st)
        st.status = "completed"
        tmp_repo.save_task(st)
        usage = {
            "prompt_tokens": 2_000_000, "completion_tokens": 0,
            "by_model": {
                "kimi-k3": {"prompt_tokens": 1_500_000, "completion_tokens": 0},
                "glm-4.7": {"prompt_tokens": 500_000, "completion_tokens": 0},
            },
        }
        # model 列写主模型 kimi-k3；by_model 应按明细拆出 glm-4.7
        tmp_repo.save_billing("cs1", usage, 10.0, model="kimi-k3")
        stats = tmp_repo.get_cost_stats(days=7)
        models = {m["model"]: m for m in stats["by_model"]}
        assert set(models) == {"kimi-k3", "glm-4.7"}
        assert models["kimi-k3"]["cost_yuan"] == pytest.approx(6.0)
        assert models["glm-4.7"]["cost_yuan"] == pytest.approx(1.0)
        assert stats["totals"]["cost_yuan"] == pytest.approx(7.0)


# ==================== A3 + B3: harness run 路径 ====================

def _make_spec(skill_name="investment-team", arguments="测试目标"):
    from app.harness.spec import TaskSpec
    from app.skills import get_skill
    return TaskSpec.build(get_skill(skill_name), arguments)


class TestRunBillingPaths:
    def test_cancelled_run_is_billed(self, tmp_repo, monkeypatch):
        """A3: 取消时按已消耗的 partial tokens 落库计费。"""
        import app.harness as harness
        from app.core import llm
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            llm.set_usage_context(spec.task_id)
            usage = type("U", (), {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                                   "total_tokens": 1_000_000})
            llm._record_usage(usage(), "kimi-k3")
            raise asyncio.CancelledError

        monkeypatch.setattr(harness, "_execute", fake_execute)
        spec = _make_spec()
        result = asyncio.run(harness.run(spec, EventBus()))
        assert result.status == "cancelled"
        billing = tmp_repo.get_billing(spec.task_id)
        assert billing["tokens_prompt"] == 1_000_000
        assert billing["cost_yuan"] == pytest.approx(4.0)  # kimi-k3 prompt 价
        assert billing["by_model"][0]["model"] == "kimi-k3"

    def test_duplicate_task_id_run_rejected(self, tmp_repo, monkeypatch):
        """B3: 同 task_id 已有 running 任务时第二个 run 立即拒绝且不覆盖注册。"""
        import app.harness as harness
        from app.harness.events import EventBus, EventType

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_execute(spec, bus, **kwargs):
            started.set()
            await release.wait()
            return "# 报告\n\n正文", []

        monkeypatch.setattr(harness, "_execute", fake_execute)

        async def main():
            bus = EventBus()
            spec = _make_spec()
            q = bus.subscribe(spec.task_id)
            t1 = asyncio.create_task(harness.run(spec, bus))
            await asyncio.wait_for(started.wait(), timeout=2)
            # 同 task_id 再跑（模拟 resume 双跑）
            r2 = await harness.run(spec, bus)
            assert r2.status == "error"
            assert "已在运行" in r2.error
            assert harness._active[spec.task_id].status == "running"
            # duplicate 错误事件已发出
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            assert any(e.type == EventType.ERROR and e.payload.get("code") == "duplicate"
                       for e in events)
            release.set()
            r1 = await t1
            assert r1.status == "completed"

        asyncio.run(main())

    def test_find_running_task_matches_skill_and_args(self, tmp_repo, monkeypatch):
        import app.harness as harness
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            return "# R\n\nx", []

        monkeypatch.setattr(harness, "_execute", fake_execute)
        spec = _make_spec(arguments="贵州茅台")
        asyncio.run(harness.run(spec, EventBus()))
        # 完成后 _active 仍是 completed 状态（未 forget），find 只匹配 running
        assert harness.find_running_task("investment-team", "贵州茅台") is None
