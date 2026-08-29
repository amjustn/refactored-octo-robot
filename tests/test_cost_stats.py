#!/usr/bin/env python3
"""成本统计（/api/stats/costs + TaskStore.get_cost_stats）测试。

覆盖：
- get_cost_stats：totals / by_day（缺日补零）/ by_skill（按费用降序）
- 只统计 completed/failed/cancelled；running、interrupted 被排除
- days 窗口按自然日（含今天）计算，窗口外的老任务不计入
- API 形状：JWT 鉴权、默认 days=30、days 越界校验（422）

Run: cd . && venv/bin/python -m pytest tests/test_cost_stats.py -v
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, ".")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core import config as _config
from app.core.task_store import TaskStore
import app.harness.persist as persist_mod

client = TestClient(app)


def _auth_headers():
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        return {"Authorization": f"Bearer {create_token(_config.BERKSHIRE_API_TOKEN)}"}
    return {}


def _seed(repo):
    """插入已知账单行。返回无，断言基于这里的固定数值。"""
    now = datetime.now()

    def ts(days_ago):
        return (now - timedelta(days=days_ago)).isoformat()

    rows = [
        # task_id, skill, status, days_ago, tok_p, tok_c, cost, duration
        ("t1", "investment-team", "completed", 0, 1000, 500, 0.01, 10.0),
        ("t2", "investment-team", "completed", 1, 2000, 1000, 0.02, 20.0),
        ("t3", "company-analysis", "failed", 0, 500, 250, 0.005, 30.0),
        ("t4", "company-analysis", "cancelled", 5, 100, 50, 0.001, 6.0),
        # 以下应被排除：
        ("t5", "investment-team", "running", 0, 9999, 9999, 9.99, 99.0),
        ("t6", "investment-team", "interrupted", 0, 8888, 8888, 8.88, 88.0),
        ("t7", "investment-team", "completed", 40, 7777, 7777, 7.77, 77.0),  # 窗口外
    ]
    conn = sqlite3.connect(str(repo.store.db_path), timeout=10)
    try:
        for tid, skill, status, ago, tp, tc, cost, dur in rows:
            conn.execute(
                """INSERT INTO tasks
                       (task_id, skill_name, status, arguments, agents_json,
                        report, error, created_at, updated_at,
                        tokens_prompt, tokens_completion, cost_yuan, duration_s)
                   VALUES (?, ?, ?, '', '[]', '', '', ?, ?, ?, ?, ?, ?)""",
                (tid, skill, status, ts(ago), ts(ago), tp, tc, cost, dur),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def tmp_repo(tmp_path):
    """独立的临时 tasks.db（含 billing 列迁移），与 test_harness_opt 同法。"""
    repo = persist_mod.Repository(TaskStore(db_path=tmp_path / "tasks.db"))
    _seed(repo)
    return repo


# ==================== TaskStore.get_cost_stats ====================

def test_totals_exclude_running_and_interrupted(tmp_repo):
    stats = tmp_repo.get_cost_stats(days=7)
    t = stats["totals"]
    assert t["runs"] == 4                       # t5/running, t6/interrupted, t7/窗口外 均排除
    assert t["tokens_prompt"] == 3600
    assert t["tokens_completion"] == 1800
    assert t["cost_yuan"] == pytest.approx(0.036, abs=1e-9)
    assert t["avg_duration_s"] == pytest.approx(16.5, abs=1e-9)  # (10+20+30+6)/4


def test_by_day_zero_fill(tmp_repo):
    stats = tmp_repo.get_cost_stats(days=7)
    by_day = stats["by_day"]
    assert len(by_day) == 7
    today = datetime.now().date()
    assert by_day[-1]["date"] == today.isoformat()
    assert by_day[0]["date"] == (today - timedelta(days=6)).isoformat()

    day_map = {d["date"]: d for d in by_day}
    d_today = day_map[today.isoformat()]
    assert d_today["runs"] == 2                  # t1 + t3
    assert d_today["cost_yuan"] == pytest.approx(0.015, abs=1e-9)
    assert d_today["tokens"] == 2250

    d_yesterday = day_map[(today - timedelta(days=1)).isoformat()]
    assert d_yesterday["runs"] == 1
    assert d_yesterday["cost_yuan"] == pytest.approx(0.02, abs=1e-9)

    d_5ago = day_map[(today - timedelta(days=5)).isoformat()]
    assert d_5ago["runs"] == 1

    # 其余天补零
    zero_days = [d for d in by_day if d["runs"] == 0]
    assert len(zero_days) == 4
    for d in zero_days:
        assert d["cost_yuan"] == 0 and d["tokens"] == 0


def test_by_skill_sorted_desc(tmp_repo):
    stats = tmp_repo.get_cost_stats(days=7)
    by_skill = stats["by_skill"]
    assert [s["skill_name"] for s in by_skill] == ["investment-team", "company-analysis"]
    s0 = by_skill[0]
    assert s0["runs"] == 2
    assert s0["cost_yuan"] == pytest.approx(0.03, abs=1e-9)
    assert s0["tokens"] == 4500
    s1 = by_skill[1]
    assert s1["runs"] == 2
    assert s1["cost_yuan"] == pytest.approx(0.006, abs=1e-9)
    assert s1["tokens"] == 900


def test_old_tasks_outside_window(tmp_repo):
    # 40 天前的 completed 任务在 30 天窗口内也不计入
    stats = tmp_repo.get_cost_stats(days=30)
    assert stats["totals"]["runs"] == 4
    assert len(stats["by_day"]) == 30


def test_days_clamped(tmp_repo):
    stats = tmp_repo.get_cost_stats(days=9999)
    assert stats["days"] == 365
    assert len(stats["by_day"]) == 365


# ==================== /api/stats/costs ====================

@pytest.fixture
def api_repo(tmp_repo, monkeypatch):
    monkeypatch.setattr(persist_mod, "_repo", tmp_repo)
    return tmp_repo


def test_api_cost_stats_shape(api_repo):
    res = client.get("/api/stats/costs?days=7", headers=_auth_headers())
    assert res.status_code == 200
    data = res.json()
    assert set(data.keys()) == {"days", "totals", "by_day", "by_skill", "by_model"}
    assert data["days"] == 7
    assert data["totals"]["runs"] == 4
    assert data["totals"]["cost_yuan"] == pytest.approx(0.036, abs=1e-9)
    assert len(data["by_day"]) == 7
    assert data["by_day"][0]["date"] <= data["by_day"][-1]["date"]
    assert data["by_skill"][0]["skill_name"] == "investment-team"
    # 种子数据无模型列 → 全部归入 unknown 分组
    assert [m["model"] for m in data["by_model"]] == ["unknown"]
    assert data["by_model"][0]["runs"] == 4


def test_api_cost_stats_default_days(api_repo):
    res = client.get("/api/stats/costs", headers=_auth_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["days"] == 30
    assert len(data["by_day"]) == 30


def test_api_cost_stats_days_out_of_range(api_repo):
    assert client.get("/api/stats/costs?days=0", headers=_auth_headers()).status_code == 422
    assert client.get("/api/stats/costs?days=500", headers=_auth_headers()).status_code == 422


# ==================== by_model 分组与 model 过滤 ====================

def _seed_models(repo):
    """插入带模型名的账单行（含一行无模型的旧数据）。"""
    now = datetime.now().isoformat()
    rows = [
        # task_id, model, tok_p, tok_c, cost, duration
        ("m1", "glm-4.7", 1000, 500, 0.01, 10.0),
        ("m2", "glm-4.7", 2000, 1000, 0.02, 30.0),
        ("m3", "kimi-k3", 500, 500, 0.04, 20.0),
        ("m4", "", 100, 100, 0.001, 5.0),
    ]
    conn = sqlite3.connect(str(repo.store.db_path), timeout=10)
    try:
        for tid, mdl, tp, tc, cost, dur in rows:
            conn.execute(
                """INSERT INTO tasks
                       (task_id, skill_name, status, arguments, agents_json,
                        report, error, created_at, updated_at,
                        tokens_prompt, tokens_completion, cost_yuan, duration_s, model)
                   VALUES (?, 'investment-team', 'completed', '', '[]', '', '', ?, ?, ?, ?, ?, ?, ?)""",
                (tid, now, now, tp, tc, cost, dur, mdl),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def model_repo(tmp_path):
    repo = persist_mod.Repository(TaskStore(db_path=tmp_path / "tasks.db"))
    _seed_models(repo)
    return repo


def test_by_model_grouping(model_repo):
    stats = model_repo.get_cost_stats(days=7)
    by_model = stats["by_model"]
    # 费用降序：kimi 0.04 > glm 0.03 > unknown 0.001
    assert [m["model"] for m in by_model] == ["kimi-k3", "glm-4.7", "unknown"]
    glm = by_model[1]
    assert glm["runs"] == 2
    assert glm["cost_yuan"] == pytest.approx(0.03, abs=1e-9)
    assert glm["tokens"] == 4500
    assert glm["avg_duration_s"] == pytest.approx(20.0, abs=1e-9)


def test_model_filter(model_repo):
    stats = model_repo.get_cost_stats(days=7, model="glm-4.7")
    t = stats["totals"]
    assert t["runs"] == 2
    assert t["cost_yuan"] == pytest.approx(0.03, abs=1e-9)
    assert t["avg_duration_s"] == pytest.approx(20.0, abs=1e-9)
    assert [m["model"] for m in stats["by_model"]] == ["glm-4.7"]

    # 旧数据（空 model 列）可通过 'unknown' 过滤出来
    stats_unknown = model_repo.get_cost_stats(days=7, model="unknown")
    assert stats_unknown["totals"]["runs"] == 1
    assert stats_unknown["totals"]["cost_yuan"] == pytest.approx(0.001, abs=1e-9)


def test_api_cost_stats_model_filter(model_repo, monkeypatch):
    monkeypatch.setattr(persist_mod, "_repo", model_repo)
    res = client.get("/api/stats/costs?days=7&model=kimi-k3", headers=_auth_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["totals"]["runs"] == 1
    assert data["totals"]["cost_yuan"] == pytest.approx(0.04, abs=1e-9)
    assert [m["model"] for m in data["by_model"]] == ["kimi-k3"]
