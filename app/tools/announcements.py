"""公司公告 / 定期报告 / 高管持股变动数据源。

为投研分析提供个股的「公司层面事件」数据，供上下文注入使用：

1. get_disclosure_reports(symbol, kind="", limit=8)
   定期报告（年报 / 半年报 / 一季报 / 三季报）— **巨潮资讯 cninfo**（官方指定披露平台）
   数据源: akshare ``stock_zh_a_disclosure_report_cninfo``
   实测列名: ``['代码', '简称', '公告标题', '公告时间', '公告链接']``
   返回: ``[{kind, title, date, url}]``，按公告时间倒序。

2. get_company_notices(symbol, limit=10)
   公司全部公告（含调研活动、权益变动、H股公告等）— **东方财富**
   数据源: akshare ``stock_individual_notice_report``
   实测列名: ``['代码', '名称', '公告标题', '公告类型', '公告日期', '网址']``
   返回: ``[{title, date, url}]``，按公告日期倒序（列名自适应，字段名不同则在日志/标准输出打印出来核对）。

3. get_management_changes(symbol, limit=15)
   高管/核心技术人员持股变动 — **同花顺**
   数据源: akshare ``stock_management_change_ths``
   实测列名: ``['变动日期', '变动人', '与公司高管关系', '变动数量', '交易均价', '剩余股数', '股份变动途径']``
   返回: 最近 limit 条（按变动日期倒序），**字典键名保持中文列名原样**。

4. format_announcement_context(symbol, company_name="")
   把以上三块拼成给大模型看的注入文本块（≤4000 字符，超长按重要性截断）。
   非 A 股（港股/美股）直接返回 ""——巨潮/东财个股公告接口均不覆盖非 A 股，绝不编造公告。

缓存: SQLite ``DataCache``，category 分别为
  - ``announcements_disclosure``  TTL 21600s (6h)  — 定期报告变动极慢
  - ``announcements_notices``     TTL 3600s  (1h)  — 公告更新较快
  - ``announcements_mgmt``        TTL 86400s (24h) — 高管变动低频

设计约束（与仓库一致）:
  * 纯同步函数，不依赖事件循环。
  * 失败返回空列表 / 空串，并 ``logger.warning`` 写清原因——**绝不静默失败，绝不编造数据**。
  * 缓存 value 全部为可 JSON 序列化的 Python 原生结构（已做 numpy → 原生转换）。

用法::

    from app.tools.announcements import format_announcement_context
    block = format_announcement_context("688008", company_name="澜起科技")
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from ..core.data_cache import DataCache

logger = logging.getLogger("ai_berkshire.announcements")

_cache = DataCache()

# ── 缓存 category / TTL ──────────────────────────────────────────────
CAT_DISCLOSURE = "announcements_disclosure"
CAT_NOTICES = "announcements_notices"
CAT_MGMT = "announcements_mgmt"

TTL_DISCLOSURE = 21600   # 6h
TTL_NOTICES = 3600       # 1h
TTL_MGMT = 86400         # 24h

# 定期报告类型（kind="" 时按此顺序全部拉取后合并）
_DISCLOSURE_KINDS = ("年报", "半年报", "一季报", "三季报")

# 定期报告起始日期（覆盖最近 2~3 个报告期）
_DISCLOSURE_START = "20240101"

# format_announcement_context 输出上限
_MAX_CHARS = 4000

# 只打印一次列名（核对列名用，避免刷屏）
_COLS_LOGGED: set = set()


# ═══════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════

def normalize_symbol(symbol: str) -> str:
    """把常见代码形式归一为 6 位纯数字 A 股代码。

    - ``"688008"`` / ``"688008.SH"`` / ``"sh688008"`` / ``"SH688008"`` → ``"688008"``
    - ``"000001.SZ"`` / ``"sz000001"``                               → ``"000001"``
    - 港股 ``"00700.HK"`` / 美股 ``"AAPL"`` / 空 / 非法               → ``""``

    返回 ``""`` 表示该数据源不覆盖（调用方据此短路，不发起网络请求）。
    """
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    if not s:
        return ""
    # 去交易所后缀（.SH / .SZ / .BJ）
    s = re.sub(r"\.(SH|SZ|BJ)$", "", s)
    # 去交易所前缀（SH / SZ / BJ，允许后跟一个点）
    s = re.sub(r"^(SH|SZ|BJ)\.?", "", s)
    if re.fullmatch(r"\d{6}", s):
        return s
    return ""


def _log_columns(source: str, columns: list, preferred: dict) -> None:
    """打印/记录数据源实际列名与我们的映射（列名以实测为准，便于核对）。"""
    if source in _COLS_LOGGED:
        return
    _COLS_LOGGED.add(source)
    msg = f"[{source}] 实际列名={columns}；字段映射={preferred}"
    # 列名以实测为准：打印一次供人工核对（进程内去重，避免刷屏）
    print(msg, flush=True)
    logger.info(msg)


def _resolve_col(df, *candidates: str) -> Optional[str]:
    """在 DataFrame 列中按候选顺序找到第一个存在的列名。"""
    try:
        cols = [str(c) for c in list(getattr(df, "columns", []))]
    except Exception:
        cols = []
    for name in candidates:
        if name in cols:
            return name
    return None


def _cell(v: Any) -> Any:
    """把 DataFrame 单元格转成 JSON 可序列化的 Python 原生值（NaN → ""）。"""
    if v is None:
        return ""
    # numpy / pandas 标量
    item = getattr(v, "item", None)
    if callable(item):
        try:
            v = v.item()
        except Exception:
            pass
    try:
        import pandas as pd  # noqa: PLC0415

        if pd.isna(v):
            return ""
    except Exception:
        pass
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _to_str(v: Any) -> str:
    """单元格 → 去空白字符串（None/NaN → ""）。"""
    c = _cell(v)
    if c is None or (isinstance(c, float) and c != c):
        return ""
    return str(c).strip()


def _date10(v: Any) -> str:
    """把日期/时间截断成 YYYY-MM-DD（用于排序与展示）。"""
    s = _to_str(v)
    return s[:10]


def _empty_frame(df) -> bool:
    return df is None or bool(getattr(df, "empty", True))


# ═══════════════════════════════════════════════════════
#  1) 定期报告 — 巨潮资讯
# ═══════════════════════════════════════════════════════

def _fetch_disclosure(kind: str, code: str, start_date: str, end_date: str) -> list:
    """拉取单一定期报告类型（巨潮资讯）。失败返回 []。"""
    try:
        import akshare as ak  # noqa: PLC0415
    except Exception as e:  # pragma: no cover - 依赖缺失
        logger.warning(f"akshare 不可用，无法拉取定期报告: {e}")
        return []

    try:
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code,
            market="沪深京",
            category=kind,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as e:
        logger.warning(f"定期报告接口失败 kind={kind} symbol={code}: {type(e).__name__}: {e}")
        return []

    if _empty_frame(df):
        logger.info(f"定期报告为空 kind={kind} symbol={code}")
        return []

    c_title = _resolve_col(df, "公告标题", "title")
    c_date = _resolve_col(df, "公告时间", "公告日期", "date")
    c_url = _resolve_col(df, "公告链接", "网址", "url", "link")
    _log_columns(
        "巨潮资讯·定期报告",
        list(df.columns),
        {"title": c_title, "date": c_date, "url": c_url},
    )
    if not c_title or not c_date:
        logger.warning(f"定期报告列名无法识别，实际列={list(df.columns)}")
        return []

    out = []
    for _, row in df.iterrows():
        title = _to_str(row.get(c_title))
        if not title:
            continue
        out.append({
            "kind": kind,
            "title": title,
            "date": _date10(row.get(c_date)),
            "url": _to_str(row.get(c_url)) if c_url else "",
        })
    return out


def get_disclosure_reports(symbol: str, kind: str = "", limit: int = 8) -> list:
    """获取个股定期报告（巨潮资讯）。

    Args:
        symbol: 代码（任意常见形式，见 normalize_symbol）
        kind:  ``"年报"`` / ``"半年报"`` / ``"一季报"`` / ``"三季报"``；
               为空则依次拉取四类并合并。
        limit: 返回条数上限（合并后按公告时间倒序取前 limit）。

    Returns:
        ``[{kind, title, date, url}]``；非 A 股 / 失败 → ``[]``（失败已在日志写明原因）。
    """
    code = normalize_symbol(symbol)
    if not code:
        logger.info(f"定期报告：非 A 股或不识别代码 {symbol!r}，巨潮仅覆盖 A 股，返回空")
        return []

    kind = (kind or "").strip()
    kinds = [kind] if kind else list(_DISCLOSURE_KINDS)
    end_date = datetime.now().strftime("%Y%m%d")
    cache_key = f"{code}:{kind or 'all'}:{limit}:{_DISCLOSURE_START}:{end_date}"

    cached = _cache.get(CAT_DISCLOSURE, cache_key, max_age_seconds=TTL_DISCLOSURE)
    if cached is not None:
        return cached

    rows: list = []
    for k in kinds:
        rows.extend(_fetch_disclosure(k, code, _DISCLOSURE_START, end_date))

    # 按公告时间倒序（同日期保持稳定）
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    if limit and limit > 0:
        rows = rows[:limit]

    if not rows:
        logger.warning(f"定期报告：symbol={code} kinds={kinds} 未获取到任何数据（接口空返回或失败）")

    _cache.set(CAT_DISCLOSURE, cache_key, rows)
    return rows


# ═══════════════════════════════════════════════════════
#  2) 公司公告 — 东方财富
# ═══════════════════════════════════════════════════════

def get_company_notices(symbol: str, limit: int = 10) -> list:
    """获取个股全部公告（东方财富）。

    Args:
        symbol: 代码（见 normalize_symbol）
        limit:  返回最近 limit 条

    Returns:
        ``[{title, date, url}]``（按公告日期倒序）；非 A 股 / 失败 → ``[]``。
        列名以实测为准，本函数自适应常见别名并打印实际列名以便核对。
    """
    code = normalize_symbol(symbol)
    if not code:
        logger.info(f"公司公告：非 A 股或不识别代码 {symbol!r}，返回空")
        return []

    cache_key = f"{code}:{limit}"
    cached = _cache.get(CAT_NOTICES, cache_key, max_age_seconds=TTL_NOTICES)
    if cached is not None:
        return cached

    try:
        import akshare as ak  # noqa: PLC0415
    except Exception as e:  # pragma: no cover
        logger.warning(f"akshare 不可用，无法拉取公司公告: {e}")
        return []

    try:
        df = ak.stock_individual_notice_report(security=code, symbol="全部")
    except Exception as e:
        logger.warning(f"公司公告接口失败 symbol={code}: {type(e).__name__}: {e}")
        return []

    if _empty_frame(df):
        logger.warning(f"公司公告为空 symbol={code}")
        return []

    c_title = _resolve_col(df, "公告标题", "标题", "title")
    c_date = _resolve_col(df, "公告日期", "公告时间", "日期", "date")
    c_url = _resolve_col(df, "网址", "公告链接", "url", "link")
    _log_columns(
        "东方财富·公司公告",
        list(df.columns),
        {"title": c_title, "date": c_date, "url": c_url},
    )
    if not c_title or not c_date:
        logger.warning(f"公司公告列名无法识别，实际列={list(df.columns)}")
        return []

    out = []
    for _, row in df.iterrows():
        title = _to_str(row.get(c_title))
        if not title:
            continue
        out.append({
            "title": title,
            "date": _date10(row.get(c_date)),
            "url": _to_str(row.get(c_url)) if c_url else "",
        })

    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    if limit and limit > 0:
        out = out[:limit]

    if not out:
        logger.warning(f"公司公告：symbol={code} 无有效记录")

    _cache.set(CAT_NOTICES, cache_key, out)
    return out


# ═══════════════════════════════════════════════════════
#  3) 高管持股变动 — 同花顺
# ═══════════════════════════════════════════════════════

def get_management_changes(symbol: str, limit: int = 15) -> list:
    """获取高管/核心技术人员持股变动（同花顺）。

    Returns:
        最近 limit 条（按变动日期倒序），**字典键名保持中文列名原样**：
        ``变动日期 / 变动人 / 与公司高管关系 / 变动数量 / 交易均价 / 剩余股数 / 股份变动途径``。
        非 A 股 / 失败 → ``[]``。
    """
    code = normalize_symbol(symbol)
    if not code:
        logger.info(f"高管持股变动：非 A 股或不识别代码 {symbol!r}，返回空")
        return []

    cache_key = f"{code}:{limit}"
    cached = _cache.get(CAT_MGMT, cache_key, max_age_seconds=TTL_MGMT)
    if cached is not None:
        return cached

    try:
        import akshare as ak  # noqa: PLC0415
    except Exception as e:  # pragma: no cover
        logger.warning(f"akshare 不可用，无法拉取高管持股变动: {e}")
        return []

    try:
        df = ak.stock_management_change_ths(symbol=code)
    except Exception as e:
        logger.warning(f"高管持股变动接口失败 symbol={code}: {type(e).__name__}: {e}")
        return []

    if _empty_frame(df):
        logger.info(f"高管持股变动为空 symbol={code}")
        return []

    cols = list(df.columns)
    c_date = _resolve_col(df, "变动日期", "日期")
    _log_columns("同花顺·高管持股变动", cols, {"date": c_date, "keys": cols})
    if not c_date:
        logger.warning(f"高管持股变动列名无法识别，实际列={cols}")
        return []

    out = []
    for _, row in df.iterrows():
        record = {c: _cell(row.get(c)) for c in cols}  # 键名保持中文原样
        out.append(record)

    out.sort(key=lambda r: _to_str(r.get(c_date)), reverse=True)
    if limit and limit > 0:
        out = out[:limit]

    if not out:
        logger.warning(f"高管持股变动：symbol={code} 无有效记录")

    _cache.set(CAT_MGMT, cache_key, out)
    return out


# ═══════════════════════════════════════════════════════
#  4) 上下文注入文本块
# ═══════════════════════════════════════════════════════

def _render_block(
    code: str,
    name: str,
    reports: list,
    notices: list,
    mgmt: list,
    n_reports: int,
    n_notices: int,
    n_mgmt: int,
) -> str:
    """按给定条数渲染注入文本块（用于超长时逐步收缩）。"""
    lines = [
        "## 公司公告与定期报告（自动获取）",
        f"*标的：{name}（{code}）*",
        "",
    ]

    if reports and n_reports > 0:
        lines.append("### 一、定期报告")
        for r in reports[:n_reports]:
            kind = r.get("kind") or "定期报告"
            date = r.get("date") or "日期未知"
            title = r.get("title") or ""
            url = r.get("url") or ""
            line = f"- [{date}] {kind}：{title}"
            if url:
                line += f"（公告链接：{url}）"
            lines.append(line)
        lines.append("")

    if notices and n_notices > 0:
        lines.append("### 二、最新公告")
        for n in notices[:n_notices]:
            date = n.get("date") or "日期未知"
            title = n.get("title") or ""
            lines.append(f"- [{date}] {title}")
        lines.append("")

    if mgmt and n_mgmt > 0:
        lines.append("### 三、高管持股变动")
        for m in mgmt[:n_mgmt]:
            date = _to_str(m.get("变动日期")) or "日期未知"
            who = _to_str(m.get("变动人")) or "—"
            rel = _to_str(m.get("与公司高管关系"))
            qty = _to_str(m.get("变动数量"))
            price = _to_str(m.get("交易均价"))
            left = _to_str(m.get("剩余股数"))
            way = _to_str(m.get("股份变动途径"))
            seg = f"- [{date}] {who}"
            if rel:
                seg += f"（{rel}）"
            if qty:
                seg += f" {qty}"
            if price:
                seg += f" @均价{price}"
            if left:
                seg += f"，剩余{left}"
            if way:
                seg += f"（{way}）"
            lines.append(seg)
        lines.append("")

    lines.append("**数据源**：定期报告 — 巨潮资讯（cninfo.com.cn）；公司公告 — 东方财富；"
                 "高管持股变动 — 同花顺。以上为公开披露信息，引用时请注明来源与日期。")
    return "\n".join(lines)


def format_announcement_context(symbol: str, company_name: str = "") -> str:
    """生成给大模型注入的「公司公告与定期报告」文本块。

    结构：首行固定标题，分三小节（定期报告 / 最新公告 / 高管持股变动），
    每条带日期，定期报告带公告链接，文末注明数据源。
    三块均无数据 → 返回 ``""``；非 A 股 → 返回 ``""``（不编造港股/美股公告）。
    总长控制在 ``_MAX_CHARS``（4000）字符以内，超长按重要性截断
    （优先级：定期报告 > 最新公告 > 高管持股变动）。
    """
    code = normalize_symbol(symbol)
    if not code:
        logger.info(f"公告上下文：非 A 股或不识别代码 {symbol!r}，返回空串")
        return ""

    try:
        reports = get_disclosure_reports(code, limit=6)
    except Exception as e:
        logger.warning(f"公告上下文：定期报告获取异常 {e}")
        reports = []
    try:
        notices = get_company_notices(code, limit=8)
    except Exception as e:
        logger.warning(f"公告上下文：公司公告获取异常 {e}")
        notices = []
    try:
        mgmt = get_management_changes(code, limit=8)
    except Exception as e:
        logger.warning(f"公告上下文：高管持股变动获取异常 {e}")
        mgmt = []

    if not reports and not notices and not mgmt:
        logger.info(f"公告上下文：symbol={code} 三块数据均为空，返回空串")
        return ""

    name = (company_name or "").strip() or code

    n_r, n_n, n_m = len(reports), len(notices), len(mgmt)
    text = _render_block(code, name, reports, notices, mgmt, n_r, n_n, n_m)

    # 超长则按重要性从低到高收缩：高管变动 → 公告 → 定期报告
    while len(text) > _MAX_CHARS and (n_m > 0 or n_n > 0):
        if n_m > 0:
            n_m -= 1
        else:
            n_n -= 1
        text = _render_block(code, name, reports, notices, mgmt, n_r, n_n, n_m)

    # 仍超长则收缩定期报告（保留至少 1 条）
    while len(text) > _MAX_CHARS and n_r > 1:
        n_r -= 1
        text = _render_block(code, name, reports, notices, mgmt, n_r, n_n, n_m)

    # 兜底硬截断，确保严格不超上限
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]

    return text
