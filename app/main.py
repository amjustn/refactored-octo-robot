"""FastAPI Application - AI Berkshire Web v3.0

v3.0: Added real-time market data integration, investment knowledge
auto-update system, and context injection for all agents.
"""
import asyncio
import base64
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Header, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from hmac import compare_digest
from pydantic import BaseModel

from .skills import list_skills, get_skill, load_skill_prompt
from .harness import (
    TaskSpec, run as harness_run, cancel as harness_cancel,
    get_status as harness_get_status, active_count as harness_active_count,
)
from .harness.events import get_bus
from .harness.persist import get_repository
from .models.schemas import (
    SkillInfo, ResearchRequest, ResearchStatus,
    AgentProgress,
)
from .core.config import BASE_DIR, REPORTS_DIR, PORT, HOST, MAX_REPORT_AGE_DAYS
from .core.task_store import TaskStore
from .core.data_cache import DataCache

app = FastAPI(title="AI Berkshire Web", version="3.0.0")

# ==================== 使用手册 ====================
MANUAL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Berkshire 使用手册</title>
<style>
  body{font-family:'Noto Serif SC','Source Han Serif SC','STSong',Georgia,'Times New Roman',serif;max-width:760px;margin:0 auto;padding:32px 24px 60px;background:#f7f3e9;color:#3d3d3d;line-height:1.85;letter-spacing:0.03em}
  h1{color:#3d3d3d;font-weight:400;font-size:1.4rem;border-bottom:1px solid #d4c5a9;padding-bottom:14px;margin-bottom:28px;letter-spacing:0.06em}
  h2{color:#6b8e6b;font-weight:400;margin-top:36px;font-size:1.1rem;letter-spacing:0.05em}
  h3{color:#c4a77d;font-weight:400;font-size:0.95rem;margin-top:24px}
  table{width:100%;border-collapse:collapse;margin:14px 0;font-size:0.82rem}
  th,td{border-bottom:1px solid #e5dac8;padding:8px 12px;text-align:left}
  th{background:#f0ebe0;color:#8c8273;font-weight:400;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.04em}
  td{background:#fdfaf5}
  code{background:#f0ebe0;padding:2px 6px;border-radius:2px;color:#6b8e6b;font-size:0.88em}
  pre{background:#f0ebe0;padding:14px;border-radius:4px;overflow-x:auto;border:1px solid #e5dac8}
  a{color:#6b8e6b;text-decoration:none;border-bottom:1px solid #d4c5a9}
  a:hover{color:#3d3d3d}
  .note{background:#fdfaf5;border-left:2px solid #c4a77d;padding:10px 16px;margin:18px 0;font-size:0.82rem;color:#8c8273}
  .tag{display:inline-block;background:#f0ebe0;color:#8c8273;font-size:0.65rem;padding:1px 6px;border-radius:2px;margin-left:4px;vertical-align:middle}
  ul,ol{padding-left:22px}
  li{margin:5px 0}
</style>
</head>
<body>
<h1>AI Berkshire Web — 价值投资研究工作站 使用指南</h1>
<p>一个人 + AI = 一个投研团队。地址：<code>http://localhost:8001/</code></p>

<div style="text-align:center;margin:24px 0">
<a href="/app" style="display:inline-block;background:#6b8e6b;color:#fff;padding:12px 32px;border-radius:4px;text-decoration:none;font-size:1rem;letter-spacing:0.06em;border:none">→ 进入研究工作站</a>
</div>

<h2>技能速查（19个技能）</h2>

<h3>深度研究</h3>
<table>
<tr><th>技能名</th><th>显示名</th><th>输入</th><th>说明</th></tr>
<tr><td><code>investment-team</code></td><td>多Agent投研团队<span class="tag">4A</span></td><td>公司名/代码</td><td>★推荐★ 商业模式/财务/护城河/估值 4Agent并行</td></tr>
<tr><td><code>investment-research</code></td><td>四大师综合分析</td><td>公司名/代码</td><td>巴菲特+芒格+段永平+李录 四大视角</td></tr>
<tr><td><code>investment-checklist</code></td><td>巴菲特Checklist</td><td>多公司</td><td>六关快速筛选，支持多公司对比</td></tr>
<tr><td><code>management-deep-dive</code></td><td>管理层深研</td><td>管理层名 公司</td><td>履历/能力圈/诚信/资本配置 全景评估</td></tr>
<tr><td><code>private-company-research</code></td><td>未上市公司研究</td><td>公司名</td><td>侦探式公开信息挖掘</td></tr>
<tr><td><code>deep-company-series</code></td><td>深度系列长文</td><td>公司名</td><td>8篇系列专题（~12万字），逐篇生成</td></tr>
</table>

<h3>财报分析</h3>
<table>
<tr><td><code>earnings-review</code></td><td>财报精读</td><td>公司名 季度</td><td>单Agent深度解读，营收/利润/现金流拆解</td></tr>
<tr><td><code>earnings-team</code></td><td>财报精读团队<span class="tag">6A</span></td><td>公司名 季度</td><td>6Agent并行 + 首席审稿，全面覆盖</td></tr>
</table>

<h3>行业筛选</h3>
<table>
<tr><td><code>industry-research</code></td><td>产业链全景扫描</td><td>行业名</td><td>上中下游格局、竞争态势、政策环境</td></tr>
<tr><td><code>industry-funnel</code></td><td>行业漏斗筛选</td><td>行业/方向</td><td>30-60家公司 → 精筛 → 终选3家</td></tr>
<tr><td><code>quality-screen</code></td><td>去劣筛选</td><td>个股/行业</td><td>7项硬指标快速排除劣质公司</td></tr>
<tr><td><code>bottleneck-hunter</code></td><td>瓶颈猎手</td><td>趋势名</td><td>产业链物理瓶颈识别</td></tr>
</table>

<h3>持仓管理</h3>
<table>
<tr><td><code>portfolio-review</code></td><td>组合审视</td><td>持仓清单</td><td>仓位体检 + 调仓建议</td></tr>
<tr><td><code>thesis-tracker</code></td><td>论文追踪</td><td>公司名</td><td>买入后纪律系统：定期复查投资逻辑</td></tr>
<tr><td><code>news-pulse</code></td><td>新闻脉搏<span class="tag">3A</span></td><td>公司名 [涨跌]</td><td>3Agent并行，10分钟新闻归因分析</td></tr>
</table>

<h3>思维工具</h3>
<table>
<tr><td><code>dyp-ask</code></td><td>段永平问答</td><td>投资问题</td><td>以段永平思维框架回答投资问题</td></tr>
<tr><td><code>financial-data</code></td><td>财务数据规范</td><td>股票代码</td><td>交叉验证的财务数据提取</td></tr>
<tr><td><code>wechat-article</code></td><td>公众号文章<span class="tag">3A</span></td><td>主题</td><td>3Agent并行生成公众号风格长文</td></tr>
</table>

<h3>日报</h3>
<table>
<tr><td><code>daily-briefing</code></td><td>金融日报</td><td>（可选）日期</td><td>★NEW★ 每日A股盘后综述：指数/板块/资金/要闻</td></tr>
</table>

<h2>典型使用场景</h2>
<ol>
<li><b>快速评估</b>：选「多Agent投研团队」→ 输入「美团」→ 开始研究</li>
<li><b>对比多家</b>：选「巴菲特Checklist」→ 输入「茅台,五粮液,泸州老窖」</li>
<li><b>行业选股</b>：选「行业漏斗筛选」→ 输入「机器人」</li>
<li><b>持仓体检</b>：选「组合审视」→ 输入「腾讯30%,美团20%,茅台20%,现金30%」</li>
<li><b>新闻归因</b>：选「新闻脉搏」→ 输入「腾讯 -5%」</li>
<li><b>每日复盘</b>：选「金融日报」→ 直接点开始（无需输入）</li>
</ol>

<h2>功能一览</h2>
<table>
<tr><th>功能</th><th>说明</th><th>位置</th></tr>
<tr><td>流式输出</td><td>实时看到 AI 生成内容，支持中途取消</td><td>默认开启</td></tr>
<tr><td>进度显示</td><td>多Agent模式下每个Agent独立显示进度条</td><td>研究时自动显示</td></tr>
<tr><td>复制报告</td><td>一键复制 Markdown 全文到剪贴板</td><td>结果区工具栏</td></tr>
<tr><td>下载 MD</td><td>下载原始 Markdown 文件</td><td>结果区工具栏</td></tr>
<tr><td>导出 HTML</td><td>导出带和风样式的独立 HTML 页面</td><td>结果区工具栏</td></tr>
<tr><td>打印 / PDF</td><td>浏览器打印，可另存为 PDF</td><td>结果区工具栏</td></tr>
<tr><td>目录</td><td>自动提取标题生成侧边目录导航</td><td>结果区工具栏</td></tr>
<tr><td>历史报告</td><td>查看、搜索、删除历史报告，支持两份对比</td><td>左下角按钮</td></tr>
<tr><td>技能收藏</td><td>点击技能旁的 ★ 收藏，置顶显示</td><td>左侧技能列表</td></tr>
<tr><td>技能搜索</td><td>关键词筛选技能，支持名称/描述搜索</td><td>左侧搜索框</td></tr>
<tr><td>输入历史</td><td>自动记录分析目标，下次可快速选择</td><td>输入框下拉</td></tr>
<tr><td>快捷键</td><td>Enter 直接开始研究</td><td>全局</td></tr>
<tr><td>移动端适配</td><td>点击左上角 ☰ 展开/收起侧边栏</td><td>左上角按钮</td></tr>
<tr><td>实时市场数据</td><td>研究时自动注入最新行情/财务/新闻</td><td>后台自动</td></tr>
</table>

<h2>AI 免责声明</h2>
<div class="note">
每条报告末尾均标注：<b>「该数据由AI在数据的基础上进行分析，仅供参考」</b>。
所有分析内容由大语言模型基于公开数据生成，不构成任何投资建议。
投资决策请以个人独立判断为准，市场有风险，投资需谨慎。
</div>

<div class="note"><b>耗时参考</b>：单Agent 1-3分钟，多Agent 3-8分钟，深度系列15-30分钟，日报 2-5分钟。</div>
</body>
</html>"""

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
from .core.jwt_utils import create_token, verify_token
from .core.config import BERKSHIRE_API_TOKEN

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

    Accepts {api_key, base_url, model}; returns None when nothing usable
    was provided (caller then falls back to the server default).

    When the user passes ONLY a model name (no custom key/URL), validate
    it against the server's known good list so they get a clear error
    instead of a cryptic provider rejection.
    """
    if not isinstance(cfg, dict):
        return None
    out = {}
    for key in ("api_key", "base_url", "model"):
        val = cfg.get(key)
        if isinstance(val, str):
            val = val.strip()[:300]
            if val:
                out[key] = val
    if not out:
        return None
    base_url = out.get("base_url", "")
    if base_url and not re.match(r'^https?://', base_url):
        return None
    # When only model is overridden (server key + server URL), validate
    # against the provider's supported models so the user gets a clear
    # error message instead of a raw 400 from the gateway.
    if "model" in out and "api_key" not in out and "base_url" not in out:
        from .core.config import LLM_SUPPORTED_MODELS
        if LLM_SUPPORTED_MODELS and out["model"] not in LLM_SUPPORTED_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"模型 '{out['model']}' 不被服务器支持。可用: {', '.join(LLM_SUPPORTED_MODELS)}"
            )
    return out


static_dir = BASE_DIR / "static"
templates_dir = BASE_DIR / "templates"
static_dir.mkdir(parents=True, exist_ok=True)
templates_dir.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Task lifecycle (active registry, running jobs, reports, billing) is owned
# by the harness — see app/harness/. Endpoints below are thin translations.


# ==================== Routes ====================

@app.get("/manual", response_class=HTMLResponse)
async def manual():
    """使用手册"""
    return HTMLResponse(MANUAL_HTML)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Redirect to manual — 每次打开先看使用手册"""
    return RedirectResponse(url="/manual", status_code=302)


@app.get("/app", response_class=HTMLResponse)
async def app_index():
    """Main application — auto-injects JWT for authenticated users."""
    index_path = templates_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>AI Berkshire Web</h1><p>Loading...</p>")

    html = index_path.read_text(encoding="utf-8")

    # Inject JWT into the page so the frontend picks it up automatically.
    # BasicAuthMiddleware already verified the user — no need to prompt again.
    if BERKSHIRE_API_TOKEN:
        jwt_token = create_token(BERKSHIRE_API_TOKEN)
        script_tag = (
            f'<script>window.__JWT__={json.dumps(jwt_token)};'
            f'localStorage.setItem("ai_berkshire_jwt",{json.dumps(jwt_token)});</script>'
        )
        html = html.replace("</head>", script_tag + "</head>")

    return HTMLResponse(html)


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
        "active_tasks": harness_active_count(),
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


# ==================== Auth API ====================

@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    """Exchange the server secret for a JWT.

    Request body:  {"token": "<BERKSHIRE_API_TOKEN>"}
    Response:      {"access_token": "<jwt>", "token_type": "bearer"}
    """
    if BERKSHIRE_API_TOKEN is None:
        return {"access_token": "", "token_type": "bearer", "note": "Auth disabled"}

    try:
        body = await request.json()
    except Exception:
        body = {}

    secret = (body.get("token") or "").strip()
    if not compare_digest(secret, BERKSHIRE_API_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")

    jwt_token = create_token(BERKSHIRE_API_TOKEN)
    return {"access_token": jwt_token, "token_type": "bearer"}


@app.get("/api/skills")
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
                "agents": s.get("agents", []),
                "series_mode": s.get("series_mode", False),
                "series_topics": s.get("series_topics", []),
                "input_hint": s.get("input_hint", ""),
            }
            for s in skills
        ]
    }


@app.get("/api/skills/{skill_name}")
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


@app.get("/api/health/data-sources")
async def api_data_source_health():
    """Check all data source availability (libraries + HTTP endpoints),
    plus ToolGateway circuit-breaker states."""
    from .core.bootstrap import health_check_data_sources
    from .harness.tools import get_gateway
    result = await health_check_data_sources()
    result["circuit_breakers"] = get_gateway().breaker_states()
    return result


@app.post("/api/health/data-sources/reset")
async def api_data_source_reset(tool: str = None):
    """Manually reset circuit breakers (one tool, or all when omitted)."""
    from .harness.tools import get_gateway
    return get_gateway().reset_breaker(tool)



@app.get("/api/data/web-search")
async def api_web_search(q: str, max_results: int = 10):
    """Search the web for any topic."""
    from .tools.web_search import web_search
    results = await web_search(q, max_results=max_results)
    return {"query": q, "results": results}


@app.get("/api/data/read-url")
async def api_read_url(url: str, max_chars: int = 8000):
    """Read any web page and extract text content."""
    from .tools.web_search import read_webpage
    result = await read_webpage(url, max_chars=max_chars)
    return result



@app.get("/api/data/industry-search")
async def api_industry_search(q: str, max_per_source: int = 3):
    """Search industry chain information from curated sources."""
    from .tools.industry_sources import search_industry_chain
    result = await search_industry_chain(q, max_per_source=max_per_source)
    return result


@app.get("/api/data/industry-sources")
async def api_industry_sources(category: str = ""):
    """List available industry research data sources."""
    from .tools.industry_sources import list_industry_sources
    return {"sources": list_industry_sources(category)}


@app.get("/api/data/price/{symbol}")
async def api_get_price(symbol: str):
    """Get real-time stock price for any market."""
    from .tools.market_data import get_stock_price
    data = await get_stock_price(symbol)
    return {"symbol": symbol, "data": data}


@app.get("/api/data/financials/{symbol}")
async def api_get_financials(symbol: str):
    """Get financial statements for a company."""
    from .tools.market_data import get_financial_data
    data = await get_financial_data(symbol)
    return {"symbol": symbol, "data": data}


@app.get("/api/data/news/{symbol}")
async def api_get_news(symbol: str, days: int = 14):
    """Get recent news for a company."""
    from .tools.market_data import fetch_company_news
    data = await fetch_company_news(symbol, days)
    return {"symbol": symbol, "news": data}


@app.get("/api/data/indices")
async def api_get_indices():
    """Get major market indices."""
    from .tools.market_data import fetch_market_indices
    data = await fetch_market_indices()
    return {"indices": data}


@app.get("/api/data/search")
async def api_search_company(q: str):
    """Search for companies by name or code."""
    from .tools.market_data import search_company
    results = await search_company(q)
    return {"query": q, "results": results}


@app.get("/api/data/context")
async def api_get_context(arguments: str = "", skill_name: str = ""):
    """Get the full real-world context that would be injected into agent prompts."""
    import asyncio
    from .core.context import build_full_context, build_time_context
    try:
        # 整体超时保护：数据源（如 Yahoo）不可达时不得无限挂起
        context = await asyncio.wait_for(
            build_full_context(arguments, skill_name=skill_name),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        context = build_time_context() + "\n\n*（市场数据获取超时，仅返回时间上下文）*"
    return {"context": context}


@app.get("/api/data/cache/stats")
async def api_cache_stats():
    """Get data cache statistics."""
    return {"stats": data_cache.get_stats()}


@app.post("/api/data/cache/cleanup")
async def api_cache_cleanup():
    """Manually trigger cache cleanup."""
    deleted = data_cache.cleanup_expired()
    return {"deleted": deleted}


# ==================== Knowledge Base API ====================

@app.get("/api/knowledge/status")
async def api_knowledge_status():
    """Get knowledge base status for all investment masters."""
    from .tools.knowledge_updater import get_knowledge_status
    return {"masters": get_knowledge_status()}


@app.get("/api/knowledge/{master_key}")
async def api_get_knowledge(master_key: str):
    """Get knowledge for a specific investment master."""
    from .tools.knowledge_updater import get_master_knowledge
    return get_master_knowledge(master_key)


@app.get("/api/knowledge")
async def api_get_all_knowledge():
    """Get all knowledge base entries."""
    from .tools.knowledge_updater import get_all_knowledge
    return {"knowledge": get_all_knowledge()}


@app.post("/api/knowledge/update")
async def api_update_knowledge(master_key: str = None):
    """Trigger knowledge base update. If master_key is specified, only update that master."""
    from .tools.knowledge_updater import update_master_knowledge, update_all_masters
    if master_key:
        result = await update_master_knowledge(master_key)
    else:
        result = await update_all_masters()
    return {"result": result}


# ==================== Skill Knowledge API ====================

@app.get("/api/skill-knowledge/status")
async def api_skill_knowledge_status():
    """Get self-update status for ALL 21 skills' domain knowledge."""
    from .tools.skill_knowledge import get_all_skill_knowledge_status
    return {"skills": get_all_skill_knowledge_status()}


@app.get("/api/skill-knowledge/{skill_name}")
async def api_get_skill_knowledge(skill_name: str):
    """Get domain knowledge for a specific skill."""
    from .tools.skill_knowledge import get_skill_knowledge
    data = get_skill_knowledge(skill_name)
    return data


@app.post("/api/skill-knowledge/update")
async def api_update_skill_knowledge(skill_name: str = None):
    """Trigger skill knowledge update. If skill_name is specified, only update that skill."""
    from .tools.skill_knowledge import update_skill_knowledge, update_all_skill_knowledge
    if skill_name:
        result = await update_skill_knowledge(skill_name)
    else:
        result = await update_all_skill_knowledge()
    return {"result": result}


# ==================== Research API ====================

@app.post("/api/research")
async def api_start_research(request: "Request", req: ResearchRequest):
    if not _check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    skill = get_skill(req.skill_name)
    if not skill:
        return {"error": f"Unknown skill: {req.skill_name}"}

    llm_config = _validate_llm_config(req.llm_config)
    spec = TaskSpec.build(
        skill, req.arguments, llm_override=llm_config,
        caller="http", stream=False,
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


@app.post("/api/cancel/{task_id}")
async def api_cancel_research(task_id: str):
    """Cancel a running research task on the server side (stops LLM calls)."""
    return harness_cancel(task_id)


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
        llm_config = _validate_llm_config(req.get("llm_config"))
    except json.JSONDecodeError:
        arguments = data
        token = ""
        llm_config = None

    if not _ws_verify_jwt(token):
        await websocket.send_json({"type": "error", "message": "Unauthorized"})
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


# ==================== LLM Config API ====================

@app.get("/api/llm/default")
async def api_llm_default():
    """Expose the server defaults — never the API key.

    Returns the model list the server's gateway actually supports so the
    frontend can build a per-provider model picker instead of a single
    free-form field.
    """
    from .core.config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, LLM_SUPPORTED_MODELS
    provider = "deepseek"  # could become config-driven later
    return {
        "provider": provider,
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "has_key": bool(LLM_API_KEY),
        "supported_models": LLM_SUPPORTED_MODELS,
    }


@app.post("/api/llm/test")
async def api_llm_test(request: "Request"):
    """Test a user-supplied LLM config with a minimal completion call."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    llm_config = _validate_llm_config(body.get("llm_config") if isinstance(body, dict) else None)
    if not llm_config:
        return {"ok": False, "error": "配置为空或格式不正确（需要 api_key / base_url / model）"}
    from .core.llm import chat_complete
    try:
        reply = await asyncio.wait_for(
            chat_complete(
                "你是一个连接测试助手。",
                "请只回复两个字：正常",
                llm_config=llm_config,
                temperature=0,
            ),
            timeout=30.0,
        )
        return {"ok": True, "model": llm_config.get("model", ""), "reply": (reply or "")[:50]}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "连接超时（30秒），请检查 base_url 是否可达"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ==================== Reports & Tasks ====================

@app.get("/api/reports")
async def api_list_reports():
    reports = []
    if REPORTS_DIR.exists():
        for f in sorted(REPORTS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            item = {
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "skill_name": "",
                "arguments": "",
                "duration_seconds": 0,
            }
            meta_path = f.with_suffix(".meta.json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    item["skill_name"] = meta.get("skill_name", "")
                    item["arguments"] = meta.get("arguments", "")
                    item["duration_seconds"] = meta.get("duration_seconds", 0)
                except Exception:
                    pass
            if not item["skill_name"]:
                # Legacy reports: derive skill from filename prefix
                item["skill_name"] = f.name.rsplit("_", 2)[0] if "_" in f.name else ""
            reports.append(item)
    return {"reports": reports[:50]}


@app.get("/api/reports/{filename}")
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
    result = {"filename": safe_name, "content": content}
    meta_path = report_path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            result["meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return result


@app.delete("/api/reports/{filename}")
async def api_delete_report(filename: str):
    """Delete a report and its metadata sidecar."""
    safe_name = _sanitize_filename(filename)
    report_path = (REPORTS_DIR / safe_name).resolve()
    try:
        report_path.relative_to(REPORTS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside reports directory")
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    report_path.unlink()
    meta_path = report_path.with_suffix(".meta.json")
    if meta_path.exists():
        meta_path.unlink()
    return {"deleted": safe_name}


@app.get("/api/task/{task_id}")
async def api_task_status(task_id: str):
    status = harness_get_status(task_id)
    if status:
        return status
    return {"error": "Task not found"}


@app.get("/api/tasks")
async def api_list_tasks(status_filter: str = None, limit: int = 50):
    repo = get_repository()
    tasks = repo.list_tasks(limit=limit, status_filter=status_filter)
    out = []
    for t in tasks:
        item = t.model_dump()
        item["billing"] = repo.get_billing(t.task_id)
        out.append(item)
    return {"tasks": out}


@app.post("/api/tools/{tool_name}")
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
