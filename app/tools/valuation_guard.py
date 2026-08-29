"""估值一致性校验器 — 研报出口的硬防线。

背景: 8001 的 financial-analyst 工具链已有 three_scenario / verify_valuation /
calc 等计算工具, 但都是"软约束"——AI 是否调用、正文数字是否带回, 无人校验。
本模块在报告生成出口对含估值内容的文本做程序化一致性检查, 拦截口算错误
(例: "9-10倍PE × EPS 8.2元 = 60-68元" 实际应为 74-82元)。

规则一览(severity: hard / warn):
  R1   单点目标价 ≈ PE倍数 × EPS        (窗口≤300字符, 容差5%)
  R2   价格区间 ⊆ 倍数区间×EPS×[0.9,1.1] (窗口内EPS容差10%; 无窗口EPS用全文EPS容差15%标记loose)
  R3   单点目标价 ≈ P/EV倍数 × 每股EV    (窗口≤300字符, 容差5%)
  R3b  括号/等式注释 价格≈倍数×EV        (全文EV, 容差3%, 例 "66元(约0.65倍PEV)")
  R4   年化EPS = 中期净利×2/总股本       (需全文出现总股本, 容差5%; 缺股本跳过)
  R5   机构目标价引用无"来源:"标注       (软警告)
  R6   PE路径价与PEV路径价分歧>20%且无解释 → 软警告

纯函数、无IO, 可单测。重写/标注由调用方(runner)负责。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ── 触发条件: 含估值关键词且存在价格数字 ─────────────────────────
_TRIGGER_RE = re.compile(
    r"目标价|合理价值|估值区间|倍\s*PE|倍市盈率|P\s*/\s*EV|PEV|每股\s*EV"
)

# ── token 模式(按出现顺序扫描, 记录位置) ────────────────────────
_RE_EPS = re.compile(r"EPS\s*[约为]?\s*(\d+(?:\.\d+)?)\s*元")
_RE_EPS_CN = re.compile(r"每股收益[约为]?\s*(\d+(?:\.\d+)?)\s*元")

# PE 区间先扫, 单点后扫(位置去重)
_RE_PE_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~～至到]\s*(\d+(?:\.\d+)?)\s*倍\s*(?:PE|市盈率)")
_RE_PE_SINGLE = re.compile(r"(?:^|[^\d.])(\d+(?:\.\d+)?)\s*倍\s*(?:PE|市盈率)")
_RE_PE_SINGLE_CN = re.compile(r"(?:PE|市盈率)\s*[约为]?\s*(\d+(?:\.\d+)?)\s*倍")

_RE_PEV_BEFORE = re.compile(r"(?:P\s*/\s*EV|PEV)\s*[约为]?\s*(\d+(?:\.\d+)?)\s*倍")
_RE_PEV_AFTER = re.compile(r"(\d+(?:\.\d+)?)\s*倍\s*(?:P\s*/\s*EV|PEV)")

_RE_EV = re.compile(r"(?:每股\s*EV|EV)\s*[约为]?\s*(\d+(?:\.\d+)?)\s*元")
_RE_EV_CN = re.compile(r"每股\s*内含价值[约为]?\s*(\d+(?:\.\d+)?)\s*元")

# 价格触发词: 目标价/合理价值/对应/修复至/回归到/回到/看至 等
_RE_PRICE_SINGLE = re.compile(
    r"(?:目标价|合理价值|对应|修复至|回归到|回到|看至|看高至|看涨至)[^\d]{0,12}[约]?\s*(\d+(?:\.\d+)?)\s*元"
)
_RE_PRICE_RANGE = re.compile(
    r"(?:目标价|合理价值|估值区间)[^\d]{0,12}(\d+(?:\.\d+)?)\s*[-~～至到]\s*(\d+(?:\.\d+)?)\s*元"
)
_RE_CUR_PRICE = re.compile(
    r"(?:现价|当前股价|最新股价|收盘价|股价)[^\d]{0,8}[约]?\s*(\d+(?:\.\d+)?)\s*元"
)

# R3b: 括号/等式注释 "66元(约0.65倍PEV)"
_RE_PEV_EQUATION = re.compile(
    r"(\d+(?:\.\d+)?)\s*元\s*[（(]\s*约?\s*(\d+(?:\.\d+)?)\s*倍\s*(?:P\s*/\s*EV|PEV)"
)
_RE_PEV_EQUATION_REV = re.compile(
    r"(?:约)?(\d+(?:\.\d+)?)\s*倍\s*(?:P\s*/\s*EV|PEV)[^\d]{0,8}[约为]?\s*(\d+(?:\.\d+)?)\s*元"
)

# R4 中报/半年报净利(两种语序)与股本
_RE_NI_A = re.compile(r"(?:中报|半年报|上半年)\s*净利[^\d]{0,6}[约]?\s*(\d+(?:\.\d+)?)\s*亿")
_RE_NI_B = re.compile(r"(?:中报|半年报|上半年)[^\d]{0,4}(\d+(?:\.\d+)?)\s*亿[^\d]{0,6}净利")
_RE_SHARES = re.compile(r"(?:总股本|股本)[^\d]{0,6}[约]?\s*(\d+(?:\.\d+)?)\s*亿股")
_RE_ANNUALIZED = re.compile(r"年化")

# R5 机构特征词(目标价前30字符内出现才要求标注来源)
_RE_ORG = re.compile(
    r"证券|国际|研报|机构|评级|招银|中金|高盛|摩根|瑞银|花旗|大和|野村|汇丰|巴克莱|美银|瑞信"
)
_RE_SOURCE = re.compile(r"来源[:：]")

# R6 解释词(出现任一视为"报告已说明分歧原因")
_RE_EXPLAIN = re.compile(r"分歧|乐观|悲观|取决于|前提|情景|风险|不确定性|更审慎|保守|基准")

# 窗口与容差
_WINDOW = 300          # 因子与价格的配对距离上限(字符)
_TOL_SINGLE = 0.05     # R1/R3 单点容差
_TOL_RANGE_STRICT = 0.10  # R2 窗口内EPS容差
_TOL_RANGE_LOOSE = 0.15   # R2 全文EPS容差
_TOL_EQUATION = 0.03   # R3b 等式容差
_TOL_NI = 0.05         # R4 年化容差
_R6_DIVERGE = 0.20     # R6 分歧阈值


@dataclass
class Token:
    kind: str            # eps / pe / pe_range / pev / ev / price / price_range / cur_price / ni / shares
    value: float
    value2: Optional[float]
    start: int
    end: int
    raw: str


def _scan(text: str) -> Tuple[List[Token], Optional[float], Optional[float]]:
    """扫描全文, 返回按位置排序的 token 列表、全文EPS(最后一个)、全文每股EV(最后一个)。"""
    tokens: List[Token] = []

    def add(kind, start, end, v1, v2=None, raw=""):
        tokens.append(Token(kind, v1, v2, start, end, raw))

    # 区间优先(PE区间 / 价格区间), 单点匹配落在区间内则跳过
    ranges = []
    for m in _RE_PE_RANGE.finditer(text):
        ranges.append((m.start(), m.end()))
        add("pe_range", m.start(), m.end(), float(m.group(1)), float(m.group(2)), m.group(0))
    for m in _RE_PRICE_RANGE.finditer(text):
        ranges.append((m.start(), m.end()))
        add("price_range", m.start(), m.end(), float(m.group(1)), float(m.group(2)), m.group(0))

    def covered(pos: int) -> bool:
        return any(rs <= pos < re_ for rs, re_ in ranges)

    for m in _RE_EPS.finditer(text):
        add("eps", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_EPS_CN.finditer(text):
        add("eps", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_PE_SINGLE.finditer(text):
        if not covered(m.start()):
            add("pe", m.start() + (1 if m.group(0)[0].isdigit() else 1), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_PE_SINGLE_CN.finditer(text):
        if not covered(m.start()):
            add("pe", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_PEV_BEFORE.finditer(text):
        add("pev", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_PEV_AFTER.finditer(text):
        add("pev", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_EV.finditer(text):
        add("ev", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_EV_CN.finditer(text):
        add("ev", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_PRICE_SINGLE.finditer(text):
        if not covered(m.start()):
            add("price", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_CUR_PRICE.finditer(text):
        add("cur_price", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_NI_A.finditer(text):
        add("ni", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_NI_B.finditer(text):
        add("ni", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_SHARES.finditer(text):
        add("shares", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    # R3b 等式注释的价格也作为 price_eq token, 保证触发与校验
    for m in _RE_PEV_EQUATION.finditer(text):
        add("price_eq", m.start(), m.end(), float(m.group(1)), raw=m.group(0))
    for m in _RE_PEV_EQUATION_REV.finditer(text):
        add("price_eq", m.start(), m.end(), float(m.group(2)), raw=m.group(0))

    tokens.sort(key=lambda t: t.start)

    last_eps = None
    last_ev = None
    for t in tokens:
        if t.kind == "eps":
            last_eps = t.value
        elif t.kind == "ev":
            last_ev = t.value
    return tokens, last_eps, last_ev


def _pct_diff(actual: float, expect: float) -> float:
    if expect == 0:
        return 0.0
    return abs(actual - expect) / expect


def check_report(text: str) -> dict:
    """对报告文本执行估值一致性校验。

    返回:
      triggered  — 是否含估值内容(否则调用方应原样放行)
      hard_fail  — 是否存在硬失败(规则1-4)
      checks     — 逐条检查结果 [{rule, severity, detail, evidence}]
      warnings   — 软警告列表
      skipped    — 因缺数据跳过的检查
    """
    result = {"triggered": False, "hard_fail": False, "checks": [], "warnings": [], "skipped": []}

    if not text:
        return result
    # 快速路径: 估值关键词; R4年化场景单独放行(EPS+股本+年化, 无目标价触发词)
    r4_path = (
        _RE_EPS.search(text) is not None
        and _RE_SHARES.search(text) is not None
        and _RE_ANNUALIZED.search(text) is not None
    )
    if not _TRIGGER_RE.search(text) and not r4_path:
        return result

    tokens, full_eps, full_ev = _scan(text)
    prices = [t for t in tokens if t.kind in ("price", "price_range", "price_eq")]
    factors = [t for t in tokens if t.kind in ("eps", "pe", "pe_range", "pev", "ev")]
    # 触发条件: 有估值因子, 且 存在价格 / R3b等式材料 / R4年化材料
    has_r4_material = (
        any(t.kind == "ni" for t in tokens)
        and any(t.kind == "shares" for t in tokens)
        and _RE_ANNUALIZED.search(text) is not None
    )
    if not factors or not (prices or has_r4_material):
        return result
    result["triggered"] = True

    # 最近因子状态(单遍扫描配对)
    last_eps: Optional[Tuple[float, int]] = None
    last_pe: Optional[Tuple[float, int]] = None
    last_pe_range: Optional[Tuple[float, float, int]] = None
    last_pev: Optional[Tuple[float, int]] = None
    last_ev: Optional[Tuple[float, int]] = None

    pe_path_prices: List[float] = []
    pev_path_prices: List[float] = []

    def hard(rule, detail, evidence=""):
        result["hard_fail"] = True
        result["checks"].append({"rule": rule, "severity": "hard", "detail": detail, "evidence": evidence})

    def warn(rule, detail, evidence=""):
        result["warnings"].append({"rule": rule, "severity": "warn", "detail": detail, "evidence": evidence})

    for t in tokens:
        if t.kind == "eps":
            last_eps = (t.value, t.start)
        elif t.kind == "pe":
            last_pe = (t.value, t.start)
        elif t.kind == "pe_range":
            last_pe_range = (t.value, t.value2 if t.value2 is not None else t.value, t.start)
        elif t.kind == "pev":
            last_pev = (t.value, t.start)
        elif t.kind == "ev":
            last_ev = (t.value, t.start)
        elif t.kind == "price":
            # R1: PE 路径
            if last_pe and last_eps and (t.start - last_eps[1]) <= _WINDOW:
                expect = last_pe[0] * last_eps[0]
                if _pct_diff(t.value, expect) > _TOL_SINGLE:
                    hard("R1",
                         f"目标价 {t.value:.2f}元 与 PE {last_pe[0]:g}倍 × EPS {last_eps[0]:g}元 = {expect:.2f}元 不符"
                         f"(偏差 {_pct_diff(t.value, expect)*100:.1f}%)",
                         t.raw)
                else:
                    pe_path_prices.append(t.value)
            # R3: PEV 路径
            if last_pev and last_ev and (t.start - last_ev[1]) <= _WINDOW:
                expect = last_pev[0] * last_ev[0]
                if _pct_diff(t.value, expect) > _TOL_SINGLE:
                    hard("R3",
                         f"目标价 {t.value:.2f}元 与 P/EV {last_pev[0]:g}倍 × 每股EV {last_ev[0]:g}元 = {expect:.2f}元 不符"
                         f"(偏差 {_pct_diff(t.value, expect)*100:.1f}%)",
                         t.raw)
                else:
                    pev_path_prices.append(t.value)
        elif t.kind == "price_range":
            lo, hi = t.value, t.value2
            # R2: PE 区间映射
            if last_pe_range and (t.start - last_pe_range[2]) <= _WINDOW:
                eps = None
                mode = "strict"
                if last_eps and (t.start - last_eps[1]) <= _WINDOW:
                    eps = last_eps[0]
                elif full_eps is not None:
                    eps = full_eps
                    mode = "loose"
                if eps is not None:
                    tol = _TOL_RANGE_STRICT if mode == "strict" else _TOL_RANGE_LOOSE
                    pe_lo, pe_hi = last_pe_range[0], last_pe_range[1]
                    exp_lo = pe_lo * eps * (1 - tol)
                    exp_hi = pe_hi * eps * (1 + tol)
                    if lo < exp_lo or hi > exp_hi:
                        hard("R2",
                             f"价格区间 {lo:.2f}-{hi:.2f}元 超出 {pe_lo:g}-{pe_hi:g}倍PE × EPS {eps:g}元 的应有区间"
                             f"[{pe_lo*eps:.2f}, {pe_hi*eps:.2f}]元(容差{tol*100:.0f}%)",
                             t.raw)
                else:
                    result["skipped"].append("R2: 缺EPS, 无法校验价格区间映射")
        elif t.kind == "ni":
            # R4: 年化推导(需全文有股本)
            if full_eps is None:
                result["skipped"].append("R4: 缺EPS, 无法校验年化推导")
                continue
            annualized_near = _RE_ANNUALIZED.search(text, t.end, t.end + 120)
            if annualized_near is None and t.start - 120 >= 0:
                annualized_near = _RE_ANNUALIZED.search(text, max(0, t.start - 120), t.start)
            if annualized_near is None:
                continue  # 净利未与"年化"同现, 不触发
            shares = None
            for st in tokens:
                if st.kind == "shares":
                    shares = st.value
                    break
            if shares is None:
                result["skipped"].append("R4: 缺总股本, 无法校验年化EPS推导")
                continue
            expect = t.value * 2 / shares
            if _pct_diff(full_eps, expect) > _TOL_NI:
                hard("R4",
                     f"年化EPS {full_eps:g}元 与 净利 {t.value:g}亿×2/{shares:g}亿股 = {expect:.2f}元 不符"
                     f"(偏差 {_pct_diff(full_eps, expect)*100:.1f}%)",
                     t.raw)
        elif t.kind == "shares":
            pass

    # R3b: 括号/等式注释(用全文每股EV)
    if full_ev is not None:
        for m in _RE_PEV_EQUATION.finditer(text):
            price, pev = float(m.group(1)), float(m.group(2))
            expect = pev * full_ev
            if _pct_diff(price, expect) > _TOL_EQUATION:
                hard("R3b",
                     f"注释 '{m.group(0)}' 中 {price:g}元 与 {pev:g}倍P/EV × 每股EV {full_ev:g}元 = {expect:.2f}元 不符"
                     f"(偏差 {_pct_diff(price, expect)*100:.1f}%)",
                     m.group(0))
        for m in _RE_PEV_EQUATION_REV.finditer(text):
            pev, price = float(m.group(1)), float(m.group(2))
            expect = pev * full_ev
            if _pct_diff(price, expect) > _TOL_EQUATION:
                hard("R3b",
                     f"注释 '{m.group(0)}' 中 {price:g}元 与 {pev:g}倍P/EV × 每股EV {full_ev:g}元 = {expect:.2f}元 不符"
                     f"(偏差 {_pct_diff(price, expect)*100:.1f}%)",
                     m.group(0))

    # R5: 机构目标价引用无来源(目标价前30字符内有机构特征词, 且全文无"来源:")
    if not _RE_SOURCE.search(text):
        for m in _RE_PRICE_SINGLE.finditer(text):
            ctx_start = max(0, m.start() - 30)
            if _RE_ORG.search(text[ctx_start:m.start()]) or _RE_ORG.search(text[m.end():m.end() + 10]):
                warn("R5", f"目标价 '{m.group(0)}' 未标注来源(机构/日期)", m.group(0))
                break

    # R6: PE路径与PEV路径分歧
    if pe_path_prices and pev_path_prices:
        pe_last = pe_path_prices[-1]
        pev_last = pev_path_prices[-1]
        if pe_last > 0 and pev_last > 0:
            div = abs(pe_last - pev_last) / min(pe_last, pev_last)
            if div > _R6_DIVERGE and not _RE_EXPLAIN.search(text):
                warn("R6", f"PE路径目标价 {pe_last:g}元 与 PEV路径目标价 {pev_last:g}元 分歧 {div*100:.0f}%, 报告未说明原因",
                     f"PE:{pe_last:g} vs PEV:{pev_last:g}")

    return result


def annotate_report(text: str, result: dict) -> str:
    """在报告顶部插入校验提示条(硬失败醒目, 软警告轻量)。"""
    if not result.get("triggered"):
        return text
    lines = []
    if result.get("hard_fail"):
        lines.append("⚠️ 估值数据一致性校验未通过, 以下数据请以复核为准:")
        for c in result["checks"]:
            if c["severity"] == "hard":
                lines.append(f"- {c['detail']}")
    if result.get("warnings"):
        lines.append("ℹ️ 提示:")
        for w in result["warnings"]:
            lines.append(f"- {w['detail']}")
    if not lines:
        return text
    block = "\n".join(lines)
    return f"{block}\n\n{text}"
