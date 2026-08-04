"""QuantTrader (QT) 量化信号桥接 — 只读研究工具。

通过本机 HTTP 调用 sibling 项目 QuantTrader (默认 http://127.0.0.1:8000)
的只读 GET 接口，把多因子信号、全市场因子评分、底部温度计、每日复盘
暴露为 AI Berkshire 的研究工具，供 Agent 做交叉验证。

配置（env，见 app/core/config.py）：
  QT_BASE_URL   QT 服务地址，默认 http://127.0.0.1:8000
  QT_API_TOKEN  Bearer Token；缺省时回退读取 QT_TOKEN_FILE
  QT_TOKEN_FILE Token 文件，默认 /home/q/quant_trader/data/api_token.txt

Token 解析顺序：环境变量 QT_API_TOKEN -> QT_TOKEN_FILE -> 本机免认证的
/api/auth/token（仅 127.0.0.1 可达）。解析结果在模块级缓存。

QT 不可达、Token 缺失或接口报错时抛出 QuantLinkError（ToolGateway /
_execute_tool 负责把异常转成 {"error": ...} 兜底文案），绝不崩溃调用方。
"""
import logging
import os
from typing import Optional

import httpx

from ..core import config as _config

logger = logging.getLogger("ai_berkshire.quant_link")

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TOKEN_FILE = "/home/q/quant_trader/data/api_token.txt"


class QuantLinkError(RuntimeError):
    """QuantTrader 桥接失败（不可达 / 未授权 / 接口报错）。"""


# Shared async HTTP client (lazy-initialized)
_http_client: Optional[httpx.AsyncClient] = None
_TOKEN: Optional[str] = None  # 模块级 token 缓存；"" 表示已确认无 token


async def get_client() -> httpx.AsyncClient:
    """Get or create the shared httpx async client."""
    global _http_client
    if _http_client is not None:
        try:
            if _http_client.is_closed:
                _http_client = None
        except Exception:
            _http_client = None
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": "AIBerkshire-QuantLink/1.0"},
        )
    return _http_client


async def close_client():
    """Close the shared HTTP client (call on shutdown)."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        try:
            await _http_client.aclose()
        except Exception:
            pass
    _http_client = None


def _base_url() -> str:
    return (os.getenv("QT_BASE_URL", "").strip()
            or getattr(_config, "QT_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")


def _token_file() -> str:
    return (os.getenv("QT_TOKEN_FILE", "").strip()
            or getattr(_config, "QT_TOKEN_FILE", "") or DEFAULT_TOKEN_FILE)


async def _get_token() -> Optional[str]:
    """解析 QT API Token：env -> token 文件 -> 本机 /api/auth/token。模块级缓存。"""
    global _TOKEN
    if _TOKEN is not None:
        return _TOKEN or None

    env_token = os.getenv("QT_API_TOKEN", "").strip() or (getattr(_config, "QT_API_TOKEN", None) or "")
    if env_token:
        _TOKEN = env_token
        return _TOKEN

    token_file = _token_file()
    try:
        if os.path.exists(token_file):
            with open(token_file) as f:
                token = f.read().strip()
            if token:
                _TOKEN = token
                return _TOKEN
    except Exception as e:
        logger.warning(f"读取 QT token 文件失败 ({token_file}): {e}")

    # 最后回退：QT 的 /api/auth/token 免认证端点（仅允许本机访问）
    try:
        client = await get_client()
        resp = await client.get(f"{_base_url()}/api/auth/token", timeout=5.0)
        data = resp.json()
        token = (data.get("data") or {}).get("token", "")
        if resp.status_code == 200 and token:
            _TOKEN = token
            return _TOKEN
    except Exception as e:
        logger.debug(f"QT /api/auth/token 获取失败: {e}")

    _TOKEN = ""
    return None


def reset_token_cache():
    """清空 token 缓存（测试或服务端 token 轮换后调用）。"""
    global _TOKEN
    _TOKEN = None


async def _get(path: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
    """调用 QT 只读 GET 接口，返回 data 字段；失败抛 QuantLinkError。"""
    global _TOKEN
    base = _base_url()

    token = await _get_token()
    if not token:
        raise QuantLinkError(
            "QuantTrader 未授权：未找到 API Token（请设置 QT_API_TOKEN，"
            f"或确认 token 文件存在: {_token_file()}）"
        )

    try:
        client = await get_client()
        resp = await client.get(
            f"{base}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise QuantLinkError(f"QuantTrader 服务不可达 ({base})：{e}") from e
    except httpx.TimeoutException as e:
        raise QuantLinkError(f"QuantTrader 请求超时 ({base}{path})：{e}") from e
    except httpx.HTTPError as e:
        raise QuantLinkError(f"QuantTrader 请求失败 ({base}{path})：{e}") from e

    if resp.status_code == 401:
        reset_token_cache()  # token 可能已轮换，下次重新解析
        raise QuantLinkError("QuantTrader 返回 401：API Token 无效或已过期（请检查 QT_API_TOKEN）")
    if resp.status_code != 200:
        raise QuantLinkError(f"QuantTrader 接口异常 ({path})：HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except Exception as e:
        raise QuantLinkError(f"QuantTrader 返回非 JSON ({path})：{e}") from e

    if payload.get("code") != 0:
        raise QuantLinkError(f"QuantTrader 接口报错 ({path})：{payload.get('message', '未知错误')}")
    return payload.get("data") or {}


def _round_factors(factors: dict) -> dict:
    out = {}
    for k, v in (factors or {}).items():
        try:
            out[k] = round(float(v), 4)
        except (TypeError, ValueError):
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════
#  1) 多因子复合信号 — GET /api/factors/signal
# ═══════════════════════════════════════════════════════

async def get_factor_signal(codes: str) -> dict:
    """获取个股多因子复合量化信号（QT FactorSignalEngine，19 个信号因子加权）。

    codes: 逗号分隔的 A 股代码（如 "600519,000001"），QT 侧上限 50 只。
    返回每只股票 action(buy/sell/hold)、加权得分、置信度、触发理由与因子明细。
    """
    codes = (codes or "").strip()
    if not codes:
        raise QuantLinkError("quant_factor_signal 需要至少一个股票代码（逗号分隔）")

    data = await _get("/api/factors/signal", params={"codes": codes}, timeout=60.0)

    results = []
    for r in data.get("results", []):
        results.append({
            "code": r.get("code"),
            "action": r.get("action"),
            "score": r.get("score"),
            "confidence": r.get("confidence"),
            "reasons": r.get("reasons", []),
            "alerts": r.get("alerts", []),
            "factors": _round_factors(r.get("factors", {})),
            "factor_trend": r.get("factor_trend", {}),
        })
    return {
        "summary": data.get("summary", {}),
        "alerts": data.get("alerts", []),
        "results": results,
        "_source": "quant_trader",
    }


# ═══════════════════════════════════════════════════════
#  2) 全市场因子评分 — GET /api/factors/market-scores
# ═══════════════════════════════════════════════════════

async def get_market_scores() -> dict:
    """获取 QT 全市场因子评分快照（服务端每日缓存）：评分分布、8 个维度
    均分、Top 买入/卖出名单。可用于了解量化视角的市场强弱与候选股。"""
    data = await _get("/api/factors/market-scores", timeout=20.0)
    if data.get("date") is None:
        raise QuantLinkError(f"QuantTrader 暂无全市场评分数据：{data.get('message', '未知')}")

    def _trim(rows):
        return [{
            "code": r.get("code"),
            "score": r.get("score"),
            "action": r.get("action"),
            "confidence": r.get("confidence"),
            "reasons": r.get("reasons", []),
        } for r in (rows or [])]

    return {
        "date": data.get("date"),
        "updated_at": data.get("updated_at"),
        "total": data.get("total"),
        "scored": data.get("scored"),
        "buy_count": data.get("buy_count"),
        "sell_count": data.get("sell_count"),
        "hold_count": data.get("hold_count"),
        "dimension_scores": data.get("dimension_scores", {}),
        "top_buys": _trim(data.get("top_buys")),
        "top_sells": _trim(data.get("top_sells")),
        "universe_size": data.get("universe_size"),
        "_source": "quant_trader",
    }


# ═══════════════════════════════════════════════════════
#  3) 底部温度计 — GET /api/bottom/status
# ═══════════════════════════════════════════════════════

async def get_bottom_thermometer() -> dict:
    """获取 QT 底部温度计：ERP/PB分位/维持担保比例/成交量分位 4 维底部信号
    触发情况 + E8 入场判定（两融回落 C 条件 AND ERP 估值 V 条件）。"""
    data = await _get("/api/bottom/status", timeout=30.0)

    e8 = data.get("E8", {})
    return {
        "latest_date": data.get("latest_date"),
        "signals": data.get("signals", {}),
        "dimensions": data.get("dimensions", {}),
        "trigger_count": data.get("trigger_count"),
        "trigger_summary": data.get("trigger_summary"),
        "E8": {
            "entry": e8.get("entry"),
            "entry_rule": e8.get("entry_rule"),
            "status": e8.get("status"),
            "C_triggered": e8.get("C_triggered"),
            "C_decline_pct": e8.get("C_decline_pct"),
            "C_threshold": e8.get("C_threshold"),
            "C_distance_pp": e8.get("C_distance_pp"),
            "V_triggered": e8.get("V_triggered"),
            "V_ERP": e8.get("V_ERP"),
            "V_ERP_roll_pct": e8.get("V_ERP_roll_pct"),
        },
        "_source": "quant_trader",
    }


# ═══════════════════════════════════════════════════════
#  4) 每日复盘 — GET /api/review/market-overview
# ═══════════════════════════════════════════════════════

async def get_daily_review(date: str = "today") -> dict:
    """获取 QT 每日复盘总览：大盘状态、市场宽度（涨跌家数/涨跌停）、
    板块机会与风险、一句话总结。date: today/yesterday/YYYY-MM-DD。"""
    date = (date or "today").strip()
    data = await _get("/api/review/market-overview", params={"d": date}, timeout=30.0)
    return {
        "date": data.get("date"),
        "market_regime": data.get("market_regime"),
        "market_regime_label": data.get("market_regime_label"),
        "index": data.get("index", {}),
        "breadth": data.get("breadth", {}),
        "sectors": data.get("sectors", {}),
        "summary": data.get("summary", ""),
        "_source": "quant_trader",
    }
