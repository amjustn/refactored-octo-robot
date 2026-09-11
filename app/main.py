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
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .core.config import BASE_DIR, HOST, MAX_REPORT_AGE_DAYS, PORT, REPORTS_DIR
from .web_common import data_cache, logger, static_dir, task_store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Lifespan 启动钩子 — 迁移自 @app.on_event("startup")，语义等价保留。

    注意：不再使用 on_event("startup")，避免与 lifespan 双跑导致下述
    迁移/种子/后台任务执行两次。TestClient 须以 context manager 使用
    （with TestClient(app)）才会触发本启动逻辑（REVIEW_FINDINGS.md Q6）。
    """
    task_store.mark_running_as_interrupted()
    # Harness repository: idempotent billing-column migration at startup
    repo = get_repository()
    # 断点续跑：扫描留有检查点的 interrupted 任务并登记日志。
    # 安全起见绝不自动重跑（避免重启即烧 token）；任务状态保持
    # interrupted，前端经 /api/task/{id} 的 has_checkpoints 感知，
    # 用户显式调 /api/tasks/{task_id}/resume 时 prepare_resume 自会
    # 利用 checkpoints 恢复现场。
    try:
        resumable = repo.list_interrupted_with_checkpoints()
        for item in resumable:
            logger.info(
                f"可恢复任务 {item['task_id']}（{item['skill_name']}）："
                f"{item['checkpoint_count']} 个检查点，阶段 {item.get('phases') or '-'}"
            )
        if resumable:
            logger.info(f"共 {len(resumable)} 个中断任务可从检查点恢复（需手动触发 resume，不自动重跑）")
    except Exception as e:
        logger.warning(f"可恢复任务扫描失败: {e}")
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
    asyncio.create_task(_verify_decision_log())
    asyncio.create_task(_cleanup_old_reports())
    asyncio.create_task(_cleanup_old_tasks())
    asyncio.create_task(_cleanup_expired_cache())
    asyncio.create_task(_auto_update_knowledge())
    asyncio.create_task(_auto_update_skill_knowledge())
    logger.info("AI Berkshire Web v3.0.0 started (market data + knowledge auto-update + 21-skill auto-update)")
    try:
        yield
    finally:
        logger.info("AI Berkshire Web v3.0.0 shutting down")

app = FastAPI(title="AI Berkshire Web", version="3.0.0", lifespan=lifespan)

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

# ==================== 客户端来源白名单（批F 2026-09-11） ====================
# 目的：不用账号密码、不用输任何东西，就把"谁能调用本服务"限制住。
# 背景：服务端持有默认 LLM Key，任何能访问 /app 的人都能烧这条 Key；
# 目前还有零配置即可开跑的入口（批A），所以来源限制比密码更划算。
# 策略（.env 可调，默认值不改变本机与局域网内既有用法）：
#   * 未配置                         → auto：放行 loopback / 私网 / 链路本地，拒绝公网来源
#   * BERKSHIRE_CLIENT_ALLOWLIST=... → strict：只放行列表内来源，关键字支持
#                                      loopback / gateway / private，其余写 IP 或 CIDR
#   * BERKSHIRE_CLIENT_GUARD=off     → 完全关闭（回到旧行为）
# 只依据 TCP 对端地址判定，**不信任 X-Forwarded-For**（请求头可伪造）。
_ALLOWLIST_RAW = os.getenv("BERKSHIRE_CLIENT_ALLOWLIST", "").strip()
_GUARD_MODE = os.getenv("BERKSHIRE_CLIENT_GUARD", "").strip().lower()
_logged_clients: set = set()


def _detect_default_gateway() -> str:
    """WSL/NAT 环境下的默认网关（= Windows 宿主机）地址；取不到返回 ''。"""
    try:
        import socket as _socket
        import struct as _struct
        with open("/proc/net/route", "r", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return _socket.inet_ntoa(_struct.pack("<L", int(parts[2], 16)))
    except Exception:
        pass
    return ""


_WSL_GATEWAY = _detect_default_gateway()


def _parse_allowlist(raw: str) -> list:
    """规则统一为 ('kw', 'loopback'|'gateway'|'private') 或 ('net', ip_network)。"""
    rules: list = []
    for part in [p.strip() for p in (raw or "").split(",") if p.strip()]:
        low = part.lower()
        if low in ("loopback", "gateway", "private"):
            rules.append(("kw", low))
            continue
        try:
            rules.append(("net", ip_network(part, strict=False)))
        except ValueError:
            logger.warning(f"BERKSHIRE_CLIENT_ALLOWLIST 条目无法解析，已忽略: {part}")
    return rules


_RULES = _parse_allowlist(_ALLOWLIST_RAW)

# auto 模式放行范围：只认"本机 / 局域网 / 链路本地"这些确定的可信网段。
# 刻意不用 addr.is_private —— Python 把 192.0.2.0/24、198.51.100.0/24、203.0.113.0/24、
# 240.0.0.0/4 等保留/文档网段也算进 is_private，用它会把这些"非局域网"一起放进来自。
# 若日后加了 Tailscale/隧道（100.64.0.0/10 等），把对应网段写进 BERKSHIRE_CLIENT_ALLOWLIST。
_AUTO_ALLOW = [
    ip_network("127.0.0.0/8"), ip_network("::1/128"),
    ip_network("10.0.0.0/8"), ip_network("172.16.0.0/12"), ip_network("192.168.0.0/16"),
    ip_network("169.254.0.0/16"), ip_network("fe80::/10"),
]


def _in_auto_allow(addr) -> bool:
    return any(addr.version == n.version and addr in n for n in _AUTO_ALLOW)


def client_allowed(ip: str) -> bool:
    """纯函数：该客户端地址是否放行（便于单测/回归，不依赖请求上下文）。"""
    if _GUARD_MODE == "off":
        return True
    try:
        addr = ip_address(ip or "")
    except ValueError:
        # starlette TestClient 把 client.host 置为字面量 'testclient'（非 IP）。
        # 真实部署里 uvicorn 必然给出 TCP 对端 IP，所以这里只对测试客户端放行，
        # 其它无法解析的来源一律拒绝。
        return ip == "testclient"
    # 双栈监听时 IPv4 客户端可能呈现为 ::ffff:a.b.c.d，先归一化再判定
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if not _ALLOWLIST_RAW:  # auto：只挡"非本机/非局域网"来源
        return _in_auto_allow(addr)
    for kind, val in _RULES:  # strict：只放行白名单
        if kind == "kw":
            if val == "loopback" and addr.is_loopback:
                return True
            if val == "private" and _in_auto_allow(addr):
                return True
            if val == "gateway" and _WSL_GATEWAY and str(addr) == _WSL_GATEWAY:
                return True
        else:
            if addr.version == val.version and addr in val:
                return True
    return False


class ClientGuardMiddleware(BaseHTTPMiddleware):
    """来源白名单：不在允许范围内直接 403，并留痕。"""

    async def dispatch(self, request: Request, call_next):
        ip = request.client.host if request.client else ""
        if not client_allowed(ip):
            logger.warning(f"拦截白名单外的客户端: {ip} {request.method} {request.url.path}")
            return JSONResponse(
                status_code=403,
                content={"detail": "来源不在允许范围内（见 BERKSHIRE_CLIENT_ALLOWLIST）"},
            )
        if not (ip == "127.0.0.1" or ip == "::1") and ip not in _logged_clients:
            # 非本机来源首次出现时留一行审计，便于判断要不要收紧
            _logged_clients.add(ip)
            logger.info(f"非本机客户端访问（已放行）: {ip} {request.method} {request.url.path}")
        return await call_next(request)


app.add_middleware(ClientGuardMiddleware)

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
            get_repository().cleanup_old_tasks(max_age_days=7)  # 连带清理孤儿检查点
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


async def _verify_decision_log():
    """P4-验证闭环(2026-09-01): 每6小时对照真实行情验证决策日志。

    先回填缺价的条目, 再判定 pending 决策 — 让闭环自动运转,
    不依赖人工触发。失败不静默: 每次跑完记 summary。
    """
    from .harness.decision_verify import backfill_missing_prices, verify_pending_decisions
    while True:
        try:
            b = await backfill_missing_prices(limit=30)
            v = await verify_pending_decisions(limit=30)
            logger.info(
                f"Decision-log verify cycle: backfill={b} verify={v}"
            )
        except Exception as e:
            logger.error(f"Decision-log verify cycle failed: {e}")
        await asyncio.sleep(6 * 3600)


async def _seed_circuit_breakers():
    """启动健康检查 → ToolGateway 熔断器初始态（东财中断/雅虎 403 → 初始摘除）。"""
    try:
        from .harness.tools import get_gateway
        seeded = await get_gateway().seed_from_bootstrap()
        if seeded:
            logger.info(f"Circuit breakers seeded from startup health check: {seeded}")
    except Exception as e:
        logger.warning(f"Circuit breaker seeding failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
