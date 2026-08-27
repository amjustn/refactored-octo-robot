"""valuation_guard 单元测试 — 估值一致性校验器。

覆盖: 触发判断 / R1单点PE / R2区间映射(strict+loose) / R3单点PEV /
R3b等式注释 / R4年化推导(含缺股本跳过) / R5来源标注 / R6路径分歧 /
annotate 标注。平安案例(用户实测暴露的数字矛盾)作为回归用例。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.tools.valuation_guard import annotate_report, check_report

# ── 回归用例: 平安案例(用户实测, "9-10倍PE×EPS 8.2元=60-68元" 必须被拦) ──
PINGAN_BAD = """平安当前股价56.13元，对应PB 0.99倍、P/EV 0.58倍，处于过去十年5%分位以下。

报告中引用的招银国际目标价75元，隐含+34%空间。但这个目标价值得拆解。

PE锚定法：报告引用的机构一致预期为2027年EPS 9.25元。若给予约8倍PE（平安历史中枢的偏低水平），则对应75元附近。

PEV锚定法：平安当前P/EV约0.58倍，每股EV约97元。若市场从极度悲观修复至0.75倍P/EV，则对应约73元。

更审慎的估值区间：若2026全年EPS约8.2元（中报925亿净利年化），给予9-10倍PE，合理价值约60-68元。

结论：66元（约0.65倍PEV）作为长期合理估值中枢的观察线，75元以上才考虑分批兑现。
"""

PINGAN_GOOD = PINGAN_BAD.replace("合理价值约60-68元", "合理价值约74-82元").replace(
    "66元（约0.65倍PEV）", "63元（约0.65倍PEV）"
)


def _rule_names(res: dict) -> set:
    return {c["rule"] for c in res["checks"]}


# ── 触发判断 ─────────────────────────────────────────────

def test_no_trigger_briefing():
    """日常简报类文本(无估值关键词)原样放行。"""
    text = "今日市场概览：沪指上涨0.8%，两市成交额1.2万亿，北向资金净流入35亿。"
    res = check_report(text)
    assert res["triggered"] is False
    assert res["hard_fail"] is False
    assert annotate_report(text, res) == text


def test_trigger_only_with_factors():
    """有价格数字但无估值因子(PE/EPS等)不触发。"""
    text = "公司今年营收约120亿元，同比+15%。"
    res = check_report(text)
    assert res["triggered"] is False


# ── R1 单点: 目标价 ≈ PE倍数 × EPS ──────────────────────

def test_r1_pass():
    text = "机构一致预期2027年EPS 9.25元，若给予8倍PE，则对应74元。"
    res = check_report(text)
    assert res["triggered"] is True
    assert res["hard_fail"] is False


def test_r1_fail():
    text = "机构一致预期2027年EPS 9.25元，若给予8倍PE，则对应95元。"
    res = check_report(text)
    assert res["hard_fail"] is True
    assert "R1" in _rule_names(res)


# ── R2 区间映射(strict: 窗口内EPS, 容差10%) ──────────────

def test_r2_pass():
    text = "若2026全年EPS约8.2元，给予9-10倍PE，合理价值约74-82元。"
    res = check_report(text)
    assert res["hard_fail"] is False


def test_r2_fail_pingan_case():
    """回归: 平安案例 9-10倍PE×8.2元 却写 60-68元 → 必须拦。"""
    res = check_report(PINGAN_BAD)
    assert res["triggered"] is True
    assert res["hard_fail"] is True
    assert "R2" in _rule_names(res)


def test_r2_loose_mode():
    """EPS 距价格区间超过窗口 → 用全文EPS宽松校验(容差15%), 仍拦。"""
    text = "公司EPS约8.2元。" + ("此处为无关内容。" * 60) + "给予9-10倍PE，合理价值约60-68元。"
    res = check_report(text)
    assert res["hard_fail"] is True
    assert "R2" in _rule_names(res)


def test_r2_loose_pass():
    text = "公司EPS约8.2元。" + ("此处为无关内容。" * 60) + "给予9-10倍PE，合理价值约75-80元。"
    res = check_report(text)
    assert res["hard_fail"] is False


# ── R3 单点: 目标价 ≈ P/EV倍数 × 每股EV ──────────────────

def test_r3_pass():
    text = "平安当前P/EV约0.58倍，每股EV约97元。若修复至0.75倍P/EV，则对应约73元。"
    res = check_report(text)
    assert res["hard_fail"] is False


def test_r3_fail():
    text = "平安当前P/EV约0.58倍，每股EV约97元。若修复至0.75倍P/EV，则对应约90元。"
    res = check_report(text)
    assert res["hard_fail"] is True
    assert "R3" in _rule_names(res)


# ── R3b 等式注释: "66元(约0.65倍PEV)" 容差3% ─────────────

def test_r3b_fail():
    text = "每股EV约97元。结论：66元（约0.65倍PEV）作为观察线。"
    res = check_report(text)
    assert res["hard_fail"] is True
    assert "R3b" in _rule_names(res)


def test_r3b_pass():
    text = "每股EV约97元。结论：63元（约0.65倍PEV）作为观察线。"
    res = check_report(text)
    assert res["hard_fail"] is False


def test_r3b_rev_pass():
    text = "每股EV约97元。结论：约0.68倍PEV 对应66元。"
    res = check_report(text)
    assert res["hard_fail"] is False


# ── R4 年化推导: 净利×2/股本 ─────────────────────────────

def test_r4_pass():
    text = "中报净利925亿元，年化EPS约10.1元，总股本183亿股。"
    res = check_report(text)
    assert res["hard_fail"] is False


def test_r4_fail():
    text = "中报925亿净利年化，年化EPS约8.2元，总股本183亿股。"
    res = check_report(text)
    assert res["hard_fail"] is True
    assert "R4" in _rule_names(res)


def test_r4_skip_without_shares():
    """缺总股本 → R4 跳过(不硬拦), 但记录 skipped。"""
    text = "中报925亿净利年化，年化EPS约8.2元。给予9-10倍PE，合理价值约74-82元。"
    res = check_report(text)
    assert res["hard_fail"] is False
    assert any("R4" in s for s in res["skipped"])


# ── R5 机构目标价引用无来源(软警告) ──────────────────────

def test_r5_warn():
    text = "报告中引用的招银国际目标价75元。EPS 9.25元，8倍PE，对应74元。"
    res = check_report(text)
    assert res["hard_fail"] is False
    assert any(w["rule"] == "R5" for w in res["warnings"])


def test_r5_ok_with_source():
    text = "EPS 9.25元，8倍PE，对应74元。（来源：东方财富研报中心）"
    res = check_report(text)
    assert not any(w["rule"] == "R5" for w in res["warnings"])


def test_r5_no_false_positive():
    """普通目标价(前无机构特征词)不触发 R5。"""
    text = "EPS 9.25元，8倍PE，对应74元，目标价有一定上行空间。"
    res = check_report(text)
    assert not any(w["rule"] == "R5" for w in res["warnings"])


# ── R6 双路径分歧(软警告) ────────────────────────────────

def test_r6_warn():
    text = "PE路径对应75元，PEV路径对应110元。EPS 9.25元，8倍PE，对应75元；每股EV约97元，修复至1.13倍P/EV，对应110元。"
    res = check_report(text)
    assert any(w["rule"] == "R6" for w in res["warnings"])


def test_r6_ok_with_explanation():
    text = "PE路径对应75元，PEV路径对应110元，分歧源于乐观情景假设。EPS 9.25元，8倍PE，对应75元；每股EV约97元，修复至1.13倍P/EV，对应110元。"
    res = check_report(text)
    assert not any(w["rule"] == "R6" for w in res["warnings"])


def test_r6_no_diverge():
    """两路径价格接近(分歧≤20%) → 不警告。"""
    text = "EPS 9.25元，8倍PE，对应74元；每股EV约97元，修复至0.75倍P/EV，对应73元。"
    res = check_report(text)
    assert not any(w["rule"] == "R6" for w in res["warnings"])


# ── 完整回归 + 标注 ──────────────────────────────────────

def test_pingan_good_case():
    """修正版平安报告(74-82元、63元)应全部通过。"""
    res = check_report(PINGAN_GOOD)
    assert res["hard_fail"] is False


def test_annotate_hard():
    res = check_report(PINGAN_BAD)
    out = annotate_report(PINGAN_BAD, res)
    assert "⚠️" in out
    assert "R2" in out or "9-10倍PE" in out
    assert "ℹ️" in out  # R5 软警告也在


def test_annotate_untouched_when_not_triggered():
    text = "普通日报，无估值内容。"
    res = check_report(text)
    assert annotate_report(text, res) == text


def test_pingan_bad_contains_r3b():
    """平安案例的 66元(约0.65倍PEV) 必须被 R3b 拦下。"""
    res = check_report(PINGAN_BAD)
    assert "R3b" in _rule_names(res)
