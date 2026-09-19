"""现金流补充资料取数 —— 折旧摊销（D&A）与 FCFE 所需的非现金项。

背景与实测结论（2026-09-13，用户选了"先只补 D&A 取数、不动估值口径"）：
- 平台 `get_financial_data()` 的 `cash_flow`（71 个中文字段）取自现金流量表**正表**，
  正表里本来就没有折旧摊销 —— D&A 在报表的"补充资料"段。
- 实测三个候选源：
    ① 东财现金流接口 `ak.stock_cash_flow_sheet_by_report_em`：
       **有**补充资料段，但列名是英文，平台的中文映射没覆盖，所以此前拿不到。
       可用字段（澜起/茅台/湖南黄金三家均实测有值）：
         FA_IR_DEPR              固定资产折旧、油气资产折耗、生产性生物资产折旧
         IA_AMORTIZE             无形资产摊销
         LPE_AMORTIZE            长期待摊费用摊销
         USERIGHT_ASSET_AMORTIZE 使用权资产摊销
       恒为 nan 的字段：IR_DEPR（投资性房地产折旧）、DEFER_INCOME_AMORTIZE（递延收益摊销）
    ② 新浪 `stock_financial_report_sina(symbol='现金流量表')`：71 列**无**折旧/摊销 → 不可用
    ③ 巨潮：akshare 无现金流财报接口（只有 fund_* 与 stock_zh_a_disclosure_report_cninfo）
       → 走巨潮得自己抓 PDF，成本高，本轮不做
  结论：**东财补充资料段是唯一可用源**。
- ⚠️ 关键坑（决定取数逻辑）：**只有中报(06-30)与年报(12-31)有补充资料**，
  一季报(03-31)与三季报(09-30)该段全为 nan（A 股季报不披露补充资料）。
  所以不能"取最近一期"，必须回退到"最近的有值报告期"，否则拿到一排 nan。
- 合理性交叉验证（实测 2025 年报 ÷ 2024-25 平均余额）：固定资产折旧率
  澜起 13.8% / 茅台 8.5% / 湖南黄金 8.3%；无形资产摊销率 21.6% / 3.3% / 5.2%，
  均在合理区间 → 数值与资产负债表口径自洽。

本模块只取数、不做任何估值计算（FCFE 估值函数属于后续步骤）。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.data_cache import DataCache

logger = logging.getLogger("ai_berkshire.cash_flow_items")

CACHE_CATEGORY = "cash_flow_items"
CACHE_TTL = 30 * 86400          # 30 天：已披露期间的补充资料不会变

# 补充资料段字段（英文列名 → 中文含义）
DA_FIELDS = {
    "FA_IR_DEPR": "固定资产折旧、油气资产折耗、生产性生物资产折旧",
    "IA_AMORTIZE": "无形资产摊销",
    "LPE_AMORTIZE": "长期待摊费用摊销",
    "USERIGHT_ASSET_AMORTIZE": "使用权资产摊销",
}
# 只用中报/年报有值（A 股季报不披露补充资料）
DISCLOSED_MMDD = ("06-30", "12-31")

_cache: Optional[DataCache] = None


def _get_cache() -> DataCache:
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache


def to_em_symbol(symbol: str) -> str:
    """'688008' / '688008.SH' / 'SH688008' → 'SH688008'（东财接口要前缀）。"""
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    if s.startswith(("SH", "SZ", "BJ")) and len(s) == 8:
        return s
    if "." in s:
        code, _, suffix = s.partition(".")
        if len(code) == 6 and code.isdigit() and suffix in ("SH", "SZ", "BJ"):
            return f"{suffix}{code}"
        return ""
    if len(s) == 6 and s.isdigit():
        # 6/9 开头沪市，4/8 北交所，其余深市
        return ("SH" if s[0] in "69" else "BJ" if s[0] in "48" else "SZ") + s
    return ""


def _num(v) -> Optional[float]:
    """只接受真实数值（过滤 nan/None/字符串）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _period_digits(v) -> str:
    """把各种报告期表示归一成 8 位数字。

    东财接口给的是 datetime（'2026-06-30 00:00:00'），平台内部用 '20260630'，
    两种都要认 —— 只截前 8 位会把 datetime 变成 '2026-06-' 而全表被跳过（实测踩过）。
    """
    import re
    return re.sub(r"\D", "", str(v or ""))[:8]


def pick_period(rows: list, disclosed_only: bool = True) -> Optional[dict]:
    """从报表行里挑"最近的有值报告期"。

    rows: [{'REPORT_DATE': '2026-06-30 00:00:00', 'FA_IR_DEPR': 47391994.43, ...}, ...]（新→旧）
    disclosed_only=True 时只在 06-30 / 12-31 里挑（季报该段恒为 nan）。
    返回选中行；无可用行返回 None。
    """
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        digits = _period_digits(r.get("报告日") or r.get("REPORT_DATE"))
        if len(digits) != 8:
            continue
        if disclosed_only and digits[4:8] not in ("0630", "1231"):
            continue
        if any(_num(r.get(k)) is not None for k in DA_FIELDS):
            return r
    return None


def summarize_da(row: dict) -> dict:
    """把一行报表汇总成 D&A 明细（缺的字段不影响合计，nan 不计入）。"""
    items = {}
    for k, label in DA_FIELDS.items():
        v = _num((row or {}).get(k))
        if v is not None:
            items[k] = {"label": label, "value": v}
    total = sum(i["value"] for i in items.values()) or None
    digits = _period_digits((row or {}).get("报告日") or (row or {}).get("REPORT_DATE"))
    return {
        "period": f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) == 8 else "",
        "items": items,
        "total": total,
        "fields_present": sorted(items.keys()),
    }


def get_depreciation_amortization(symbol: str, use_cache: bool = True) -> dict:
    """取最近一期（中报/年报）的折旧摊销合计。

    返回 {ok, symbol, period, total, items, source, note, cached}；
    ok=False 时 note 写明原因（非 A 股 / 接口无返回 / 无有值期间），total 为 None。
    """
    out = {
        "ok": False, "symbol": symbol or "", "period": "", "total": None,
        "items": {}, "source": "", "note": "", "cached": False,
    }
    em = to_em_symbol(symbol)
    if not em:
        out["note"] = "仅支持 A 股 6 位代码（沪深京）；港股/美股暂无补充资料源"
        return out
    out["symbol"] = em
    cache = _get_cache()
    key = f"da|{em}"
    if use_cache:
        try:
            hit = cache.get(CACHE_CATEGORY, key, max_age_seconds=CACHE_TTL)
            if isinstance(hit, dict) and hit.get("ok"):
                hit["cached"] = True
                return hit
        except Exception as e:  # noqa: BLE001
            logger.debug(f"D&A 缓存读取失败: {e}")

    try:
        import akshare as ak
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=em)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"D&A 取数失败（{em}）: {type(e).__name__}: {str(e)[:120]}")
        out["note"] = f"东财现金流接口取数失败：{type(e).__name__}"
        return out

    if df is None or len(df) == 0:
        out["note"] = "东财现金流接口返回空表"
        return out

    rows = df.to_dict("records")
    picked = pick_period(rows, disclosed_only=True)
    if picked is None:
        out["note"] = "无有值的中报/年报期间（补充资料段仅中报与年报披露）"
        return out

    summary = summarize_da(picked)
    if not summary["total"]:
        out["note"] = "选中期间的折旧摊销字段全为 nan"
        return out

    out.update({
        "ok": True, "period": summary["period"], "total": summary["total"],
        "items": summary["items"], "source": "eastmoney_cash_flow_supplement",
        "note": "",
    })
    try:
        cache.set(CACHE_CATEGORY, key, {k: v for k, v in out.items() if k != "cached"})
    except Exception as e:  # noqa: BLE001
        logger.debug(f"D&A 缓存写入失败: {e}")
    return out


async def aget_depreciation_amortization(symbol: str, use_cache: bool = True) -> dict:
    import asyncio
    return await asyncio.to_thread(get_depreciation_amortization, symbol, use_cache)
