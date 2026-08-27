"""FastAPI Application - AI Berkshire Web v3.0

v3.0: Added real-time market data integration, investment knowledge
auto-update system, and context injection for all agents.

Route handlers live in app/routers/* (split out of this module verbatim);
shared state/helpers live in app/web_common.py.  This module keeps the app
object, middleware, the startup hooks and router wiring.  The rate
limiter itself lives in web_common and is re-exported here for back-compat.
"""
import asyncio
import base64
import json
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .core.config import BASE_DIR, HOST, MAX_REPORT_AGE_DAYS, PORT, REPORTS_DIR
from .web_common import data_cache, logger, static_dir, task_store

app = FastAPI(title="AI Berkshire Web", version="3.0.0")

# Rate limiting & WS JWT verify live in web_common (re-exported below).
# ==================== CORS ====================
_ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:8001,http://127.0.0.1:8001").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== Basic Auth (frontend gate) ====================
_BASIC_AUTH = os.getenv("BERKSHIRE_BASIC_AUTH", "")  # format: "user:password"


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth guard for static pages and WebSocket.

    API routes (/api/*) skip this and use the X-API-Token check instead.
    /health also skips for monitoring.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Skip: API tokens have their own auth; health is public; WebSocket has its own token check
        if path.startswith("/api/") or path == "/health" or path.startswith("/ws/"):
            return await call_next(request)

        if not _BASIC_AUTH:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="AI Berkshire Web"'},
            )

        try:
            creds = base64.b64decode(auth[6:]).decode("utf-8")
            user, pwd = creds.split(":", 1)
        except Exception:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="AI Berkshire Web"'},
            )

        expected_user, expected_pwd = _BASIC_AUTH.split(":", 1)
        if user != expected_user or pwd != expected_pwd:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="AI Berkshire Web"'},
            )

        return await call_next(request)


app.add_middleware(BasicAuthMiddleware)

# ==================== Auth (JWT) ====================
from .core.config import BERKSHIRE_API_TOKEN
from .core.jwt_utils import verify_token

# Public API paths that don't require authentication
_AUTH_WHITELIST = {"/api/auth/login", "/api/llm/default"}


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """JWT authentication middleware — intercepts every /api/* request.

    Checks the ``Authorization: Bearer <token>`` header.  When
    BERKSHIRE_API_TOKEN is not configured, all requests pass through
    without authentication (dev mode).

    Whitelisted paths (login, llm default info) skip the check.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Only guard /api/* paths
        if not path.startswith("/api/"):
            return await call_next(request)

        # Whitelist: login + public info endpoints
        if path in _AUTH_WHITELIST or path.startswith("/api/auth/"):
            return await call_next(request)

        # Dev mode: no token configured → open
        if BERKSHIRE_API_TOKEN is None:
            return await call_next(request)

        # Validate JWT
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return Response(
                status_code=401,
                content=json.dumps({"detail": "Missing Authorization header"}),
                media_type="application/json",
            )

        token = auth[7:]  # strip "Bearer "
        payload = verify_token(token, BERKSHIRE_API_TOKEN)
        if payload is None:
            return Response(
                status_code=401,
                content=json.dumps({"detail": "Invalid or expired token"}),
                media_type="application/json",
            )

        return await call_next(request)


app.add_middleware(JWTAuthMiddleware)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Task lifecycle (active registry, running jobs, reports, billing) is owned
# by the harness — see app/harness/. Endpoints below are thin translations.


# ==================== Routers ====================
from .routers import charts, data, export_wechat, knowledge, misc, pages, research, skin  # noqa: E402

# NOTE: FastAPI 0.138's include_router() registers a lazy _IncludedRouter
# wrapper (no .path attribute), which would break callers iterating
# app.routes (tests do ``[r.path for r in app.routes]``).  Routers are
# included without prefix/tags/dependencies, so extending the route list
# directly is equivalent to the classic eager include.
for _r in (pages, data, knowledge, research, misc, skin, charts, export_wechat):
    app.router.routes.extend(_r.router.routes)

# Back-compat re-exports: tests (and any external callers) import these
# directly from app.main — keep them available here.
from .harness.persist import get_repository  # noqa: E402
from .routers.misc import _extract_image_text, api_auth_login  # noqa: E402,F401
from .routers.research import ws_research  # noqa: E402,F401
from .web_common import (  # noqa: E402,F401
    _RATE_LIMIT_MAX,
    _RATE_LIMIT_WINDOW,
    _TASK_REPORT_MAX_CHARS,
    _check_rate_limit,
    _rate_limiter,
    _sanitize_filename,
    _validate_llm_config,
    _ws_verify_jwt,
)

# ==================== Background Tasks ====================

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
                    meta = f.with_suffix(".meta.json")
                    if meta.exists():
                        meta.unlink()
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
        # E6: per-task scratchpad JSONL files expire together with tasks.
        try:
            import time as _time
            cutoff = _time.time() - 7 * 86400
            for f in (BASE_DIR / "data" / "scratchpad").glob("*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
        except Exception as e:
            logger.warning(f"Scratchpad cleanup failed: {e}")


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


async def _seed_circuit_breakers():
    """启动健康检查 → ToolGateway 熔断器初始态（东财中断/雅虎 403 → 初始摘除）。"""
    try:
        from .harness.tools import get_gateway
        seeded = await get_gateway().seed_from_bootstrap()
        if seeded:
            logger.info(f"Circuit breakers seeded from startup health check: {seeded}")
    except Exception as e:
        logger.warning(f"Circuit breaker seeding failed: {e}")

@app.on_event("startup")
async def _startup():
    task_store.mark_running_as_interrupted()
    # Harness repository: idempotent billing-column migration at startup
    get_repository()
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
    asyncio.create_task(_seed_circuit_breakers())
    asyncio.create_task(_cleanup_old_reports())
    asyncio.create_task(_cleanup_old_tasks())
    asyncio.create_task(_cleanup_expired_cache())
    asyncio.create_task(_auto_update_knowledge())
    asyncio.create_task(_auto_update_skill_knowledge())
    logger.info("AI Berkshire Web v3.0.0 started (market data + knowledge auto-update + 21-skill auto-update)")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
