"""Research task routes: /api/research, /api/cancel, /api/task(s), WS /ws/research.

Rate-limit / JWT helpers are resolved through app.web_common at call time
(_wc.<name>) because the test-suite monkeypatches those attributes on
the app.web_common module; binding them here by value would break that.
"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from .. import web_common as _wc
from ..harness import (
    TaskSpec,
)
from ..harness import (
    cancel as harness_cancel,
)
from ..harness import (
    find_running_task as harness_find_running,
)
from ..harness import (
    get_status as harness_get_status,
)
from ..harness import (
    is_active as harness_is_active,
)
from ..harness import (
    pending_confirmation as harness_pending_confirmation,
)
from ..harness import (
    prepare_resume as harness_prepare_resume,
)
from ..harness import (
    resolve_confirmation as harness_resolve_confirmation,
)
from ..harness import (
    run as harness_run,
)
from ..harness.events import get_bus
from ..harness.persist import get_repository
from ..models.schemas import ResearchRequest
from ..skills import get_skill
from ..web_common import _validate_llm_config

router = APIRouter()


# ==================== Research API ====================

@router.post("/api/research")
async def api_start_research(request: "Request", req: ResearchRequest):
    if not _wc._check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    skill = get_skill(req.skill_name)
    if not skill:
        return {"error": f"Unknown skill: {req.skill_name}"}

    # 服务端去重：同 skill+arguments 已有 running 任务 → 409 + 既有 task_id，
    # 前端收到后只提示不重复开跑（WS 回退竞态的服务端兜底，见 app.js _batchFallback）。
    running_id = harness_find_running(req.skill_name, req.arguments)
    if running_id:
        return JSONResponse(
            status_code=409,
            content={
                "task_id": running_id,
                "status": "running",
                "error": "相同参数的研究任务正在运行中，请勿重复提交",
            },
        )

    llm_config = _validate_llm_config(req.llm_config)
    spec = TaskSpec.build(
        skill, req.arguments, llm_override=llm_config,
        caller="http", stream=False,
        attachments=req.attachments or "",
        debate_confirm=bool(getattr(req, "debate_confirm", False)),
    )
    result = await harness_run(spec)

    if result.status == "completed":
        return {
            "task_id": result.task_id,
            "status": "completed",
            "report": result.report,
            "report_path": result.report_path,
            "duration_seconds": result.duration_seconds,
            "tokens": result.tokens,
            "usage": result.usage,
        }
    if result.status == "cancelled":
        return {"task_id": result.task_id, "status": "cancelled", "error": result.error}
    return {"task_id": result.task_id, "status": "failed", "error": result.error}


@router.post("/api/cancel/{task_id}")
async def api_cancel_research(task_id: str):
    """Cancel a running research task on the server side (stops LLM calls)."""
    return harness_cancel(task_id)


@router.post("/api/task/{task_id}/confirm")
async def api_confirm_task(task_id: str, request: "Request"):
    """HITL 辩论后人工确认门决议：approve 放行 / modify 批注后放行 / reject 终止。

    JWT 由全局中间件统一把关（/api/*）；无等待中的确认门（不存在、
    已决议或已超时清理）返回 404。reject 复用标准取消路径：状态落库
    cancelled、已消耗 token 照常计费、已完成 Agent 成果存 partial 报告。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    action = str(body.get("action") or "").strip()
    note = str(body.get("note") or "").strip()[:2000]
    if action not in ("approve", "reject", "modify"):
        raise HTTPException(status_code=400, detail="action 必须是 approve / reject / modify")
    ok = harness_resolve_confirmation(task_id, action, note)
    if not ok:
        raise HTTPException(status_code=404, detail="该任务没有等待中的确认门（不存在、已处理或已超时）")
    return {"task_id": task_id, "status": "confirmed", "action": action}


@router.post("/api/tasks/{task_id}/resume")
async def api_resume_task(request: "Request", task_id: str):
    """断点续跑：复用上次成功的 Agent 成果，只重跑失败的 Agent。

    校验通过后任务在后台启动（同一 task_id），前端随后用 WS
    watch_task_id 跟随事件流。响应携带 skipped/rerun 名单供进度页即时展示。
    """
    if not _wc._check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    try:
        body = await request.json()
    except Exception:
        body = {}
    llm_config = _validate_llm_config(body.get("llm_config") if isinstance(body, dict) else None)
    # HITL：续跑任务可同样开启辩论后人工确认门
    debate_confirm = bool(body.get("debate_confirm", False)) if isinstance(body, dict) else False

    plan, error = await harness_prepare_resume(task_id, llm_override=llm_config,
                                               debate_confirm=debate_confirm)
    if error:
        raise HTTPException(status_code=400, detail=error)

    bus = get_bus()
    asyncio.create_task(
        harness_run(
            plan["spec"], bus=bus,
            prefill_results=plan["prefill_results"],
            report_overwrite=plan["report_overwrite"],
            billing_accumulate=True,
            prefill_context=plan.get("prefill_context"),
            prefill_goal_hints=plan.get("prefill_goal_hints"),
            prefill_articles=plan.get("prefill_articles"),
            prefill_debate=plan.get("prefill_debate"),
        )
    )
    return {
        "task_id": task_id,
        "status": "resumed",
        "skipped_agents": plan["skipped_agents"],
        "rerun_agents": plan["rerun_agents"],
    }

@router.websocket("/ws/research/{skill_name}")
async def ws_research(websocket: WebSocket, skill_name: str):
    await websocket.accept()

    try:
        data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
    except asyncio.TimeoutError:
        await websocket.send_json({"type": "error", "message": "等待参数超时"})
        try:
            await websocket.close()
        except Exception:
            pass
        return
    try:
        req = json.loads(data)
        arguments = req.get("arguments", "")
        attachments = (req.get("attachments", "") or "")[:30000]
        token = req.get("token", "")
        watch_task_id = req.get("watch_task_id", "") or ""
        llm_config = _validate_llm_config(req.get("llm_config"))
        # HITL：辩论后人工确认门开关（默认关闭，缺失/非真值皆为关）
        debate_confirm = bool(req.get("debate_confirm", False))
    except json.JSONDecodeError:
        arguments = data
        attachments = ""
        token = ""
        watch_task_id = ""
        llm_config = None
        debate_confirm = False

    if not _wc._ws_verify_jwt(token):
        await websocket.send_json({"type": "error", "message": "Unauthorized"})
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    # Reconnect resume: subscribe to an already-running task's event stream
    # instead of starting a new run. No LLM budget is spent, so the run rate
    # limit below does not apply.
    if watch_task_id:
        await _ws_watch_task(websocket, watch_task_id)
        return

    # Same per-IP rate limit as /api/research — WS runs cost the same LLM budget.
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not _wc._check_rate_limit(client_ip):
        await websocket.send_json({"type": "error", "message": "请求过于频繁，请稍后再试"})
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    skill = get_skill(skill_name)
    if not skill:
        await websocket.send_json({"type": "error", "message": f"Unknown skill: {skill_name}"})
        try:
            await websocket.close()
        except Exception:
            pass
        return

    spec = TaskSpec.build(
        skill, arguments, llm_override=llm_config, caller="ws", stream=True,
        attachments=attachments,
        debate_confirm=debate_confirm,
    )

    # Subscribe BEFORE run() so no events are lost; forward bus → websocket.
    bus = get_bus()
    queue = bus.subscribe(spec.task_id)
    _STOP = object()

    async def _forward():
        while True:
            event = await queue.get()
            if event is _STOP:
                break
            try:
                await websocket.send_json(event.to_ws_dict())
            except Exception:
                break

    forwarder = asyncio.create_task(_forward())
    try:
        await harness_run(spec, bus=bus)
    finally:
        bus.unsubscribe(spec.task_id, queue)
        # Flush remaining events (e.g. complete/error) before closing.
        while True:
            try:
                queue.put_nowait(_STOP)
                break
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        try:
            await asyncio.wait_for(forwarder, timeout=10.0)
        except Exception:
            forwarder.cancel()
        try:
            await websocket.close()
        except Exception:
            pass


async def _ws_watch_task(websocket: WebSocket, task_id: str):
    """Attach the socket to an already-running task's event stream.

    Reconnect-resume path: the client dropped mid-run and, instead of
    starting a new research, asks to keep receiving events for the task it
    was following. A late subscriber only sees FUTURE events — sufficient
    because the terminal ``complete`` frame carries the full report.

    - Task still active → forward events until a terminal event, the client
      disconnects, or the socket errors.
    - Task already terminal/unknown → send a synthesized terminal frame
      (complete with the stored report, else a clean error) and close, so
      the client can fall back to its restart path. No crash, no hang.
    """
    bus = get_bus()
    queue = bus.subscribe(task_id)
    _STOP = object()
    _TERMINAL = ("complete", "error", "cancelled")

    async def _forward():
        while True:
            event = await queue.get()
            if event is _STOP:
                return
            try:
                await websocket.send_json(event.to_ws_dict())
            except Exception:
                return
            if event.type in _TERMINAL:
                return

    forwarder = asyncio.create_task(_forward())
    try:
        if not harness_is_active(task_id):
            # Finished (or never existed) between the client's status check
            # and this subscribe — no more events will ever arrive.
            status = harness_get_status(task_id)
            if status is not None and status.status == "completed":
                await websocket.send_json({
                    "type": "complete",
                    "task_id": task_id,
                    "report": (status.report or "")[:_wc._TASK_REPORT_MAX_CHARS],
                    "resumed": True,
                })
            else:
                await websocket.send_json({
                    "type": "error",
                    "task_id": task_id,
                    "message": "任务不存在或已结束，无法继续监听",
                    "watch": True,
                })
            return

        # HITL：任务正停在人工确认门时，向重连订阅者重放 awaiting_confirmation
        # 帧，前端据此重新渲染确认卡片（帧为注册表中的完整 WS 载荷）。
        pending = harness_pending_confirmation(task_id)
        if pending:
            try:
                await websocket.send_json(pending)
            except Exception:
                return

        async def _client_gone():
            try:
                await websocket.receive_text()
            except Exception:
                pass

        waiter = asyncio.create_task(_client_gone())
        try:
            await asyncio.wait(
                {forwarder, waiter}, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()
    finally:
        bus.unsubscribe(task_id, queue)
        try:
            queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            pass
        if not forwarder.done():
            forwarder.cancel()
        try:
            await websocket.close()
        except Exception:
            pass

@router.get("/api/task/{task_id}")
async def api_task_status(task_id: str):
    """Task status payload; terminal tasks also carry render-ready result data.

    The frontend reconnect-resume flow uses this to decide between rewatching
    (running), rendering the stored report (completed) or offering a restart
    (failed/cancelled/unknown).
    """
    status = harness_get_status(task_id)
    if not status:
        return {"error": "Task not found"}
    payload = status.model_dump()
    payload["report"] = (payload.get("report") or "")[:_wc._TASK_REPORT_MAX_CHARS]
    extras = get_repository().get_task_extras(task_id)
    payload["arguments"] = extras.get("arguments") or ""
    if extras.get("duration_s") is not None:
        payload["duration_seconds"] = extras["duration_s"]
    if extras.get("cost_yuan") is not None:
        payload["cost_yuan"] = extras["cost_yuan"]
    # 断点续跑：interrupted 任务留有检查点时前端可提示「可恢复」
    payload["has_checkpoints"] = bool(extras.get("has_checkpoints"))
    if extras.get("checkpoint_phases"):
        payload["checkpoint_phases"] = extras["checkpoint_phases"]
    return payload


@router.get("/api/tasks")
async def api_list_tasks(status_filter: str = None, limit: int = 50):
    repo = get_repository()
    tasks = repo.list_tasks(limit=limit, status_filter=status_filter)
    out = []
    for t in tasks:
        item = t.model_dump()
        item["billing"] = repo.get_billing(t.task_id)
        out.append(item)
    return {"tasks": out}
