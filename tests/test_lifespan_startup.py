#!/usr/bin/env python3
"""lifespan 迁移回归门（REVIEW_FINDINGS.md Q6）。

- startup 逻辑已从 @app.on_event("startup") 迁到 lifespan；
- 只有 ``with TestClient(app)``（context manager）才会触发 lifespan，
  裸 ``TestClient(app)`` 不触发 —— 本用例用 with 形式验证启动逻辑真实执行。

Run: cd <repo-root> && venv/bin/python -m pytest tests/test_lifespan_startup.py -v
"""
import logging
import os
import sys

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

import app.main as main_mod


def test_app_wired_with_lifespan():
    """应用应挂载 lifespan（而非裸 FastAPI）。"""
    assert main_mod.app.router.lifespan_context is not None
    # 旧的 on_event startup 已删除，避免与 lifespan 双跑
    assert not hasattr(main_mod, "_startup")
    assert not hasattr(main_mod, "_startup_event")


def test_lifespan_startup_executes_on_context_enter(caplog):
    """with TestClient(app) 进入时应执行启动迁移/种子并打印 started 日志。"""
    caplog.set_level(logging.INFO, logger="ai_berkshire")
    with TestClient(main_mod.app) as client:
        # /api/llm/default 在鉴权白名单内（公开端点），顺带验证服务可用
        r = client.get("/api/llm/default")
        assert r.status_code == 200
        r.raise_for_status()
    assert any(
        "AI Berkshire Web v3.0.0 started" in rec.getMessage()
        for rec in caplog.records
    ), "lifespan 启动日志未出现 —— startup 未执行？"
