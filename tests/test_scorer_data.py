# -*- coding: utf-8 -*-
"""scorer 数据维度：标签多处出现时的任一命中规则"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))

from scorer import score_data  # noqa: E402


def test_header_pollution_still_scores():
    """表头"2025营收增速"不抢命中：后文"营收 1,239.12亿元"应得分"""
    report = (
        "| 公司 | 2025营收增速 | 2025净利变化 |\n"
        "营收增速从2023年+89.7%回落。\n"
        "| 营收 | 1,239.12亿元 | +12.0% |\n"
    )
    case = {"expect": {"data_points": {"营收": 1240}}}
    r = score_data(report, case)
    assert r["points"]["营收"]["ok"] is True
    assert r["points"]["营收"]["found"] == 1239.12
    assert r["score"] == 30


def test_no_match_anywhere_still_zero():
    """全文无接近值仍为 0，并报告首个候选便于对账"""
    report = "营收增速从2023年回落，详见后文。营收情况良好。"
    case = {"expect": {"data_points": {"营收": 1240}}}
    r = score_data(report, case)
    assert r["score"] == 0
    assert r["points"]["营收"]["found"] == 2023.0


def test_tolerance_boundary():
    """2% 容差边界"""
    report = "营收 1000亿元"
    assert score_data(report, {"expect": {"data_points": {"营收": 1020}}})["score"] == 30
    assert score_data(report, {"expect": {"data_points": {"营收": 1021}}})["score"] == 0


def test_no_data_points_full_marks():
    assert score_data("任意", {"expect": {}})["score"] == 30


def test_multi_column_year_table():
    """多年对比表：标签行首列是旧年份，当期值在同窗口后续列"""
    label = "营收"
    report = f"| {label} | 6,090 | 6,603 | 7,518 | 4,012 |"
    r = score_data(report, {"expect": {"data_points": {label: 7518}}})
    assert r["points"][label]["ok"] is True and r["score"] == 30
