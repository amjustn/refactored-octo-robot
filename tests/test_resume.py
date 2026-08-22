#!/usr/bin/env python3
"""断点续跑（resume）测试。

覆盖：
- prepare_resume：plan 组装（跳过成功 Agent / 补跑失败与缺失 Agent）、
  各类拒绝路径（任务不存在 / 运行中 / 单Agent技能 / 无可复用成果）
- save_billing accumulate：续跑消耗累加而非覆盖
- save_report overwrite + task_id：成功续跑覆盖 partial 报告、meta 去 partial
- POST /api/tasks/{id}/resume：200 + skipped/rerun 名单；未知任务 400

"""
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient

import app.harness as harness
import app.harness.persist as persist_mod
from app.core import config as _config
from app.core.task_store import TaskStore
from app.main import app  # 先于 routers 导入，避免 main→routers→main 循环
import app.routers.research as research_mod
from app.skills import get_skill

client = TestClient(app)


def _auth_headers():
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        return {"Authorization": f"Bearer {create_token(_config.BERKSHIRE_API_TOKEN)}"}
    return {}


@pytest.fixture
def tmp_repo(monkeypatch, tmp_path):
    """临时 tasks.db + reports/（与 test_harness_opt 同法）。"""
    repo = persist_mod.Repository(TaskStore(db_path=tmp_path / "tasks.db"))
    monkeypatch.setattr(persist_mod, "_repo", repo)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    monkeypatch.setattr(persist_mod, "REPORTS_DIR", reports_dir)
    return repo


def _seed_failed_multi(repo, task_id="t-resume"):
    """一个 4-Agent 任务：2 成功 + 1 失败 + 1 未产出，状态 failed。"""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(repo.store.db_path), timeout=10)
    try:
        conn.execute(
            """INSERT INTO tasks (task_id, skill_name, status, arguments, created_at, updated_at)
               VALUES (?, 'investment-team', 'failed', '测试公司', ?, ?)""",
            (task_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    repo.save_artifact(task_id, "business-analyst", "商业模式分析成果")
    repo.save_artifact(task_id, "financial-analyst", "财务估值分析成果")
    repo.save_artifact(task_id, "industry-researcher", "[错误] Agent 超时 (300s)")
    return task_id


# ==================== prepare_resume ====================

def test_prepare_resume_plan(tmp_repo):
    tid = _seed_failed_multi(tmp_repo)
    plan, error = asyncio.run(harness.prepare_resume(tid))
    assert error is None
    assert plan["spec"].task_id == tid
    assert plan["spec"].skill_name == "investment-team"
    assert plan["spec"].arguments == "测试公司"
    assert set(plan["skipped_agents"]) == {"business-analyst", "financial-analyst"}
    assert set(plan["rerun_agents"]) == {"industry-researcher", "risk-assessor"}
    assert plan["prefill_results"]["business-analyst"] == "商业模式分析成果"


def test_prepare_resume_rejects(tmp_repo):
    # 任务不存在
    _, err = asyncio.run(harness.prepare_resume("no-such-task"))
    assert err == "任务不存在"

    # 无可复用成果（全部失败）
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(tmp_repo.store.db_path), timeout=10)
    conn.execute(
        """INSERT INTO tasks (task_id, skill_name, status, arguments, created_at, updated_at)
           VALUES ('t-allfail', 'investment-team', 'failed', 'X', ?, ?)""",
        (now, now),
    )
    conn.commit()
    conn.close()
    tmp_repo.save_artifact("t-allfail", "business-analyst", "[错误] boom")
    _, err = asyncio.run(harness.prepare_resume("t-allfail"))
    assert "没有可复用" in err

    # 单Agent技能不支持
    conn = sqlite3.connect(str(tmp_repo.store.db_path), timeout=10)
    conn.execute(
        """INSERT INTO tasks (task_id, skill_name, status, arguments, created_at, updated_at)
           VALUES ('t-single', 'dyp-ask', 'failed', 'X', ?, ?)""",
        (now, now),
    )
    conn.commit()
    conn.close()
    _, err = asyncio.run(harness.prepare_resume("t-single"))
    assert "不支持断点续跑" in err


# ==================== billing accumulate ====================

def test_billing_accumulate(tmp_repo):
    tid = _seed_failed_multi(tmp_repo)
    tmp_repo.save_billing(tid, {"prompt_tokens": 1000, "completion_tokens": 500}, 10.0, model="deepseek-v4-flash")
    tmp_repo.save_billing(
        tid, {"prompt_tokens": 2000, "completion_tokens": 1000}, 20.0,
        model="deepseek-v4-flash", accumulate=True,
    )
    b = tmp_repo.get_billing(tid)
    assert b["tokens_prompt"] == 3000
    assert b["tokens_completion"] == 1500
    assert b["duration_s"] == pytest.approx(30.0)
    assert b["cost_yuan"] == pytest.approx(
        persist_mod.compute_cost_yuan({"prompt_tokens": 1000, "completion_tokens": 500}, "deepseek-v4-flash")
        + persist_mod.compute_cost_yuan({"prompt_tokens": 2000, "completion_tokens": 1000}, "deepseek-v4-flash"),
        abs=1e-6,
    )


# ==================== report overwrite + task_id ====================

def test_report_overwrite_replaces_partial(tmp_repo):
    p1 = tmp_repo.save_report("investment-team", "测试公司", "# 部分报告", partial=True, task_id="t-resume")
    assert json.loads(p1.with_suffix(".meta.json").read_text(encoding="utf-8"))["partial"] is True

    # 反查：task_id 命中
    assert tmp_repo.find_report_name_for_task("t-resume") == p1.name

    p2 = tmp_repo.save_report(
        "investment-team", "测试公司", "# 完整报告",
        task_id="t-resume", overwrite_name=p1.name,
    )
    assert p2.name == p1.name  # 同一个文件被覆盖
    meta = json.loads(p2.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert "partial" not in meta
    assert meta["task_id"] == "t-resume"
    assert p2.read_text(encoding="utf-8") == "# 完整报告"


def test_find_report_legacy_fallback(tmp_repo):
    """旧 partial 报告（meta 无 task_id）按 skill+arguments 模糊匹配。"""
    p = tmp_repo.save_report("investment-team", "测试公司", "# 旧部分报告", partial=True)
    assert tmp_repo.find_report_name_for_task("t-legacy", "investment-team", "测试公司") == p.name
    assert tmp_repo.find_report_name_for_task("t-legacy", "investment-team", "别的目标") is None


# ==================== API ====================

def test_api_resume_ok(tmp_repo, monkeypatch):
    tid = _seed_failed_multi(tmp_repo)
    captured = {}

    async def fake_run(spec, bus=None, **kwargs):
        captured["spec"] = spec
        captured["kwargs"] = kwargs
        return harness.RunResult(task_id=spec.task_id, status="completed", report="# ok")

    monkeypatch.setattr(research_mod, "harness_run", fake_run)
    res = client.post(f"/api/tasks/{tid}/resume", headers=_auth_headers(), json={})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "resumed"
    assert set(data["skipped_agents"]) == {"business-analyst", "financial-analyst"}
    assert set(data["rerun_agents"]) == {"industry-researcher", "risk-assessor"}
    # 后台任务已启动（fake_run 被调度），参数正确
    asyncio.get_event_loop  # noqa: B018 — keep flake quiet about loop usage below
    import time
    for _ in range(50):
        if captured:
            break
        time.sleep(0.05)
    assert captured["spec"].task_id == tid
    assert captured["kwargs"]["billing_accumulate"] is True
    assert "business-analyst" in captured["kwargs"]["prefill_results"]


def test_api_resume_not_found(tmp_repo):
    res = client.post("/api/tasks/nope/resume", headers=_auth_headers(), json={})
    assert res.status_code == 400
    assert "不存在" in res.json()["detail"]


def test_skill_roster_sanity():
    """锁定 investment-team 花名册假设（测试数据依赖它）。"""
    skill = get_skill("investment-team")
    assert skill["is_multi_agent"]
    assert "business-analyst" in skill["agents"]
    assert "risk-assessor" in skill["agents"]
