"""新鲜度门闸（P2，2026-09-13）——报告说"需要核实"，而数据其实已经注入。

问题现场（实测）：
- 144 份报告里 68 份（47%）含"需要核实/待核实"措辞。
- 同一时刻（2026-09-13 12:46 / 12:48）同一标的（澜起科技）：
    多Agent链路（带工具，prompt 557k tokens）→ 给出 2026H1 营收 33.36 亿、
    同比 +26.66%、归母 +72.33%，并标注公告日 2026-08-29；
    单Agent链路（无工具，prompt 3.8k tokens）→ 开篇写"2025年年报、2026年
    半年报应已披露，但具体数字与最新高管名单、持股变动需要核实"。
- 也就是说：模型把"我没看到数据"写成了"数据不掌握"，而数据本可以在上下文里。

判据（刻意做窄，避免对高质量报告滥告警）：
1. 报告侧必须出现【整类数据】的缺口声明，而非子科目提示。例如
   "具体数字…需要核实" / "高管名单…待核实" 算；而
   "需要核实（证据链缺失）：分部季度毛利率、应收账款账龄" 这类**子科目**清单不算
   —— 实测后者来自一份数据充分的 earnings-team 报告（40 处"需要核实"），
   若按词频告警会天天误报。
   实现：缺口措辞与"数据类别名词"必须在 ±20 字窗口内同现。
2. 上下文侧必须真的有对应数据块（公告/定期报告、高管持股变动、机构研报、实时行情）。
3. 无上下文留痕时不告警（不凭空指控）。

本门闸不改报告正文（只告警 + 落 guard_warnings / 事件）。

用法：
    from .freshness_gate import check_freshness
    warnings = check_freshness(report_text, context_text)
"""
from __future__ import annotations

import re

GATE_NAME = "freshness"

# ── 缺口措辞（"我不掌握"类免责）──────────────────────────────
_GAP_PHRASES = (
    r"需要核实", r"待核实", r"需核实", r"有待核实", r"无法核实", r"未能核实", r"未核实",
    r"尚未披露", r"未披露", r"未获取到", r"未能获取", r"无法获取", r"无法获得",
    r"未获得", r"暂无数据", r"数据缺失", r"无从获取", r"未能查到", r"未查到",
)
_GAP_RE = re.compile("|".join(_GAP_PHRASES))

# 锚点式强信号：即便数据类别名词不在窗口内，这类"应已披露却没给数字"的表述也算
_ANCHOR_RE = re.compile(
    r"(应已披露|本应披露|已是公开信息|已经披露)[^。；\n]{0,40}?(需要核实|待核实|未获取到|未能获取|未获得|无法获取)"
)

# ── 每类数据的判定规则 ────────────────────────────────────────
# data_re: 该类数据的"整类名词"（用于与缺口措辞同现判定）
# ctx_markers: 上下文里该类数据块的标记
_RULES = [
    {
        "rule": "financials",
        "data_re": re.compile(r"数字|数据|财报|年报|半年报|中报|季报|财务|营收|营业收入|净利润|归母|现金流|资产负债|业绩"),
        "ctx_markers": ("公司公告与定期报告", "定期报告"),
        "detail": "报告称财报/定期报告的具体数字需要核实，但本次上下文已注入「公司公告与定期报告」"
                  "（巨潮资讯定期报告披露日期与链接 + 东财公告），请直接引用并标注来源（来源：巨潮资讯 <日期>）。",
    },
    {
        "rule": "management",
        "data_re": re.compile(r"高管|管理层变动|持股|减持|增持|股权激励|股份变动|董事|监事|股东名单"),
        "ctx_markers": ("高管持股变动",),
        "detail": "报告称高管名单/持股变动需要核实，但本次上下文已注入「高管持股变动」明细"
                  "（同花顺，含变动人/关系/数量/均价/剩余股数），请直接引用并标注来源。",
    },
    {
        "rule": "research",
        "data_re": re.compile(r"研报|机构观点|机构评级|评级|一致预期|目标价|卖方|盈利预测"),
        "ctx_markers": ("机构研报",),
        "detail": "报告称缺少机构研报/一致预期，但本次上下文已注入「机构研报」（东财研报中心/慧博投研），"
                  "请引用列表中的机构名与日期并标注来源。",
    },
    {
        "rule": "quote",
        "data_re": re.compile(r"股价|行情|现价|收盘价|市值|估值水平"),
        "ctx_markers": ("实时行情",),
        "detail": "报告称未获取到行情/股价数据，但本次上下文已注入「实时行情」块，请直接引用。",
    },
    {
        "rule": "notice_link",
        "data_re": re.compile(r"公告原文|公告链接|披露文件|公告详情"),
        "ctx_markers": ("公告链接", "cninfo.com.cn", "公司公告与定期报告"),
        "detail": "报告称公告原文/链接未获取，但本次上下文已注入巨潮公告链接，请直接引用。",
    },
]

_WINDOW = 20              # 缺口措辞与数据类别名词的最大同现距离（字）
# 整类缺口声明的密度阈值：>=3 处 + 上下文 >=4000 字符才提示（实测高质量
# earnings-team 报告的"需要核实"多为子科目提示，不构成整类缺口声明）
_DENSITY_THRESHOLD = 3
_RICH_CONTEXT_CHARS = 4000

# ── 豁免规则：报告已经引用了本次注入的数据 → 不再告警 ────────────
# 理由（实测）：earnings-team_20260913_124602.md 数据充分（给出 2026H1 营收
# 33.36 亿/+26.66%、公告日 2026-08-29），但仍有 40 处"需要核实"，指的是分部
# 季度毛利率、应收账款账龄等**上下文里本来就没有的明细**——那是诚实的证据链
# 标注，不是"没看到数据"。判据：报告里出现了本次上下文中的高辨识度数据
# （带小数的数字、4 位以上数字、YYYY-MM-DD 日期），命中 >=2 个即认定该报告
# 确实用上了注入数据，豁免告警。
_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
# 只收"高辨识度"的数字：带小数且 >=3 位有效数字（33.36 / 191.96），
# 或千分位分组（1,234）。纯整数（含年份 2026）不收 —— 否则任何报告都能
# 靠"提到 2025/2026 年"蒙到豁免。
_NUM_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d{3,}\b|\b\d{2,}\.\d{1,2}\b")
_EVIDENCE_MIN = 2


def _distinctive_tokens(text: str, limit: int = 400) -> set:
    """高辨识度数据 token：带千分位或带小数的数字 + YYYY-MM-DD 日期。"""
    toks: set = set()
    for m in _NUM_RE.finditer(text or ""):
        toks.add(m.group(0))
        if len(toks) >= limit:
            break
    for m in _DATE_RE.finditer(text or ""):
        toks.add(m.group(0))
        if len(toks) >= limit:
            break
    return toks


def _context_data_used(report: str, context: str) -> int:
    """报告里出现了多少个本次上下文中的高辨识度数据（>=_EVIDENCE_MIN 即豁免）。"""
    hits = 0
    for t in _distinctive_tokens(context):
        if t in report:
            hits += 1
            if hits >= _EVIDENCE_MIN:
                break
    return hits


def _macro_claim_hits(report: str, data_re: re.Pattern) -> int:
    """统计"整类数据缺口"声明次数（缺口措辞与数据类别名词在 ±WINDOW 内同现）。"""
    return len(_macro_gap_positions(report, data_re))


def _macro_gap_positions(report: str, data_re: re.Pattern) -> set:
    positions: set = set()
    for m in _GAP_RE.finditer(report):
        lo = max(0, m.start() - _WINDOW)
        hi = min(len(report), m.end() + _WINDOW)
        if data_re.search(report[lo:hi]):
            positions.add(m.start())
    return positions


def _macro_claims_total(report: str) -> int:
    """整类缺口声明的去重总数（同一处缺口被多类规则同时命中只算 1 次）。"""
    allpos: set = set()
    for spec in _RULES:
        allpos |= _macro_gap_positions(report, spec["data_re"])
    return len(allpos)


def check_freshness(report: str, context: str) -> list:
    """返回告警列表（每项 {'gate','rule','detail'}）；无命中返回 []。"""
    warnings: list = []
    if not report:
        return warnings
    ctx = context or ""
    if not ctx:
        # 没有上下文留痕时无法判定"其实有数据"，不做任何指控（避免误报）
        return warnings

    # 豁免：报告确实引用了本次注入的数据（>=2 个高辨识度数据命中）→ 剩下的
    # "需要核实"是子科目/明细级缺口，不再告警。
    if _context_data_used(report, ctx) >= _EVIDENCE_MIN:
        return warnings

    macro_total = _macro_claims_total(report)
    for spec in _RULES:
        n = _macro_claim_hits(report, spec["data_re"])
        if n <= 0:
            continue
        if any(marker in ctx for marker in spec["ctx_markers"]):
            warnings.append({"gate": GATE_NAME, "rule": spec["rule"], "detail": spec["detail"]})

    # 锚点式：明确写了"应已披露…需要核实"，且上下文确有公告类数据
    if _ANCHOR_RE.search(report) and any(
        marker in ctx for marker in ("公司公告与定期报告", "定期报告", "实时行情")
    ):
        if not any(w["rule"] == "financials" for w in warnings):
            warnings.append({"gate": GATE_NAME, "rule": "financials", "detail": _RULES[0]["detail"]})

    # 整类缺口声明的密度规则（用 macro_total，而不是"需要核实"原始词频）
    if macro_total >= _DENSITY_THRESHOLD and len(ctx) >= _RICH_CONTEXT_CHARS:
        warnings.append({
            "gate": GATE_NAME,
            "rule": "density",
            "detail": (
                f"报告出现 {macro_total} 处整类数据缺口声明，而本次上下文已注入 {len(ctx)} 字符数据；"
                "请逐条核对：上下文中已有的数据必须直接引用，不得写成'需要核实'。"
            ),
        })
    return warnings
