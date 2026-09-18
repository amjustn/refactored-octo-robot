#!/usr/bin/env python3
"""Citation/structure gate calibration (plan A).

The old citation gate judged an 80-char window around each number, so a
source at a table tail (or a table header source) never covered the rows,
and report-computed numbers were counted as unsourced. The old structure
gate inferred required sections from every ``##`` heading in the skill
prompt, turning execution steps ("第一步：…") into mandatory report
headings.

These tests pin the new behaviour and — critically — prove the gate still
catches a genuinely unsourced report (calibration, not silencing).

Run: cd <repo-root> && venv/bin/python -m pytest tests/test_guard_calibration.py -v
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harness import guards


def _citation_warnings(report: str) -> list:
    _, warnings = guards.OutputGuard().check(report)
    return [w for w in warnings if w["gate"] == "citation"]


# ============================================================
# Citation: block-level source coverage
# ============================================================

def test_table_header_source_covers_all_rows():
    report = (
        "| 项目 | 2025E | 来源 |\n"
        "|------|-------|------|\n"
        "| 营业收入 | 1450亿元 | 交银国际证券 |\n"
        "| 净利润 | 320亿元 | 交银国际证券 |\n"
    )
    assert _citation_warnings(report) == []


def test_table_tail_caption_covers_whole_table():
    report = (
        "| 项目 | 2025E |\n"
        "|------|-------|\n"
        "| 营业收入 | 1450亿元 |\n"
        "| 净利润 | 320亿元 |\n"
        "\n"
        "来源：交银国际证券\n"
    )
    assert _citation_warnings(report) == []


def test_source_far_in_paragraph_covers_block():
    report = "公司实现营业收入1450亿元，同比增长12%。" + "补充说明。" * 40 + "（来源：公司公告）"
    assert _citation_warnings(report) == []


def test_iter_blocks_merges_caption_and_keeps_tables_whole():
    blocks = guards._iter_blocks(
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n来源：x\n\n普通段落。"
    )
    assert len(blocks) == 2
    assert "来源" in blocks[0] and "1 | 2" in blocks[0]
    assert blocks[1] == "普通段落。"


# ============================================================
# Citation: report-derived numbers are exempt
# ============================================================

def test_scenario_table_without_source_is_exempt():
    report = (
        "| 情景 | 目标价 |\n"
        "|------|--------|\n"
        "| 悲观 | 1056元 |\n"
        "| 中性 | 1450元 |\n"
        "| 乐观 | 1720元 |\n"
    )
    assert _citation_warnings(report) == []


def test_formula_and_implied_numbers_are_exempt():
    report = "合理价 = 920.76元 × 15 ≈ 13811元；较现价隐含上行空间34%。"
    assert _citation_warnings(report) == []


# ============================================================
# Citation: negative validation — real misses must still fire
# ============================================================

def test_unsourced_table_still_warns():
    report = (
        "| 项目 | 数值 |\n"
        "|------|------|\n"
        "| 营业收入 | 250亿元 |\n"
        "| 净利润 | 45亿元 |\n"
    )
    warnings = _citation_warnings(report)
    assert len(warnings) == 1
    assert "2 处" in warnings[0]["detail"]


def test_only_unsourced_block_is_counted():
    report = (
        "| 项目 | 数值 | 来源 |\n"
        "|------|------|------|\n"
        "| 营业收入 | 1450亿元 | 公司公告 |\n"
        "\n"
        "公司净利润45亿元。\n"
    )
    warnings = _citation_warnings(report)
    assert len(warnings) == 1
    assert "1 处" in warnings[0]["detail"]
    assert "45亿" in warnings[0]["detail"]


def test_plain_unsourced_numbers_still_warn():
    assert _citation_warnings("营收250亿元。净利润45亿元。增长率20%。")


# ============================================================
# Structure: registry-declared sections only
# ============================================================

def test_required_sections_come_from_registry():
    assert "独立判断" in guards._required_sections("broker-reports")
    # skills without an explicit declaration are not structure-checked
    assert guards._required_sections("investment-research") == []


def test_undeclared_skill_has_no_structure_warning():
    _, warnings = guards.OutputGuard().check(
        "# 任意标题\n\n正文。", skill_name="investment-research", strategy="single"
    )
    assert not [w for w in warnings if w["gate"] == "structure"]


def test_declared_sections_warn_only_when_missing():
    _, warnings = guards.OutputGuard().check(
        "# 报告\n\n## 独立判断\n\n结论。", skill_name="broker-reports", strategy="single"
    )
    details = [w["detail"] for w in warnings if w["gate"] == "structure"]
    assert len(details) == 1
    # process-step headings are never demanded
    assert "第一步" not in details[0] and "第二步" not in details[0]


def test_declared_sections_present_no_warning():
    report = "## 机构观点总览\n\n## 主流机构观点归纳\n\n## 独立判断\n"
    _, warnings = guards.OutputGuard().check(
        report, skill_name="broker-reports", strategy="single"
    )
    assert not [w for w in warnings if w["gate"] == "structure"]
