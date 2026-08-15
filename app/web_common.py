"""Shared state & helpers for the web layer.

Single importable home for objects used by both ``app.main`` and the
``app.routers`` modules.  This module must NOT import ``app.main`` or
``app.routers`` (that would be circular).

The rate limiter, _ws_verify_jwt and _TASK_REPORT_MAX_CHARS
live here (single source of truth); app.main re-exports them for
back-compat.  Tests that monkeypatch values/functions must target
THIS module — routers look them up here late-bound.
"""
import logging
import re
from collections import defaultdict, deque
from pathlib import Path
from time import time
from typing import Optional

from fastapi import HTTPException

from .core.config import (
    BASE_DIR,
    BERKSHIRE_API_TOKEN,
    REPORTS_DIR,  # noqa: F401  (re-exported; routers read _wc.REPORTS_DIR)
)
from .core.data_cache import DataCache
from .core.jwt_utils import verify_token
from .core.task_store import TaskStore

# ==================== Logging ====================
logger = logging.getLogger("ai_berkshire.main")

# ==================== Persistent Stores ====================
task_store = TaskStore()
data_cache = DataCache()

static_dir = BASE_DIR / "static"
templates_dir = BASE_DIR / "templates"
static_dir.mkdir(parents=True, exist_ok=True)
templates_dir.mkdir(parents=True, exist_ok=True)


# ==================== Rate Limiting ====================
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 5
_rate_limiter: dict[str, deque] = defaultdict(deque)

# Cap on report text served via /api/task/{id} and WS watch-resume frames.
_TASK_REPORT_MAX_CHARS = 200_000


def _check_rate_limit(client_ip: str) -> bool:
    now = time()
    dq = _rate_limiter[client_ip]
    while dq and dq[0] < now - _RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT_MAX:
        return False
    dq.append(now)
    return True


def _ws_verify_jwt(token: str) -> bool:
    """Validate a JWT for WebSocket connections."""
    if BERKSHIRE_API_TOKEN is None:
        return True
    return verify_token(token, BERKSHIRE_API_TOKEN) is not None


# ==================== Helpers ====================
def _sanitize_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', name)
    if not name:
        name = "report"
    return name


def _validate_llm_config(cfg) -> Optional[dict]:
    """Sanitize a user-supplied per-request LLM override.

    Accepts {api_key, base_url, model, synthesis_model, per_agent_enabled,
    agent_models}; returns None when nothing usable was provided (caller then
    falls back to the server default).

    synthesis_model：多Agent任务 Team Lead 综合环节可选的独立模型。

    per_agent_enabled / agent_models：按Agent分配独立LLM配置（可选）。
    agent_models 值可以是 string（仅 model 名，旧版兼容）或 dict
    {model, base_url?, api_key?}（新版，可独立指定 provider/URL/key）。
    仅在 per_agent_enabled=True 时生效。

    When the user passes ONLY a model name (no custom key/URL), validate
    against the server's known good list so they get a clear error
    instead of a cryptic provider rejection.
    """
    if not isinstance(cfg, dict):
        return None
    out = {}
    for key in ("api_key", "base_url", "model", "synthesis_model"):
        val = cfg.get(key)
        if isinstance(val, str):
            val = val.strip()[:300]
            if val:
                out[key] = val
    # per_agent_enabled: bool field
    per_agent_enabled = bool(cfg.get("per_agent_enabled", False))
    if per_agent_enabled:
        out["per_agent_enabled"] = True
    # agent_models: sanitize agent_name → string|{model,base_url?,api_key?}
    raw_agent_models = cfg.get("agent_models")
    if isinstance(raw_agent_models, dict) and raw_agent_models:
        agent_models = {}
        for agent_name, agent_val in raw_agent_models.items():
            if not isinstance(agent_name, str):
                continue
            clean_name = str(agent_name).strip()[:80]
            if not clean_name:
                continue
            if isinstance(agent_val, str):
                # Legacy: plain model name
                clean_model = str(agent_val).strip()[:300]
                if clean_model:
                    agent_models[clean_name] = clean_model
            elif isinstance(agent_val, dict):
                # New: {model, base_url?, api_key?}
                sub = {}
                m = (agent_val.get("model") or "").strip()[:300]
                if m:
                    sub["model"] = m
                for key in ("base_url", "api_key"):
                    val = (agent_val.get(key) or "").strip()[:500]
                    if val:
                        sub[key] = val
                if sub:
                    agent_models[clean_name] = sub
        if agent_models:
            out["agent_models"] = agent_models
    if not out:
        return None
    base_url = out.get("base_url", "")
    if base_url and not re.match(r'^https?://', base_url):
        return None
    # When only model names are overridden (server key + server URL),
    # validate against the provider's supported models.
    # Per-agent models with their own api_key bypass this check
    # (they use the agent's own provider).
    if "api_key" not in out and "base_url" not in out:
        from .core.config import LLM_SUPPORTED_MODELS
        if LLM_SUPPORTED_MODELS:
            for model_key in ("model", "synthesis_model"):
                if model_key in out and out[model_key] not in LLM_SUPPORTED_MODELS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"模型 '{out[model_key]}' 不被服务器支持。可用: {', '.join(LLM_SUPPORTED_MODELS)}"
                    )
            # Per-agent models: validate only those without their own api_key
            for agent_name, agent_val in out.get("agent_models", {}).items():
                model_name = agent_val if isinstance(agent_val, str) else agent_val.get("model", "")
                has_own_key = isinstance(agent_val, dict) and agent_val.get("api_key")
                if has_own_key:
                    continue  # agent has its own provider — skip whitelist
                if model_name and model_name not in LLM_SUPPORTED_MODELS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Agent '{agent_name}' 的模型 '{model_name}' 不被服务器支持。可用: {', '.join(LLM_SUPPORTED_MODELS)}"
                    )
    return out
