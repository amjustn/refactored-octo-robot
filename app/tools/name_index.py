"""全A股股票名称索引 —— 硬编码映射表（143 条）的远程兜底。

背景（2026-09-13 实测，见 scripts/verify_batch1.py）：
- app/tools/market_data.py 的 COMPANY_NAME_MAP 只有 143 条（其中 A 股 71 条），
  沪深300 成分股里按全名能命中的只有 42/300 = 14%。
- 用户用"澜起科技""长鑫科技"这类表外公司名提问时，标的解析返回 None →
  行情/新闻/券商研报块全部不注入，决策留痕写 no_credible_stock_name，
  报告只能靠模型内部知识写（实测 12:48 的 management-deep-dive 报告开篇
  就是"2025 年年报、2026 年半年报应已披露，但具体数字需要核实"）。
- akshare.stock_info_a_code_name() 实测返回 5562 条（冷启动约 24 秒），
  按日缓存进 SQLite（DataCache），之后查表是纯内存操作。

设计约束（防止引入新的错绑）：
- 只接受"全名子串"匹配，且名称长度 >= 4 字，不做简称模糊匹配。
  实测反例："苹果折叠屏产业链" 会因子串命中硬编码表里的"苹果"绑到 AAPL，
  索引里的名称都是 >=4 字的正式简称，不会出现这种误伤。
- 同一名称命中多个不同代码（ST 改名等）时判为歧义，返回 None 并记日志，
  宁可让上下文缺数据，也不注入错标的的数据。
- 数据源不可用时返回空表（并记 warning），绝不静默失败、绝不编造。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.data_cache import DataCache

logger = logging.getLogger("ai_berkshire.name_index")

_CACHE_CATEGORY = "name_index"
_CACHE_KEY = "a_share_v1"
_CACHE_TTL = 86400          # 正常按日刷新
_STALE_TTL = 30 * 86400     # 网络失败时允许用 30 天内的旧表兜底

_MIN_NAME_LEN = 4           # 名称长度下限，低于此不做子串匹配（防错绑）

_cache: Optional[DataCache] = None
_name_map: Optional[dict] = None      # name -> symbol
_sorted_names: Optional[tuple] = None  # 按长度倒序的名称元组（长名优先匹配）


def _get_cache() -> DataCache:
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache


def _normalize_a_code(code: str) -> str:
    """6 位纯数字代码 → 带交易所后缀的标的代码。

    6/9 开头 → .SH（6 主板+科创板、9 B股）
    0/2/3 开头 → .SZ（0 主板、2 B股、3 创业板）
    4/8 开头 → .BJ（北交所）
    """
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _fetch_remote_name_map() -> dict:
    """从 akshare 拉全A名称表；失败返回 {} 并记 warning。"""
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        if df is None or len(df) == 0:
            logger.warning("name_index: akshare 返回空名称表")
            return {}
        out = {}
        for _, row in df.iterrows():
            name = str(row.get("name", "") or "").strip()
            code = str(row.get("code", "") or "").strip()
            if not name or not code or not code.isdigit() or len(code) != 6:
                continue
            out[name] = _normalize_a_code(code)
        logger.info(f"name_index: 拉取全A名称表 {len(out)} 条")
        return out
    except Exception as e:
        logger.warning(f"name_index: 拉取全A名称表失败: {type(e).__name__}: {e}")
        return {}


def get_name_map(force_refresh: bool = False) -> dict:
    """返回 {股票简称: 代码} 映射（按日缓存，失败可用 30 天旧表兜底）。"""
    global _name_map, _sorted_names
    if _name_map is not None and not force_refresh:
        return _name_map

    cache = _get_cache()
    if not force_refresh:
        try:
            cached = cache.get(_CACHE_CATEGORY, _CACHE_KEY, max_age_seconds=_CACHE_TTL)
            if isinstance(cached, dict) and cached:
                _name_map = cached
                _sorted_names = tuple(sorted(cached.keys(), key=len, reverse=True))
                logger.debug(f"name_index: 命中当日缓存 {len(cached)} 条")
                return _name_map
        except Exception as e:
            logger.debug(f"name_index: 缓存读取失败: {e}")

    fresh = _fetch_remote_name_map()
    if fresh:
        try:
            cache.set(_CACHE_CATEGORY, _CACHE_KEY, fresh)
        except Exception as e:
            logger.debug(f"name_index: 缓存写入失败: {e}")
        _name_map = fresh
        _sorted_names = tuple(sorted(fresh.keys(), key=len, reverse=True))
        return _name_map

    # 远程失败 → 用 30 天内的旧表兜底（绝不返回半成品或编造内容）
    try:
        stale = cache.get(_CACHE_CATEGORY, _CACHE_KEY, max_age_seconds=_STALE_TTL)
        if isinstance(stale, dict) and stale:
            logger.warning(f"name_index: 远程拉取失败，用旧表兜底（{len(stale)} 条）")
            _name_map = stale
            _sorted_names = tuple(sorted(stale.keys(), key=len, reverse=True))
            return _name_map
    except Exception as e:
        logger.debug(f"name_index: 旧表兜底失败: {e}")

    logger.warning("name_index: 名称索引不可用（远程失败且无旧表）")
    _name_map = {}
    _sorted_names = ()
    return _name_map


def lookup(name: str) -> Optional[str]:
    """精确全名查询：'澜起科技' → '688008.SH'。查不到返回 None。"""
    if not name:
        return None
    nm = str(name).strip()
    if len(nm) < 2:
        return None
    return get_name_map().get(nm)


def resolve_by_name(text: str, min_len: int = _MIN_NAME_LEN) -> Optional[dict]:
    """在自由文本里按全名子串找标的（长名优先）。

    返回 {"symbol", "name", "source"}；未命中或多义（同名多代码）返回 None。
    """
    if not text:
        return None
    text = str(text)
    if len(text) < min_len:
        return None

    m = get_name_map()
    if not m:
        return None
    names = _sorted_names if _sorted_names is not None else tuple(sorted(m.keys(), key=len, reverse=True))

    hits = []  # (name, symbol)
    for nm in names:
        if len(nm) < min_len:
            break  # 已按长度倒序，后面的都更短
        if nm in text:
            hits.append((nm, m[nm]))
            if len(hits) > 4:
                break
    if not hits:
        return None

    # 长名优先：取最长命中；若同长度命中多个不同标的 → 歧义，拒绝
    longest = max(len(nm) for nm, _ in hits)
    tops = {(nm, sym) for nm, sym in hits if len(nm) == longest}
    symbols = {sym for _, sym in tops}
    if len(symbols) > 1:
        logger.warning(f"name_index: 名称多义，拒绝解析 {sorted(tops)} in {text[:40]!r}")
        return None
    nm, sym = next(iter(tops))
    return {"symbol": sym, "name": nm, "source": "name_index"}


def reverse_lookup(symbol: str) -> Optional[str]:
    """代码 → 股票简称（用于把 688008.SH 变回"澜起科技"喂给搜索/上下文）。"""
    if not symbol:
        return None
    sym = str(symbol).strip().upper()
    bare = sym.split(".")[0]
    for name, code in get_name_map().items():
        if code.upper() == sym or code.split(".")[0] == bare:
            return name
    return None


def stats() -> dict:
    """自检用：索引规模与来源状态。"""
    m = get_name_map()
    return {
        "count": len(m),
        "suffixes": {
            ".SH": sum(1 for c in m.values() if c.endswith(".SH")),
            ".SZ": sum(1 for c in m.values() if c.endswith(".SZ")),
            ".BJ": sum(1 for c in m.values() if c.endswith(".BJ")),
        },
        "cache_category": _CACHE_CATEGORY,
        "cache_key": _CACHE_KEY,
        "ttl_seconds": _CACHE_TTL,
        "min_name_len": _MIN_NAME_LEN,
    }
