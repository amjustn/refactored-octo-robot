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

from ..core.config import LLM_MODEL, MAX_CONCURRENT_RUNS
from ..core.llm import chat_complete, clear_usage, get_usage, set_usage_context
from ..models.schemas import AgentProgress, ResearchStatus
from .events import EventBus, EventType, get_bus
from .persist import Repository, get_repository, sanitize_llm_config_for_storage
from .spec import TaskSpec

logger = logging.getLogger("ai_berkshire.harness")

_MAX_TASKS = 100
_FORGET_DELAY_S = 3600

# Active task state (replaces main.py's active_tasks / running_jobs)
_active: dict[str, ResearchStatus] = {}
_jobs: dict[str, asyncio.Task] = {}
# task_id -> arguments（ResearchStatus 不带 arguments，服务端去重需要它）
_active_args: dict[str, str] = {}

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

async def _register(status: ResearchStatus, arguments: str, repo: Repository,
                    llm_config: Optional[dict] = None):
    if len(_active) >= _MAX_TASKS:
        oldest_id = next(iter(_active))
        _active.pop(oldest_id, None)
    _active[status.task_id] = status
    _active_args[status.task_id] = arguments or ""
    # llm_config 已脱敏（无 api_key），仅首次 INSERT 落库，供 resume 沿用
    await repo.asave_task(status, arguments, sanitize_llm_config_for_storage(llm_config))


async def _persist_active(task_id: str, arguments: str, repo: Repository):
    if task_id in _active:
        await repo.asave_task(_active[task_id], arguments)


async def _forget_later(task_id: str, delay: int = _FORGET_DELAY_S):
    await asyncio.sleep(delay)
    _active.pop(task_id, None)
    _active_args.pop(task_id, None)


def find_running_task(skill_name: str, arguments: str) -> Optional[str]:
    """按 skill+arguments 在活跃注册表里找 running 任务（服务端去重用）。

    命中说明同参任务正在跑，调用方应返回既有 task_id 而非新起任务。
    """
    for task_id, status in _active.items():
        if status.status != "running":
            continue
        if status.skill_name != skill_name:
            continue
        if _active_args.get(task_id, "") == (arguments or ""):
            return task_id
    return None


def active_count() -> int:
    return len(_active)


def is_active(task_id: str) -> bool:
    """True while the task is registered as running in the active registry.

    Used by the WS watch path (reconnect resume): only tasks still in the
    registry will ever emit more events on the bus.
    """
    status = _active.get(task_id)
    return bool(status) and status.status == "running"


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


# ==================== report summary ====================

_SUMMARY_LLM_TIMEOUT_S = 15
_SUMMARY_MAX_CHARS = 100
_SUMMARY_SYSTEM_PROMPT = "用一句不超过60字的中文概括这份投研报告的核心结论。只输出这句话。"


def _extract_summary(report: str) -> str:
    """Deterministic summary: first heading + first non-empty paragraph line."""
    heading = ""
    paragraph = ""
    for line in report.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if not heading:
                heading = s.lstrip("#").strip()
            continue
        if s.startswith(">"):
            continue
        paragraph = s
        break
    if heading and paragraph:
        text = f"{heading}：{paragraph}"
    else:
        text = heading or paragraph
    return text[:_SUMMARY_MAX_CHARS]


async def _summarize_report(report: str, spec: TaskSpec) -> str:
    """One-line conclusion for the report meta.

    LLM polish over the deterministic extract; any failure/timeout falls
    back to the extract so report saving is never blocked.
    """
    fallback = _extract_summary(report)
    try:
        out = await asyncio.wait_for(
            chat_complete(
                _SUMMARY_SYSTEM_PROMPT,
                report[:3000],
                temperature=0.3,
                llm_config=spec.llm.to_llm_config_dict(),
            ),
            timeout=_SUMMARY_LLM_TIMEOUT_S,
        )
        first_line = (out or "").strip().splitlines()[0].strip().strip('"').strip() if (out or "").strip() else ""
        return first_line[:_SUMMARY_MAX_CHARS] or fallback
    except Exception as e:
        logger.info(f"report summary LLM polish failed for {spec.task_id}, using extract: {e}")
        return fallback


# ==================== execution ====================

async def _execute(spec: TaskSpec, bus: EventBus, prefill_results: Optional[dict] = None) -> tuple[str, list]:
    """Dispatch to AgentRunner (single / multi / series unified).

    Returns (report, extra_warnings) — extra_warnings carries llm_fallback
    events raised when a custom model couldn't function-call and an agent
    was retried with the server default model.
    prefill_results（断点续跑）：成功 artifact 直接复用，不重跑对应 Agent。
    """
    from .runner import AgentRunner, AllAgentsFailedError

    runner = AgentRunner(bus=bus, task_id=spec.task_id)
    report = await runner.run(spec, prefill_results=prefill_results)
    if runner.fatal_error:
        # E2: every research agent failed — surface as a hard failure so the
        # task is marked failed.  The generic exception path still persists
        # a partial report built from the error artifacts.
        raise AllAgentsFailedError(runner.fatal_error)
    return report, runner.extra_warnings


async def _save_partial_report(spec: TaskSpec, repo: Repository, reason: str,
                               overwrite_name: Optional[str] = None):
    """Persist completed agent artifacts as a partial report.

    Called on cancelled/failed runs that never produced a final report so
    finished agent work still shows up on the reports page. Non-fatal.
    overwrite_name（续跑再次失败时）覆盖同一份 partial 报告，避免历史列表堆积。
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
            task_id=spec.task_id, overwrite_name=overwrite_name,
        )
        logger.info(f"partial report saved for {spec.task_id}: {path}")
    except Exception as e:
        logger.warning(f"partial report save failed for {spec.task_id}: {e}")


# ==================== public run() ====================

async def run(
    spec: TaskSpec,
    bus: Optional[EventBus] = None,
    prefill_results: Optional[dict] = None,
    report_overwrite: Optional[str] = None,
    billing_accumulate: bool = False,
) -> RunResult:
    """Execute a TaskSpec; all progress flows through the EventBus.

    Persists task state transitions + billing to the Repository. Cancellation
    emits the legacy {"type":"error","cancelled":true} plus the additive
    {"type":"cancelled"} event.

    A global semaphore (MAX_CONCURRENT_RUNS) caps simultaneously running
    tasks; when full, the run is rejected immediately with a {"code":"busy"}
    error event and status="error" — it is never registered as running and
    never billed. Cancelled/failed runs that produced agent artifacts but no
    final report get a partial report persisted from those artifacts.

    prefill_results（断点续跑）：agent_name → 上次成功 artifact，跳过重跑。
    report_overwrite：成功后将报告写回该文件名（替换 partial 报告）。
    billing_accumulate：本次 token/费用/耗时累加到既有任务记录而非覆盖。
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
        # resume 双跑防护：同 task_id 已在跑则拒绝注册。
        # 检查与 _register 的占位之间无 await，asyncio 下是原子的。
        existing = _active.get(spec.task_id)
        if existing is not None and existing.status == "running":
            dup_msg = "任务已在运行中，请勿重复启动"
            bus.emit(spec.task_id, EventType.ERROR, code="duplicate", message=dup_msg)
            return RunResult(task_id=spec.task_id, status="error", error=dup_msg)
        await _register(status, spec.arguments, repo, llm_config=spec.llm.to_storage_dict())
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

            report, extra_warnings = await _execute(spec, bus, prefill_results=prefill_results)

            # OutputGuard: four quality gates before persistence
            from .guards import OutputGuard
            guard = OutputGuard(bus=bus, task_id=spec.task_id)
            report, guard_warnings = guard.check(report, spec.skill_name, spec.strategy)
            if guard.fatals:
                # 第3层闸门：报告残留未执行的工具调用标记 → 硬失败。
                # 先把破损报告存为 partial（可审计、可对比），再复用 E2 的
                # AllAgentsFailedError 路径走通用异常分支：任务标 failed、
                # 发 error 事件，历史列表出现「⟳ 继续任务」入口。
                from .runner import AllAgentsFailedError
                fatal_detail = "；".join(f["detail"] for f in guard.fatals)
                logger.error(f"fatal guard hit for {spec.task_id}: {fatal_detail}")
                try:
                    await repo.asave_report(
                        spec.skill_name, spec.arguments, report, partial=True,
                        task_id=spec.task_id, overwrite_name=report_overwrite,
                    )
                except Exception as pe:
                    logger.warning(f"fatal-gate partial save failed for {spec.task_id}: {pe}")
                raise AllAgentsFailedError(fatal_detail)
            if extra_warnings:
                guard_warnings.extend(extra_warnings)
                notes = "；".join(w["detail"] for w in extra_warnings[:2])
                report = f"> ⚠️ 模型告警：{notes}\n\n" + report

            # One-line conclusion for the reports list (before get_usage so
            # its tokens flow into the existing usage accounting).
            summary = await _summarize_report(report, spec)

            duration = time() - start_ts
            tokens = get_usage(spec.task_id)
            billing = await repo.asave_billing(
                spec.task_id, tokens, duration,
                guard_warnings=guard_warnings,
                # Effective primary model: user override or server default,
                # so the cost dashboard can break stats down by model.
                model=spec.llm.model or LLM_MODEL,
                accumulate=billing_accumulate,
            )
            usage = {
                "prompt_tokens": billing["tokens_prompt"],
                "completion_tokens": billing["tokens_completion"],
                "cost_yuan": billing["cost_yuan"],
                "by_model": billing.get("by_model", []),
                "priced": billing.get("priced", True),
            }
            report_path = await repo.asave_report(
                spec.skill_name, spec.arguments, report, duration, tokens,
                summary=summary,
                task_id=spec.task_id, overwrite_name=report_overwrite,
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
            # 取消也计费：已消耗的 partial tokens 落库（guard 未运行，warnings 为空）。
            # clear_usage 在 finally 中照常执行。
            try:
                await repo.asave_billing(
                    spec.task_id, get_usage(spec.task_id), time() - start_ts,
                    guard_warnings=[],
                    model=spec.llm.model or LLM_MODEL,
                    accumulate=billing_accumulate,
                )
            except Exception as be:
                logger.warning(f"billing on cancel failed for {spec.task_id}: {be}")
            if not report_saved:
                await _save_partial_report(spec, repo, "取消", overwrite_name=report_overwrite)
            # Legacy-compatible cancel frame + additive explicit type
            bus.emit(spec.task_id, EventType.ERROR, message="已被用户取消", cancelled=True)
            bus.emit(spec.task_id, EventType.CANCELLED)
            return RunResult(task_id=spec.task_id, status="cancelled", error="已被用户取消")

        except Exception as e:
            status.status = "failed"
            status.error = str(e)
            await _persist_active(spec.task_id, spec.arguments, repo)
            if not report_saved:
                await _save_partial_report(spec, repo, "失败", overwrite_name=report_overwrite)
            bus.emit(spec.task_id, EventType.ERROR, message=str(e))
            return RunResult(task_id=spec.task_id, status="failed", error=str(e))

        finally:
            _jobs.pop(spec.task_id, None)
            clear_usage(spec.task_id)
            asyncio.create_task(_forget_later(spec.task_id))
    finally:
        sem.release()


# ==================== resume (断点续跑) ====================

async def prepare_resume(task_id: str, llm_override: Optional[dict] = None) -> tuple:
    """Validate a task for resume and assemble the execution plan.

    Returns (plan, error). plan keys:
      spec             — rebuilt TaskSpec with the SAME task_id
      prefill_results  — agent_name → prior good artifact (skipped agents)
      skipped_agents   — roster order, for UI display
      rerun_agents     — agents that will actually execute
      report_overwrite — prior partial report filename to replace, or None
    error is a user-facing message when resume is not possible (caller then
    falls back to a full re-run).
    """
    repo = get_repository()
    brief = await asyncio.to_thread(repo.get_task_brief, task_id)
    if not brief:
        return None, "任务不存在"
    if brief["status"] == "running" or is_active(task_id):
        return None, "任务仍在运行中"

    # 原任务的 per-agent LLM 配置（落库时已脱敏，无 api_key）：请求未带
    # override 时默认沿用；请求 override 优先（调用方已做校验清洗）。
    if llm_override is None:
        import json as _json
        try:
            stored = _json.loads(brief.get("llm_config_json") or "{}")
        except Exception:
            stored = {}
        llm_override = stored or None

    from ..skills import get_skill
    skill = get_skill(brief["skill_name"])
    if not skill:
        return None, f"技能 {brief['skill_name']} 已不存在"
    if not skill.get("is_multi_agent"):
        return None, "该技能为单Agent/系列模式，不支持断点续跑，请整局重跑"

    artifacts = await repo.aget_artifacts(task_id)
    latest: dict[str, str] = {}
    for name, content in artifacts:
        latest[name] = content  # append-only log — last write wins

    post = set(skill.get("post_synthesis_agents", []))
    roster = [a for a in skill.get("agents", []) if a not in post]
    prefill = {n: c for n, c in latest.items() if n in roster and not c.startswith("[错误]")}
    if not prefill:
        return None, "没有可复用的 Agent 成果，请整局重跑"

    spec = TaskSpec.build(
        skill, brief["arguments"], llm_override=llm_override,
        caller="http", stream=True, task_id=task_id,
    )
    overwrite = await asyncio.to_thread(
        repo.find_report_name_for_task, task_id, brief["skill_name"], brief["arguments"]
    )
    plan = {
        "spec": spec,
        "prefill_results": prefill,
        "skipped_agents": [n for n in roster if n in prefill],
        "rerun_agents": [n for n in roster if n not in prefill],
        "report_overwrite": overwrite,
    }
    return plan, None


async def resume(task_id: str, llm_override: Optional[dict] = None,
                 bus: Optional[EventBus] = None) -> RunResult:
    """Resume a terminal multi-agent task: reuse good artifacts, rerun the rest.

    Team Lead synthesis and post-synthesis agents always rerun; billing is
    accumulated onto the existing task row; a successful run overwrites the
    partial report file in place.
    """
    plan, error = await prepare_resume(task_id, llm_override)
    if error:
        return RunResult(task_id=task_id, status="error", error=error)
    return await run(
        plan["spec"], bus=bus,
        prefill_results=plan["prefill_results"],
        report_overwrite=plan["report_overwrite"],
        billing_accumulate=True,
    )
