"""Shared pytest fixtures — reset cross-test global state.

harness 的全局注册表（_active / _jobs / _active_args / _run_semaphores）与
web 层限流器（_rate_limiter）是进程级单例：测试用例里 asyncio.run() 的
loop 关闭后，_forget_later 的延时清理任务随之被取消，注册表条目永远残留，
导致并发闸等用例在全量套件中读到别的用例留下的任务（flaky）。
每个用例前后统一复位这些全局状态，保证用例间互不可见。
"""
import os
import sys

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 2026-08-27: 以下 3 个是"顶层断言式"验证脚本（bash 直跑），不是 pytest 用例。
# 其顶层 async def test_xxx 会被 pytest 误收集并报 fixture 错误，故显式排除。
# 单独运行方式不变：venv/bin/python3 tests/<file>.py
collect_ignore_glob = [
    "test_bug_fixes_round1.py",
    "test_bug_fixes_round2.py",
    "test_skill_knowledge_full_chain.py",
]

import pytest


@pytest.fixture(autouse=True)
def _reset_harness_globals():
    """每个用例前后复位 harness 全局注册表与事件循环信号量。"""
    import app.harness as harness

    def _clear():
        harness._active.clear()
        harness._jobs.clear()
        harness._active_args.clear()
        harness._run_semaphores.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """每个用例前后清空 IP 限流桶（web_common 单一事实源，app.main 兼容再导出）。"""
    limiter = None
    for mod_name in ("app.web_common", "app.main"):
        try:
            import importlib
            limiter = getattr(importlib.import_module(mod_name), "_rate_limiter", None)
        except Exception:
            limiter = None
        if limiter is not None:
            break
    if limiter is None:
        yield
        return
    limiter.clear()
    yield
    limiter.clear()
