"""Harness public API — the only entry points main.py (and eval) may use.

    run(spec)        -> execute a TaskSpec, emitting events on the EventBus
    cancel(task_id)  -> server-side cancellation of a running task
    get_status(...)  -> active-task lookup with DB fallback

    HITL 人工确认门（multi 辩论后，默认关闭）：
    register_confirmation / await_confirmation（runner 用）
    resolve_confirmation / pending_confirmation（REST / WS 层用）

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

# ── HITL 人工确认门注册表 ─────────────────────────────────────
# 仅在 spec.require_debate_confirm=True 的 multi 任务辩论完成后使用：
# runner 注册并等待；REST 端点决议；WS 重连时重放 awaiting_confirmation 帧。
CONFIRM_TIMEOUT_S = 300  # 等待上限；超时自动放行（测试可 monkeypatch 调小）
_confirm_events: dict[str, asyncio.Event] = {}
_confirm_payloads: dict[str, dict] = {}   # task_id -> 完整 WS 帧（重放用）
_confirm_results: dict[str, dict] = {}    # task_id -> {"action", "note"}

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
    # 兜底清理确认门残留（正常路径下 await_confirmation 已自行清理）
    _confirm_events.pop(task_id, None)
    _confirm_payloads.pop(task_id, None)
    _confirm_results.pop(task_id, None)


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


# ==================== HITL 人工确认门 ====================

def register_confirmation(task_id: str, payload: dict) -> None:
    """注册一个人工确认门（runner 在发 awaiting_confirmation 前调用）。

    payload 为事件业务字段（gate/votes/reviews/timeout_s）；注册表另存
    完整 WS 帧，供断线重连订阅时原样重放。
    """
    _confirm_events[task_id] = asyncio.Event()
    _confirm_payloads[task_id] = {
        "type": str(EventType.AWAITING_CONFIRMATION),
        "task_id": task_id,
        **payload,
    }
    _confirm_results.pop(task_id, None)


def pending_confirmation(task_id: str) -> Optional[dict]:
    """该任务当前等待中的确认门 WS 帧（无则 None）。WS 重连重放用。"""
    return _confirm_payloads.get(task_id)


def resolve_confirmation(task_id: str, action: str, note: str = "") -> bool:
    """人工确认门决议入口（REST 调用）。返回 False 表示无等待中的门。

    approve/modify：置位 Event 放行（modify 的 note 由 runner 注入综合
    prompt）；reject：先记决议，再走标准取消路径（job.cancel()）——
    run() 的 CancelledError 分支统一完成计费、partial 报告与 cancelled
    事件，与手动取消完全一致；无 job（runner 独立使用）时仅置位 Event，
    由确认门等待方自行抛出 CancelledError。
    """
    ev = _confirm_events.get(task_id)
    if ev is None or ev.is_set():
        return False
    _confirm_results[task_id] = {"action": action, "note": (note or "")[:2000]}
    if action == "reject":
        job = _jobs.get(task_id)
        if job is not None:
            # CancelledError 在等待点抛出 → run() 取消分支（计费/partial/事件）
            job.cancel()
    ev.set()
    return True


async def await_confirmation(task_id: str, timeout: Optional[float] = None) -> Optional[dict]:
    """等待确认门决议。返回 {"action", "note"}；超时返回 None（调用方放行）。

    任务被取消（含 reject 触发的 job.cancel()）时 CancelledError 正常向上
    传播，不吞不改。无论结果如何，等待结束即清理注册表——决议后/超时后
    WS 重连不再重放该门。
    """
    ev = _confirm_events.get(task_id)
    if ev is None:
        return None
    result: Optional[dict] = None
    try:
        await asyncio.wait_for(
            ev.wait(), timeout if timeout is not None else CONFIRM_TIMEOUT_S
        )
        result = _confirm_results.get(task_id) or {"action": "approve", "note": ""}
    except asyncio.TimeoutError:
        result = None
    finally:
        _confirm_events.pop(task_id, None)
        _confirm_payloads.pop(task_id, None)
        _confirm_results.pop(task_id, None)
    return result


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

async def _execute(spec: TaskSpec, bus: EventBus, prefill_results: Optional[dict] = None,
                   prefill_context: Optional[str] = None,
                   prefill_goal_hints: Optional[dict] = None,
                   prefill_articles: Optional[list] = None,
                   prefill_debate: Optional[str] = None) -> tuple[str, list]:
    """Dispatch to AgentRunner (single / multi / series unified).

    Returns (report, extra_warnings) — extra_warnings carries llm_fallback
    events raised when a custom model couldn't function-call and an agent
    was retried with the server default model.
    prefill_results（断点续跑）：成功 artifact 直接复用，不重跑对应 Agent。
    其余 prefill_* 为 checkpoint 级续跑参数，原样透传给 AgentRunner。
    """
    from .runner import AgentRunner, AllAgentsFailedError

    runner = AgentRunner(bus=bus, task_id=spec.task_id)
    report = await runner.run(
        spec, prefill_results=prefill_results,
        prefill_context=prefill_context, prefill_goal_hints=prefill_goal_hints,
        prefill_articles=prefill_articles, prefill_debate=prefill_debate,
    )
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
    prefill_context: Optional[str] = None,
    prefill_goal_hints: Optional[dict] = None,
    prefill_articles: Optional[list] = None,
    prefill_debate: Optional[str] = None,
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
    prefill_context / prefill_goal_hints（single 续跑）：复用上次构建的
    上下文与 goal parser 结果；prefill_articles（series 续跑）：已完成
    单篇 [(topic, article)]；prefill_debate（multi 续跑）：上次交叉辩论
    结论。均只在对应策略下生效，默认 None 时行为与旧版一致。
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

            report, extra_warnings = await _execute(
                spec, bus, prefill_results=prefill_results,
                prefill_context=prefill_context, prefill_goal_hints=prefill_goal_hints,
                prefill_articles=prefill_articles, prefill_debate=prefill_debate,
            )

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
                # 批D(2026-09-11): 把运行时的模型版本与降级告警一起写进报告 meta，
                # 让「历史报告」自己说明它是怎么跑出来的（此前只存在 tasks 表里）。
                model=(spec.llm.model or LLM_MODEL),
                guard_warnings=guard_warnings,
                data_status={
                    "state": "degraded" if guard_warnings else "ok",
                    "gates": sorted({str(w.get("gate") or "") for w in guard_warnings
                                     if isinstance(w, dict) and w.get("gate")}),
                },
            )
            report_saved = True

            status.status = "completed"
            status.report = report
            for a in status.agents:
                a.status = "completed"
                a.progress = 1.0
            await _persist_active(spec.task_id, spec.arguments, repo)
            # 断点续跑：任务成功完成后清空检查点（失败/取消保留供续跑）
            try:
                await repo.aclear_checkpoints(spec.task_id)
            except Exception as ce:
                logger.warning(f"clear checkpoints failed for {spec.task_id}: {ce}")

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

async def prepare_resume(task_id: str, llm_override: Optional[dict] = None,
                         debate_confirm: bool = False) -> tuple:
    """Validate a task for resume and assemble the execution plan.

    Returns (plan, error). plan keys:
      spec             — rebuilt TaskSpec with the SAME task_id
      prefill_results  — agent_name → prior good artifact（multi 跳过的 Agent）
      prefill_debate   — 上次交叉辩论结论 checkpoint（multi，可空）
      prefill_articles — [(topic, article)] 已完成系列单篇（series，可空）
      prefill_context  — 上次构建的上下文（single，可空）
      prefill_goal_hints — 上次 goal parser 结果（single，可空）
      skipped_agents   — roster order, for UI display
      rerun_agents     — agents that will actually execute
      report_overwrite — prior partial report filename to replace, or None
    error is a user-facing message when resume is not possible (caller then
    falls back to a full re-run).

    debate_confirm：续跑任务同样启用辩论后人工确认门（HITL）；默认关闭，
    与整局重跑的同名开关语义一致。

    检查点优先：multi 的辩论结论、series 的单篇正文、single 的上下文
    先读 task_checkpoints；multi 研究 Agent 成果仍复用 task_artifacts
    （runner 对该层本就打 artifact，不重复存 checkpoint）。
    """
    import json as _json

    repo = get_repository()
    brief = await asyncio.to_thread(repo.get_task_brief, task_id)
    if not brief:
        return None, "任务不存在"
    if brief["status"] == "running" or is_active(task_id):
        return None, "任务仍在运行中"

    # 原任务的 per-agent LLM 配置（落库时已脱敏，无 api_key）：请求未带
    # override 时默认沿用；请求 override 优先（调用方已做校验清洗）。
    if llm_override is None:
        try:
            stored = _json.loads(brief.get("llm_config_json") or "{}")
        except Exception:
            stored = {}
        llm_override = stored or None

    from ..skills import get_skill
    skill = get_skill(brief["skill_name"])
    if not skill:
        return None, f"技能 {brief['skill_name']} 已不存在"

    # 读取阶段检查点（进程重启前的中间产出）；读失败不阻断续跑判定
    try:
        checkpoints = await repo.aload_checkpoints(task_id)
    except Exception as e:
        logger.warning(f"load checkpoints failed for {task_id}: {e}")
        checkpoints = []
    latest_ckpt: dict[str, dict] = {}
    for row in checkpoints:
        latest_ckpt[row.get("phase") or ""] = row  # append-only — 后写覆盖先写

    overwrite = await asyncio.to_thread(
        repo.find_report_name_for_task, task_id, brief["skill_name"], brief["arguments"]
    )

    def _plan(spec, prefill_results=None, skipped=None, rerun=None, **extra):
        plan = {
            "spec": spec,
            "prefill_results": prefill_results or {},
            "prefill_debate": None,
            "prefill_articles": None,
            "prefill_context": None,
            "prefill_goal_hints": None,
            "skipped_agents": skipped or [],
            "rerun_agents": rerun or [],
            "report_overwrite": overwrite,
        }
        plan.update(extra)
        return plan

    def _build_spec():
        return TaskSpec.build(
            skill, brief["arguments"], llm_override=llm_override,
            caller="http", stream=True, task_id=task_id,
            debate_confirm=debate_confirm,
        )

    # ── series：从最后一个完成的 episode 之后继续，前文从 checkpoint 恢复 ──
    if skill.get("series_mode"):
        topics = skill.get("series_topics", [])
        done: dict[int, tuple] = {}
        # 优先读 checkpoints（phase=series_episode_N，content 为
        # {"topic","article","prev_summaries"} JSON；前文摘要由 runner
        # 续跑时按既有 articles 重建，这里只取正文）
        for row in checkpoints:
            phase = row.get("phase") or ""
            if not phase.startswith("series_episode_"):
                continue
            try:
                idx = int(phase.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                continue
            content = row.get("content") or ""
            topic = row.get("agent") or ""
            article = content
            try:
                payload = _json.loads(content)
                if isinstance(payload, dict) and payload.get("article"):
                    article = payload["article"]
                    topic = payload.get("topic") or topic
            except Exception:
                pass  # 非 JSON（手写/旧格式）：content 即正文
            if article and not article.startswith("[错误]"):
                done[idx] = (topic, article)
        # fallback：无 episode checkpoint 时退到 artifacts（agent_name=topic）
        if not done:
            artifacts = await repo.aget_artifacts(task_id)
            latest_art: dict[str, str] = {}
            for name, content in artifacts:
                latest_art[name] = content
            for i, topic in enumerate(topics, start=1):
                c = latest_art.get(topic)
                if c and not c.startswith("[错误]"):
                    done[i] = (topic, c)
        # 前文必须连续：断档后的篇目一律重跑，保证「前文回顾」衔接
        prefill_articles = []
        skipped = []
        for i in range(1, len(topics) + 1):
            if i not in done:
                break
            prefill_articles.append(done[i])
            skipped.append(f"article-{i}")
        if not prefill_articles:
            return None, "没有可复用的系列文章成果，请整局重跑"
        return _plan(
            _build_spec(), skipped=skipped,
            rerun=[f"article-{i}" for i in range(len(skipped) + 1, len(topics) + 1)],
            prefill_articles=prefill_articles,
        ), None

    # ── multi：研究 Agent 成果复用 artifacts；辩论结论优先读 checkpoints ──
    if skill.get("is_multi_agent"):
        artifacts = await repo.aget_artifacts(task_id)
        latest: dict[str, str] = {}
        for name, content in artifacts:
            latest[name] = content  # append-only log — last write wins

        post = set(skill.get("post_synthesis_agents", []))
        roster = [a for a in skill.get("agents", []) if a not in post]
        prefill = {n: c for n, c in latest.items() if n in roster and not c.startswith("[错误]")}
        debate_ckpt = latest_ckpt.get("debate")
        if not prefill and debate_ckpt is None:
            return None, "没有可复用的 Agent 成果，请整局重跑"

        return _plan(
            _build_spec(), prefill_results=prefill,
            skipped=[n for n in roster if n in prefill],
            rerun=[n for n in roster if n not in prefill],
            prefill_debate=(debate_ckpt.get("content") if debate_ckpt else None),
        ), None

    # ── single：恢复上下文构建结果（含 goal parser 摘要），跳过 goal parser ──
    single_ckpt = latest_ckpt.get("single")
    prefill_context, prefill_goal_hints = "", None
    if single_ckpt and (single_ckpt.get("content") or ""):
        raw = single_ckpt["content"]
        try:
            payload = _json.loads(raw)
            if isinstance(payload, dict):
                prefill_context = payload.get("context") or ""
                prefill_goal_hints = payload.get("goal_hints") or None
        except Exception:
            prefill_context = raw  # 非 JSON：整段当作上下文
    if not prefill_context:
        return None, "没有可复用的上下文检查点，请整局重跑"
    spec = _build_spec()
    return _plan(
        spec, skipped=[], rerun=list(spec.agent_names),
        prefill_context=prefill_context, prefill_goal_hints=prefill_goal_hints,
    ), None


async def resume(task_id: str, llm_override: Optional[dict] = None,
                 bus: Optional[EventBus] = None,
                 debate_confirm: bool = False) -> RunResult:
    """Resume a terminal task: reuse checkpoints/artifacts, rerun the rest.

    multi 复用研究 Agent artifact + 辩论 checkpoint；series 从最后一篇
    完成的 episode 之后继续；single 复用上下文构建结果。Team Lead 综合
    与 post-synthesis agents 始终重跑；billing 累加到既有任务行；成功
    后原位覆盖 partial 报告。
    """
    plan, error = await prepare_resume(task_id, llm_override, debate_confirm=debate_confirm)
    if error:
        return RunResult(task_id=task_id, status="error", error=error)
    return await run(
        plan["spec"], bus=bus,
        prefill_results=plan["prefill_results"],
        report_overwrite=plan["report_overwrite"],
        billing_accumulate=True,
        prefill_context=plan.get("prefill_context"),
        prefill_goal_hints=plan.get("prefill_goal_hints"),
        prefill_articles=plan.get("prefill_articles"),
        prefill_debate=plan.get("prefill_debate"),
    )
