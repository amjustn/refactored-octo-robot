"""FastAPI Application - AI Berkshire Web v3.0

v3.0: Added real-time market data integration, investment knowledge
auto-update system, and context injection for all agents.
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Header, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from hmac import compare_digest
from pydantic import BaseModel

from .skills import list_skills, get_skill, load_skill_prompt
from .agents.orchestrator import run_single_agent, run_multi_agent, run_series
from .models.schemas import (
    SkillInfo, ResearchRequest, ResearchStatus,
    AgentProgress,
)
from .core.config import BASE_DIR, REPORTS_DIR, PORT, HOST, MAX_REPORT_AGE_DAYS
from .core.task_store import TaskStore
from .core.data_cache import DataCache

app = FastAPI(title="AI Berkshire Web", version="3.0.0")

# ==================== Logging ====================
import logging
logger = logging.getLogger("ai_berkshire.main")

# ==================== Persistent Stores ====================
task_store = TaskStore()
data_cache = DataCache()

# ==================== Rate Limiting ====================
from collections import defaultdict, deque
from time import time

_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 5
_rate_limiter: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(client_ip: str) -> bool:
    now = time()
    dq = _rate_limiter[client_ip]
    while dq and dq[0] < now - _RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT_MAX:
        return False
    dq.append(now)
    return True

# ==================== CORS ====================
_ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:8001,http://127.0.0.1:8001").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== Auth ====================
_API_TOKEN = os.getenv("BERKSHIRE_API_TOKEN", "")


async def _verify_token(x_api_token: str = Header(default="", alias="X-API-Token")):
    if _API_TOKEN:
        if not compare_digest(x_api_token, _API_TOKEN):
            raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API token")
    return True


# ==================== Helpers ====================
def _sanitize_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', name)
    if not name:
        name = "report"
    return name


static_dir = BASE_DIR / "static"
templates_dir = BASE_DIR / "templates"
static_dir.mkdir(parents=True, exist_ok=True)
templates_dir.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

active_tasks: dict[str, ResearchStatus] = {}
_MAX_TASKS = 100


def _add_task(task_id: str, status: ResearchStatus, arguments: str = ""):
    if len(active_tasks) >= _MAX_TASKS:
        oldest_id = next(iter(active_tasks))
        active_tasks.pop(oldest_id, None)
    active_tasks[task_id] = status
    task_store.save_task(status, arguments)


def _update_task(task_id: str, arguments: str = ""):
    if task_id in active_tasks:
        task_store.save_task(active_tasks[task_id], arguments)


# ==================== Routes ====================

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = templates_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>AI Berkshire Web</h1><p>Loading...</p>")


@app.get("/health")
async def health():
    from .core.config import LLM_API_KEY, LLM_MODEL
    from .tools.knowledge_updater import get_knowledge_status
    from .tools.skill_knowledge import get_all_skill_knowledge_status
    return {
        "status": "ok",
        "version": "3.0.0",
        "llm_configured": bool(LLM_API_KEY),
        "llm_model": LLM_MODEL,
        "skills_count": len(list_skills()),
        "reports_count": len(list(REPORTS_DIR.glob("*.md"))) if REPORTS_DIR.exists() else 0,
        "active_tasks": len(active_tasks),
        "db_size": task_store.db_path.stat().st_size if task_store.db_path.exists() else 0,
        "data_cache": data_cache.get_stats(),
        "knowledge_status": get_knowledge_status(),
        "skill_knowledge_status": get_all_skill_knowledge_status(),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/favicon.ico")
async def favicon():
    favicon_path = static_dir / "favicon.svg"
    if favicon_path.exists():
        return FileResponse(str(favicon_path), media_type="image/svg+xml")
    return HTMLResponse("")


@app.get("/api/skills", dependencies=[Depends(_verify_token)])
async def api_list_skills():
    skills = list_skills()
    return {
        "skills": [
            {
                "name": s["name"],
                "display_name": s["display_name"],
                "description": s["description"],
                "category": s["category"],
                "is_multi_agent": s.get("is_multi_agent", False),
                "agent_count": s.get("agent_count", 1),
                "series_mode": s.get("series_mode", False),
                "series_topics": s.get("series_topics", []),
                "input_hint": s.get("input_hint", ""),
            }
            for s in skills
        ]
    }


@app.get("/api/skills/{skill_name}", dependencies=[Depends(_verify_token)])
async def api_get_skill(skill_name: str):
    skill = get_skill(skill_name)
    if not skill:
        return {"error": f"Skill not found: {skill_name}"}
    prompt = load_skill_prompt(skill_name)
    return {
        "skill": skill,
        "prompt_preview": prompt[:500] + "..." if len(prompt) > 500 else prompt,
    }


# ==================== Market Data API ====================

@app.get("/api/data/price/{symbol}", dependencies=[Depends(_verify_token)])
async def api_get_price(symbol: str):
    """Get real-time stock price for any market."""
    from .tools.market_data import get_stock_price
    data = await get_stock_price(symbol)
    return {"symbol": symbol, "data": data}


@app.get("/api/data/financials/{symbol}", dependencies=[Depends(_verify_token)])
async def api_get_financials(symbol: str):
    """Get financial statements for a company."""
    from .tools.market_data import get_financial_data
    data = await get_financial_data(symbol)
    return {"symbol": symbol, "data": data}


@app.get("/api/data/news/{symbol}", dependencies=[Depends(_verify_token)])
async def api_get_news(symbol: str, days: int = 14):
    """Get recent news for a company."""
    from .tools.market_data import fetch_company_news
    data = await fetch_company_news(symbol, days)
    return {"symbol": symbol, "news": data}


@app.get("/api/data/indices", dependencies=[Depends(_verify_token)])
async def api_get_indices():
    """Get major market indices."""
    from .tools.market_data import fetch_market_indices
    data = await fetch_market_indices()
    return {"indices": data}


@app.get("/api/data/search", dependencies=[Depends(_verify_token)])
async def api_search_company(q: str):
    """Search for companies by name or code."""
    from .tools.market_data import search_company
    results = await search_company(q)
    return {"query": q, "results": results}


@app.get("/api/data/context", dependencies=[Depends(_verify_token)])
async def api_get_context(arguments: str = "", skill_name: str = ""):
    """Get the full real-world context that would be injected into agent prompts."""
    from .core.context import build_full_context
    context = await build_full_context(arguments, skill_name=skill_name)
    return {"context": context}


@app.get("/api/data/cache/stats", dependencies=[Depends(_verify_token)])
async def api_cache_stats():
    """Get data cache statistics."""
    return {"stats": data_cache.get_stats()}


@app.post("/api/data/cache/cleanup", dependencies=[Depends(_verify_token)])
async def api_cache_cleanup():
    """Manually trigger cache cleanup."""
    deleted = data_cache.cleanup_expired()
    return {"deleted": deleted}


# ==================== Knowledge Base API ====================

@app.get("/api/knowledge/status", dependencies=[Depends(_verify_token)])
async def api_knowledge_status():
    """Get knowledge base status for all investment masters."""
    from .tools.knowledge_updater import get_knowledge_status
    return {"masters": get_knowledge_status()}


@app.get("/api/knowledge/{master_key}", dependencies=[Depends(_verify_token)])
async def api_get_knowledge(master_key: str):
    """Get knowledge for a specific investment master."""
    from .tools.knowledge_updater import get_master_knowledge
    return get_master_knowledge(master_key)


@app.get("/api/knowledge", dependencies=[Depends(_verify_token)])
async def api_get_all_knowledge():
    """Get all knowledge base entries."""
    from .tools.knowledge_updater import get_all_knowledge
    return {"knowledge": get_all_knowledge()}


@app.post("/api/knowledge/update", dependencies=[Depends(_verify_token)])
async def api_update_knowledge(master_key: str = None):
    """Trigger knowledge base update. If master_key is specified, only update that master."""
    from .tools.knowledge_updater import update_master_knowledge, update_all_masters
    if master_key:
        result = await update_master_knowledge(master_key)
    else:
        result = await update_all_masters()
    return {"result": result}


# ==================== Skill Knowledge API ====================

@app.get("/api/skill-knowledge/status", dependencies=[Depends(_verify_token)])
async def api_skill_knowledge_status():
    """Get self-update status for ALL 21 skills' domain knowledge."""
    from .tools.skill_knowledge import get_all_skill_knowledge_status
    return {"skills": get_all_skill_knowledge_status()}


@app.get("/api/skill-knowledge/{skill_name}", dependencies=[Depends(_verify_token)])
async def api_get_skill_knowledge(skill_name: str):
    """Get domain knowledge for a specific skill."""
    from .tools.skill_knowledge import get_skill_knowledge
    data = get_skill_knowledge(skill_name)
    return data


@app.post("/api/skill-knowledge/update", dependencies=[Depends(_verify_token)])
async def api_update_skill_knowledge(skill_name: str = None):
    """Trigger skill knowledge update. If skill_name is specified, only update that skill."""
    from .tools.skill_knowledge import update_skill_knowledge, update_all_skill_knowledge
    if skill_name:
        result = await update_skill_knowledge(skill_name)
    else:
        result = await update_all_skill_knowledge()
    return {"result": result}


# ==================== Research API ====================

@app.post("/api/research", dependencies=[Depends(_verify_token)])
async def api_start_research(request: "Request", req: ResearchRequest):
    task_id = str(uuid.uuid4())[:8]
    if not _check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    skill = get_skill(req.skill_name)
    if not skill:
        return {"error": f"Unknown skill: {req.skill_name}"}

    is_multi = skill.get("is_multi_agent", False)
    is_series = skill.get("series_mode", False)

    if is_series:
        agent_names = [f"article-{i+1}" for i in range(len(skill.get("series_topics", [])))]
    elif is_multi:
        agent_names = skill.get("agents", [])
    else:
        agent_names = ["default"]

    status = ResearchStatus(
        task_id=task_id,
        skill_name=req.skill_name,
        status="running",
        agents=[AgentProgress(agent_name=name, status="pending") for name in agent_names],
    )
    _add_task(task_id, status, req.arguments)

    try:
        if is_series:
            report = await run_series(req.skill_name, req.arguments, progress_callback=None)
        elif is_multi:
            report = await run_multi_agent(req.skill_name, req.arguments, progress_callback=None)
        else:
            report = await run_single_agent(req.skill_name, req.arguments)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_skill = _sanitize_filename(req.skill_name)
        report_path = REPORTS_DIR / f"{safe_skill}_{timestamp}.md"
        report_path.write_text(report, encoding="utf-8")

        status.status = "completed"
        status.report = report
        for a in status.agents:
            a.status = "completed"
            a.progress = 1.0
        _update_task(task_id, req.arguments)

        return {"task_id": task_id, "status": "completed", "report": report, "report_path": str(report_path)}
    except Exception as e:
        status.status = "failed"
        status.error = str(e)
        _update_task(task_id, req.arguments)
        return {"task_id": task_id, "status": "failed", "error": str(e)}
    finally:
        asyncio.create_task(_cleanup_task(task_id, 3600))


@app.websocket("/ws/research/{skill_name}")
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
        token = req.get("token", "")
    except json.JSONDecodeError:
        arguments = data
        token = ""

    if _API_TOKEN and not compare_digest(token, _API_TOKEN):
        await websocket.send_json({"type": "error", "message": "Unauthorized"})
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    task_id = str(uuid.uuid4())[:8]
    skill = get_skill(skill_name)
    if not skill:
        await websocket.send_json({"type": "error", "message": f"Unknown skill: {skill_name}"})
        try:
            await websocket.close()
        except Exception:
            pass
        return

    is_multi = skill.get("is_multi_agent", False)
    is_series = skill.get("series_mode", False)

    if is_series:
        agent_names = [f"article-{i+1}" for i in range(len(skill.get("series_topics", [])))]
    elif is_multi:
        agent_names = skill.get("agents", [])
    else:
        agent_names = ["default"]

    status = ResearchStatus(
        task_id=task_id, skill_name=skill_name, status="running",
        agents=[AgentProgress(agent_name=name, status="pending") for name in agent_names],
    )
    _add_task(task_id, status, arguments)

    try:
        for a in status.agents:
            a.status = "running"
        _update_task(task_id, arguments)

        if is_series or is_multi:
            async def _ws_progress(p):
                try:
                    await websocket.send_json({
                        "type": "progress",
                        "agent": p.agent_name,
                        "status": p.status,
                        "message": p.message,
                    })
                except Exception:
                    pass

            if is_series:
                report = await run_series(skill_name, arguments, progress_callback=_ws_progress)
            else:
                report = await run_multi_agent(skill_name, arguments, progress_callback=_ws_progress)
        else:
            from .skills import build_user_prompt
            from .core.llm import chat_stream

            # Inject context for single-agent streaming too
            from .core.context import build_full_context
            try:
                context = await build_full_context(arguments, skill_name=skill_name)
            except Exception:
                context = ""

            user_msg = build_user_prompt(skill_name, arguments)
            system_msg = "你是一位精通价值投资的分析师，遵循巴菲特、芒格、段永平、李录四位大师的方法论。"
            if context:
                system_msg += f"\n\n{context}"

            full_report = ""
            async for chunk in chat_stream(system_msg, user_msg):
                full_report += chunk
                await websocket.send_json({"type": "chunk", "content": chunk})

            report = full_report

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_skill = _sanitize_filename(skill_name)
        report_path = REPORTS_DIR / f"{safe_skill}_{timestamp}.md"
        report_path.write_text(report, encoding="utf-8")

        status.status = "completed"
        status.report = report
        for a in status.agents:
            a.status = "completed"
            a.progress = 1.0
        _update_task(task_id, arguments)

        await websocket.send_json({
            "type": "complete",
            "report": report,
            "report_path": str(report_path),
        })

    except Exception as e:
        status.status = "failed"
        status.error = str(e)
        _update_task(task_id, arguments)
        await websocket.send_json({"type": "error", "message": str(e)})
    finally:
        asyncio.create_task(_cleanup_task(task_id, 3600))
        try:
            await websocket.close()
        except Exception:
            pass


# ==================== Reports & Tasks ====================

@app.get("/api/reports", dependencies=[Depends(_verify_token)])
async def api_list_reports():
    reports = []
    if REPORTS_DIR.exists():
        for f in sorted(REPORTS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            reports.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return {"reports": reports[:50]}


@app.get("/api/reports/{filename}", dependencies=[Depends(_verify_token)])
async def api_get_report(filename: str):
    safe_name = _sanitize_filename(filename)
    report_path = (REPORTS_DIR / safe_name).resolve()
    try:
        report_path.relative_to(REPORTS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside reports directory")
    if not report_path.exists():
        return {"error": "Report not found"}
    content = report_path.read_text(encoding="utf-8")
    return {"filename": safe_name, "content": content}


@app.get("/api/task/{task_id}", dependencies=[Depends(_verify_token)])
async def api_task_status(task_id: str):
    if task_id in active_tasks:
        return active_tasks[task_id]
    task = task_store.get_task(task_id)
    if task:
        return task
    return {"error": "Task not found"}


@app.get("/api/tasks", dependencies=[Depends(_verify_token)])
async def api_list_tasks(status_filter: str = None, limit: int = 50):
    tasks = task_store.list_tasks(limit=limit, status_filter=status_filter)
    return {"tasks": [t.model_dump() for t in tasks]}


@app.post("/api/tools/{tool_name}", dependencies=[Depends(_verify_token)])
async def api_run_tool(tool_name: str, params: dict = None):
    """Run a financial tool (sync tools only via this endpoint)."""
    from .tools import (
        verify_market_cap, verify_valuation, cross_validate,
        three_scenario, calc, benford
    )
    tool_map = {
        "verify-market-cap": verify_market_cap,
        "verify-valuation": verify_valuation,
        "cross-validate": cross_validate,
        "three-scenario": three_scenario,
        "calc": calc,
        "benford": benford,
    }
    if tool_name not in tool_map:
        return {"error": f"Unknown tool: {tool_name}", "available": list(tool_map.keys())}
    try:
        result = tool_map[tool_name](**(params or {}))
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== Background Tasks ====================

async def _cleanup_task(task_id: str, delay: int):
    await asyncio.sleep(delay)
    active_tasks.pop(task_id, None)


async def _cleanup_old_reports():
    while True:
        await asyncio.sleep(86400)
        try:
            import time as _time
            cutoff = _time.time() - MAX_REPORT_AGE_DAYS * 86400
            count = 0
            for f in REPORTS_DIR.glob("*.md"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    count += 1
            if count:
                logger.info(f"Cleaned up {count} old reports (>{MAX_REPORT_AGE_DAYS} days)")
        except Exception as e:
            logger.warning(f"Report cleanup failed: {e}")


async def _cleanup_old_tasks():
    while True:
        await asyncio.sleep(86400)
        try:
            task_store.cleanup_old_tasks(max_age_days=7)
        except Exception as e:
            logger.warning(f"Task cleanup failed: {e}")


async def _cleanup_expired_cache():
    """Clean up expired data cache entries every 6 hours."""
    while True:
        await asyncio.sleep(21600)
        try:
            data_cache.cleanup_expired()
        except Exception as e:
            logger.warning(f"Cache cleanup failed: {e}")


async def _auto_update_knowledge():
    """Periodically update investment knowledge base (every 7 days)."""
    from .tools.knowledge_updater import update_all_masters
    # Initial delay to let server stabilize
    await asyncio.sleep(60)
    while True:
        try:
            logger.info("Starting scheduled knowledge base update...")
            results = await update_all_masters()
            success = sum(1 for r in results.values() if "error" not in r)
            logger.info(f"Knowledge update complete: {success}/{len(results)} masters updated")
        except Exception as e:
            logger.error(f"Scheduled knowledge update failed: {e}")
        # Wait 7 days
        await asyncio.sleep(7 * 86400)


async def _auto_update_skill_knowledge():
    """Periodically update ALL 21 skills' domain knowledge (every 7 days).

    This ensures every analysis capability stays current with latest
    frameworks, criteria, and methodologies — not just the investment
    masters' philosophy.
    """
    from .tools.skill_knowledge import update_all_skill_knowledge
    # Initial delay: wait 120s after server starts (after masters update begins)
    await asyncio.sleep(120)
    while True:
        try:
            logger.info("Starting scheduled skill knowledge update for all 21 skills...")
            results = await update_all_skill_knowledge()
            success = sum(1 for r in results.values() if "error" not in r)
            logger.info(f"Skill knowledge update complete: {success}/{len(results)} skills updated")
        except Exception as e:
            logger.error(f"Scheduled skill knowledge update failed: {e}")
        # Wait 7 days
        await asyncio.sleep(7 * 86400)


@app.on_event("startup")
async def _startup():
    task_store.mark_running_as_interrupted()
    # Ensure default knowledge base exists on first run
    try:
        from .tools.knowledge_updater import _ensure_default_knowledge
        _ensure_default_knowledge()
    except Exception as e:
        logger.warning(f"Could not initialize default knowledge: {e}")
    # Ensure ALL 21 skills have default domain knowledge on first run
    try:
        from .tools.skill_knowledge import _ensure_all_default_knowledge
        _ensure_all_default_knowledge()
    except Exception as e:
        logger.warning(f"Could not initialize skill knowledge: {e}")
    asyncio.create_task(_cleanup_old_reports())
    asyncio.create_task(_cleanup_old_tasks())
    asyncio.create_task(_cleanup_expired_cache())
    asyncio.create_task(_auto_update_knowledge())
    asyncio.create_task(_auto_update_skill_knowledge())
    logger.info("AI Berkshire Web v3.0.0 started (market data + knowledge auto-update + 21-skill auto-update)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
