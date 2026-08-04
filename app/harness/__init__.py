"""Harness public API — the only entry points main.py (and eval) may use.

    run(spec)        -> execute a TaskSpec, emitting events on the EventBus
    cancel(task_id)  -> server-side cancellation of a running task
    get_status(...)  -> active-task lookup with DB fallback

The harness knows nothing about HTTP or WebSockets; endpoints are thin
translation layers that build a TaskSpec and forward events.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import time
from typing import Optional

from ..core.config import MAX_CONCURRENT_RUNS
from ..core.llm import set_usage_context, get_usage, clear_usage
from ..models.schemas import AgentProgress, ResearchStatus
from .events import EventBus, EventType, get_bus
from .persist import Repository, get_repository
from .spec import TaskSpec

logger = logging.getLogger("ai_berkshire.harness")

_MAX_TASKS = 100
_FORGET_DELAY_S = 3600

# Active task state (replaces main.py's active_tasks / running_jobs)
_active: dict[str, ResearchStatus] = {}
_jobs: dict[str, asyncio.Task] = {}

# Global concurrency gate: one semaphore per event loop, created lazily
# (asyncio.Semaphore binds to the running loop; the app uses one loop,
# tests/eval may create others).
_run_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _run_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _run_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        _run_semaphores[loop] = sem
    return sem


@dataclass
class RunResult:
    task_id: str
    status: str                      # completed / failed / cancelled / error (busy)
    report: str = ""
    report_path: str = ""
    duration_seconds: float = 0.0
    tokens: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)   # tokens + cost_yuan
    error: str = ""


# ==================== task registry ====================

async def _register(status: ResearchStatus, arguments: str, repo: Repository):
    if len(_active) >= _MAX_TASKS:
        oldest_id = next(iter(_active))
        _active.pop(oldest_id, None)
    _active[status.task_id] = status
    await repo.asave_task(status, arguments)


async def _persist_active(task_id: str, arguments: str, repo: Repository):
    if task_id in _active:
        await repo.asave_task(_active[task_id], arguments)


async def _forget_later(task_id: str, delay: int = _FORGET_DELAY_S):
    await asyncio.sleep(delay)
    _active.pop(task_id, None)


def active_count() -> int:
    return len(_active)


def get_status(task_id: str):
    """Active ResearchStatus, else DB row, else None."""
    if task_id in _active:
        return _active[task_id]
    return get_repository().get_task(task_id)


def cancel(task_id: str) -> dict:
    """Cancel a running task server-side (stops LLM calls)."""
    job = _jobs.get(task_id)
    if not job:
        return {"cancelled": False, "reason": "task not running or not found"}
    job.cancel()
    status = _active.get(task_id)
    if status:
        status.status = "cancelled"
        status.error = "已被用户取消"
        get_repository().save_task(status)
    return {"cancelled": True, "task_id": task_id}


# ==================== execution ====================

async def _execute(spec: TaskSpec, bus: EventBus) -> str:
    """Dispatch to AgentRunner (single / multi / series unified)."""
    from .runner import AgentRunner

    runner = AgentRunner(bus=bus, task_id=spec.task_id)
    return await runner.run(spec)


async def _save_partial_report(spec: TaskSpec, repo: Repository, reason: str):
    """Persist completed agent artifacts as a partial report.

    Called on cancelled/failed runs that never produced a final report so
    finished agent work still shows up on the reports page. Non-fatal.
    """
    try:
        artifacts = await repo.aget_artifacts(spec.task_id)
    except Exception as e:
        logger.warning(f"partial report: artifact lookup failed for {spec.task_id}: {e}")
        return
    if not artifacts:
        return
    done_count = sum(1 for _, c in artifacts if not c.startswith("[错误]"))
    parts = [
        "# AI Berkshire 投研报告（部分结果）\n",
        f"> ⚠️ 本报告为部分结果（任务{reason}时已完成的 {done_count} 个 Agent 成果）\n",
        f"**目标**：{spec.arguments}",
        f"**框架**：{spec.skill_name}\n",
    ]
    for name, content in artifacts:
        parts.append(f"\n---\n## {name}\n\n{content}\n")
    try:
        path = await repo.asave_report(
            spec.skill_name, spec.arguments, "\n".join(parts), partial=True,
        )
        logger.info(f"partial report saved for {spec.task_id}: {path}")
    except Exception as e:
        logger.warning(f"partial report save failed for {spec.task_id}: {e}")


# ==================== public run() ====================

async def run(spec: TaskSpec, bus: Optional[EventBus] = None) -> RunResult:
    """Execute a TaskSpec; all progress flows through the EventBus.

    Persists task state transitions + billing to the Repository. Cancellation
    emits the legacy {"type":"error","cancelled":true} plus the additive
    {"type":"cancelled"} event.

    A global semaphore (MAX_CONCURRENT_RUNS) caps simultaneously running
    tasks; when full, the run is rejected immediately with a {"code":"busy"}
    error event and status="error" — it is never registered as running and
    never billed. Cancelled/failed runs that produced agent artifacts but no
    final report get a partial report persisted from those artifacts.
    """
    bus = bus or get_bus()
    repo = get_repository()

    sem = _run_semaphore()
    if sem.locked():
        busy_msg = "服务器繁忙，当前研究任务已满，请稍后再试"
        bus.emit(spec.task_id, EventType.ERROR, code="busy", message=busy_msg)
        return RunResult(task_id=spec.task_id, status="error", error=busy_msg)
    # No await between the locked() check and here, so this never blocks.
    await sem.acquire()

    try:
        status = ResearchStatus(
            task_id=spec.task_id,
            skill_name=spec.skill_name,
            status="running",
            agents=[AgentProgress(agent_name=n, status="pending") for n in spec.agent_names],
        )
        await _register(status, spec.arguments, repo)
        _jobs[spec.task_id] = asyncio.current_task()
        set_usage_context(spec.task_id)
        start_ts = time()
        report_saved = False

        bus.emit(
            spec.task_id, EventType.STARTED,
            skill=spec.skill_name, strategy=spec.strategy,
            agents=list(spec.agent_names),
        )

        try:
            for a in status.agents:
                a.status = "running"
            await _persist_active(spec.task_id, spec.arguments, repo)

            report = await _execute(spec, bus)

            # OutputGuard: four quality gates before persistence
            from .guards import OutputGuard
            guard = OutputGuard(bus=bus, task_id=spec.task_id)
            report, guard_warnings = guard.check(report, spec.skill_name, spec.strategy)

            duration = time() - start_ts
            tokens = get_usage(spec.task_id)
            billing = await repo.asave_billing(
                spec.task_id, tokens, duration,
                guard_warnings=guard_warnings, model=spec.llm.model,
            )
            usage = {
                "prompt_tokens": billing["tokens_prompt"],
                "completion_tokens": billing["tokens_completion"],
                "cost_yuan": billing["cost_yuan"],
            }
            report_path = await repo.asave_report(
                spec.skill_name, spec.arguments, report, duration, tokens,
            )
            report_saved = True

            status.status = "completed"
            status.report = report
            for a in status.agents:
                a.status = "completed"
                a.progress = 1.0
            await _persist_active(spec.task_id, spec.arguments, repo)

            bus.emit(
                spec.task_id, EventType.COMPLETE,
                report=report,
                report_path=str(report_path),
                duration_seconds=round(duration, 1),
                tokens=tokens,
                usage=usage,
                guard_warnings=len(guard_warnings),
            )
            return RunResult(
                task_id=spec.task_id, status="completed", report=report,
                report_path=str(report_path),
                duration_seconds=round(duration, 1),
                tokens=tokens, usage=usage,
            )

        except asyncio.CancelledError:
            status.status = "cancelled"
            status.error = "已被用户取消"
            await _persist_active(spec.task_id, spec.arguments, repo)
            if not report_saved:
                await _save_partial_report(spec, repo, "取消")
            # Legacy-compatible cancel frame + additive explicit type
            bus.emit(spec.task_id, EventType.ERROR, message="已被用户取消", cancelled=True)
            bus.emit(spec.task_id, EventType.CANCELLED)
            return RunResult(task_id=spec.task_id, status="cancelled", error="已被用户取消")

        except Exception as e:
            status.status = "failed"
            status.error = str(e)
            await _persist_active(spec.task_id, spec.arguments, repo)
            if not report_saved:
                await _save_partial_report(spec, repo, "失败")
            bus.emit(spec.task_id, EventType.ERROR, message=str(e))
            return RunResult(task_id=spec.task_id, status="failed", error=str(e))

        finally:
            _jobs.pop(spec.task_id, None)
            clear_usage(spec.task_id)
            asyncio.create_task(_forget_later(spec.task_id))
    finally:
        sem.release()
