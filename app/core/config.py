"""AI Berkshire Web - Core Configuration v3.0

Added data source configuration for market data and knowledge base.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent.parent
SKILLS_DIR = BASE_DIR / "app" / "skills"
REPORTS_DIR = BASE_DIR / "data" / "reports"
LOGS_DIR = BASE_DIR / "logs"
KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge"

# LLM Configuration
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# DeepSeek specific
DEEPSEEK_API_KEY = LLM_API_KEY
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Models the server's gateway actually supports (used to validate user overrides
# when they rely on the server key, and to build the frontend model picker).
# Each entry verified via /v1/models API + /chat/completions smoke test (2026-08-07).
_DEFAULT_MODELS = (
    # DeepSeek (api.deepseek.com)
    "deepseek-v4-pro,deepseek-v4-flash,"
    # Kimi / Moonshot (api.moonshot.cn)
    "kimi-k2.5,kimi-k2.6,kimi-k2.7-code,kimi-k2.7-code-highspeed,kimi-k3,"
    # Zhipu / GLM (open.bigmodel.cn)
    "glm-4.5,glm-4.5-air,glm-4.6,glm-4.7,glm-5,glm-5-turbo,glm-5.1,glm-5.2"
)
LLM_SUPPORTED_MODELS = [
    m.strip() for m in os.getenv("LLM_SUPPORTED_MODELS", _DEFAULT_MODELS).split(",") if m.strip()
]

# Agent settings
MAX_PARALLEL_AGENTS = 4
# 单个 Agent 的墙钟上限。2026-09-19 由 300 提到 600：investment-team 这类
# 「4 个 Agent 各自全量取数 + 交叉验证」的任务，实测单 Agent 20+ 次工具调用、
# 中间还夹 1.5-2.5 分钟空档，300 秒必被掐（task 9d717fe98c67 四个 Agent 全超时）。
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "600"))  # seconds
# 单次 LLM 请求超时（与 Agent 总预算分开）：上游 hang 住时不该把整个 Agent 的
# 预算耗在一次请求上，短超时 + 既有 3 次重试更划算。
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "120"))  # seconds
MAX_REPORT_LENGTH = 70000  # chars（2026-09-19 由 5 万提到 7 万：工具上限放宽后报告变长，6.1 万字符的报告曾被砍尾）
MAX_REPORT_AGE_DAYS = 90
# Global cap on simultaneously running research tasks; extra runs get a
# "busy" error immediately instead of queueing (protects the LLM budget).
MAX_CONCURRENT_RUNS = int(os.getenv("BERKSHIRE_MAX_CONCURRENT_RUNS", "2"))

# Market Data Configuration
# Data libraries: akshare (A-shares), yfinance (US/HK stocks)
DATA_CACHE_TTL_REALTIME = int(os.getenv("DATA_CACHE_TTL_REALTIME", "300"))  # 5 min
DATA_CACHE_TTL_FINANCIAL = int(os.getenv("DATA_CACHE_TTL_FINANCIAL", "86400"))  # 24 hours
DATA_CACHE_TTL_NEWS = int(os.getenv("DATA_CACHE_TTL_NEWS", "3600"))  # 1 hour
GOAL_PARSE_CACHE_TTL = int(os.getenv("GOAL_PARSE_CACHE_TTL", "86400"))  # 24 hours
# Bounded thread pool for sync SDK calls (akshare/yfinance). asyncio.wait_for
# cancels the await but cannot kill the underlying thread, so hung fetches
# leave orphaned threads behind; a dedicated small pool caps that accumulation
# and isolates it from the event loop's default executor.
MARKET_SYNC_WORKERS = int(os.getenv("MARKET_SYNC_WORKERS", "6"))

# QuantTrader (QT) quant-signal service — sibling FastAPI project on the same
# host, exposes read-only quant signals consumed by app/tools/quant_link.py.
QT_BASE_URL = os.getenv("QT_BASE_URL", "http://127.0.0.1:8000")
QT_API_TOKEN = os.getenv("QT_API_TOKEN", "").strip() or None
QT_TOKEN_FILE = os.getenv("QT_TOKEN_FILE", "/home/q/quant_trader/data/api_token.txt")

# Knowledge Base Configuration
KNOWLEDGE_REFRESH_INTERVAL = int(os.getenv("KNOWLEDGE_REFRESH_INTERVAL", "604800"))  # 7 days

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))

# Optional API token — when set, ALL /api/* + WebSocket endpoints require
# `?token=<value>` or a JSON `token` field.  Protects your server key when
# the service is reachable from untrusted networks.
BERKSHIRE_API_TOKEN = os.getenv("BERKSHIRE_API_TOKEN", "").strip() or None

# Ensure directories exist
for d in [REPORTS_DIR, LOGS_DIR, KNOWLEDGE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Logging configuration
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOGS_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("ai_berkshire")
logger.info(f"AI Berkshire Web v3.0.0 configured — PORT={PORT}, MODEL={LLM_MODEL}")
