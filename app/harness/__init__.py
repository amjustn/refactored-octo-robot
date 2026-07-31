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


@dataclass
class RunResult:
    task_id: str
    status: str                      # completed / failed / cancelled
    report: str = ""
    report_path: str = ""
    duration_seconds: float = 0.0
    tokens: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)   # tokens + cost_yuan
    error: str = ""


# ==================== task registry ====================

def _register(status: ResearchStatus, arguments: str, repo: Repository):
    if len(_active) >= _MAX_TASKS:
        oldest_id = next(iter(_active))
        _active.pop(oldest_id, None)
    _active[status.task_id] = status
    repo.save_task(status, arguments)


def _persist_active(task_id: str, arguments: str, repo: Repository):
    if task_id in _active:
        repo.save_task(_active[task_id], arguments)


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


# ==================== execution (PR-1: delegates to orchestrator) ====================

async def _execute(spec: TaskSpec, bus: EventBus) -> str:
    """PR-1 executor — delegates to the existing orchestrator functions.

    PR-2 replaces this with harness.runner.AgentRunner; the event contract
    stays identical.
    """
    from ..agents.orchestrator import (
        run_single_agent, run_multi_agent, run_series,
    )

    llm_config = spec.llm.to_llm_config_dict()

    async def _progress(p: AgentProgress):
        bus.emit(
            spec.task_id, EventType.PROGRESS,
            agent=p.agent_name, status=p.status, message=p.message,
            progress=p.progress,
        )

    if spec.strategy == "series":
        return await run_series(
            spec.skill_name, spec.arguments,
            progress_callback=_progress, llm_config=llm_config,
        )
    if spec.strategy == "multi":
        return await run_multi_agent(
            spec.skill_name, spec.arguments,
            progress_callback=_progress, llm_config=llm_config,
        )

    # single strategy
    if not spec.stream:
        return await run_single_agent(
            spec.skill_name, spec.arguments, llm_config=llm_config,
        )

    # Streaming single-agent branch (moved verbatim from main.py WS handler)
    from ..skills import build_user_prompt
    from ..core.llm import chat_stream
    from ..core.context import build_full_context

    try:
        context = await build_full_context(spec.arguments, skill_name=spec.skill_name)
    except Exception:
        context = ""

    user_msg = build_user_prompt(spec.skill_name, spec.arguments)
    system_msg = "你是一位精通价值投资的分析师，遵循巴菲特、芒格、段永平、李录四位大师的方法论。"
    if context:
        system_msg += f"\n\n{context}"

    full_report = ""
    async for chunk in chat_stream(system_msg, user_msg, llm_config=llm_config):
        full_report += chunk
        bus.emit(spec.task_id, EventType.CHUNK, content=chunk)
    return full_report


# ==================== public run() ====================

async def run(spec: TaskSpec, bus: Optional[EventBus] = None) -> RunResult:
    """Execute a TaskSpec; all progress flows through the EventBus.

    Persists task state transitions + billing to the Repository. Cancellation
    emits the legacy {"type":"error","cancelled":true} plus the additive
    {"type":"cancelled"} event.
    """
    bus = bus or get_bus()
    repo = get_repository()

    status = ResearchStatus(
        task_id=spec.task_id,
        skill_name=spec.skill_name,
        status="running",
        agents=[AgentProgress(agent_name=n, status="pending") for n in spec.agent_names],
    )
    _register(status, spec.arguments, repo)
    _jobs[spec.task_id] = asyncio.current_task()
    set_usage_context(spec.task_id)
    start_ts = time()

    bus.emit(
        spec.task_id, EventType.STARTED,
        skill=spec.skill_name, strategy=spec.strategy,
        agents=list(spec.agent_names),
    )

    try:
        for a in status.agents:
            a.status = "running"
        _persist_active(spec.task_id, spec.arguments, repo)

        report = await _execute(spec, bus)

        duration = time() - start_ts
        tokens = get_usage(spec.task_id)
        billing = repo.save_billing(
            spec.task_id, tokens, duration, model=spec.llm.model,
        )
        usage = {
            "prompt_tokens": billing["tokens_prompt"],
            "completion_tokens": billing["tokens_completion"],
            "cost_yuan": billing["cost_yuan"],
        }
        report_path = repo.save_report(
            spec.skill_name, spec.arguments, report, duration, tokens,
        )

        status.status = "completed"
        status.report = report
        for a in status.agents:
            a.status = "completed"
            a.progress = 1.0
        _persist_active(spec.task_id, spec.arguments, repo)

        bus.emit(
            spec.task_id, EventType.COMPLETE,
            report=report,
            report_path=str(report_path),
            duration_seconds=round(duration, 1),
            tokens=tokens,
            usage=usage,
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
        _persist_active(spec.task_id, spec.arguments, repo)
        # Legacy-compatible cancel frame + additive explicit type
        bus.emit(spec.task_id, EventType.ERROR, message="已被用户取消", cancelled=True)
        bus.emit(spec.task_id, EventType.CANCELLED)
        return RunResult(task_id=spec.task_id, status="cancelled", error="已被用户取消")

    except Exception as e:
        status.status = "failed"
        status.error = str(e)
        _persist_active(spec.task_id, spec.arguments, repo)
        bus.emit(spec.task_id, EventType.ERROR, message=str(e))
        return RunResult(task_id=spec.task_id, status="failed", error=str(e))

    finally:
        _jobs.pop(spec.task_id, None)
        clear_usage(spec.task_id)
        asyncio.create_task(_forget_later(spec.task_id))
