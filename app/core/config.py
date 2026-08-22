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
AGENT_TIMEOUT = 300  # seconds
MAX_REPORT_LENGTH = 50000  # chars
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
