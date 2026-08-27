"""three_scenario 多估值锚测试 — PE/PB/PS 三情景估值。

覆盖: 旧调用兼容(eps+pe_multiples) / 新调用三种 anchor / 手算正确性 /
参数缺失与未知 anchor 的错误处理。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.tools.financial_rigor import three_scenario


# ── 旧调用兼容(eps + pe_multiples 风格) ─────────────────────

def test_legacy_pe_call():
    """旧签名(eps+pe_multiples)必须照常工作, 等价于 PE 锚。"""
    r = three_scenario(price=39.8, eps=5.7, shares_100m=252.2,
                       growth_rates=[-0.03, 0.04, 0.08], pe_multiples=[5.5, 6.8, 8.0])
    assert r["anchor"] == "PE"
    assert len(r["scenarios"]) == 3
    # 手算: 中性 = 5.7×(1+0.04)×6.8 = 40.3104
    assert r["scenarios"][1]["target_price"] == pytest.approx(40.31, abs=0.02)
    assert r["scenarios"][1]["pe_multiple"] == 6.8
    assert r["scenarios"][1]["future_eps"] == pytest.approx(5.93, abs=0.01)


# ── PE 锚(新调用) ────────────────────────────────────────────

def test_pe_anchor():
    r = three_scenario(price=1302.8, shares_100m=12.5, growth_rates=[0.2, 0.14, 0.05],
                       multiples=[22, 18.4, 15], anchor="PE", eps=65.66)
    assert r["anchor"] == "PE"
    assert r["current_eps"] == 65.66
    # 乐观 = 65.66×1.2×22 = 1733.4
    assert r["scenarios"][0]["target_price"] == pytest.approx(1733.4, abs=0.5)


# ── PB 锚(银行/周期股) ───────────────────────────────────────

def test_pb_anchor():
    """招商银行场景: 银行主用 PB。"""
    r = three_scenario(price=39.8, shares_100m=252.2, growth_rates=[-0.02, 0.03, 0.06],
                       multiples=[0.75, 0.90, 1.05], anchor="PB", bvps=44.9)
    assert r["anchor"] == "PB"
    assert r["current_bvps"] == 44.9
    # 中性 = 44.9×1.03×0.90 = 41.62
    assert r["scenarios"][1]["target_price"] == pytest.approx(41.62, abs=0.02)
    assert r["scenarios"][1]["pb_multiple"] == 0.90
    assert r["scenarios"][1]["future_bvps"] == pytest.approx(46.25, abs=0.01)
    # 上涨空间: (41.62-39.8)/39.8 = 4.6%
    assert r["scenarios"][1]["upside_pct"] == pytest.approx(4.6, abs=0.1)


# ── PS 锚(轻资产/互联网) ─────────────────────────────────────

def test_ps_anchor():
    """金山办公场景: 轻资产主用 PS。"""
    r = three_scenario(price=233.03, shares_100m=4.64, growth_rates=[0.3, 0.25, 0.1],
                       multiples=[20, 16, 12], anchor="PS", revenue_per_share=14.2)
    assert r["anchor"] == "PS"
    # 中性 = 14.2×1.25×16 = 284.0
    assert r["scenarios"][1]["target_price"] == pytest.approx(284.0, abs=0.05)
    assert r["scenarios"][1]["ps_multiple"] == 16
    assert r["scenarios"][1]["future_revenue_per_share"] == pytest.approx(17.75, abs=0.01)


# ── 错误处理 ─────────────────────────────────────────────────

def test_pb_missing_bvps():
    r = three_scenario(price=10, shares_100m=10, growth_rates=[0, 0, 0],
                       multiples=[1, 1, 1], anchor="PB")
    assert "error" in r
    assert "bvps" in r["error"]


def test_ps_missing_revenue():
    r = three_scenario(price=10, shares_100m=10, growth_rates=[0, 0, 0],
                       multiples=[1, 1, 1], anchor="PS")
    assert "error" in r
    assert "revenue_per_share" in r["error"]


def test_unknown_anchor():
    r = three_scenario(price=10, shares_100m=10, growth_rates=[0, 0, 0],
                       multiples=[1, 1, 1], anchor="XYZ", eps=1)
    assert "error" in r
    assert "XYZ" in r["error"]


def test_pe_missing_eps_new_style():
    """新调用 anchor=PE 但未传 eps → 报错。"""
    r = three_scenario(price=10, shares_100m=10, growth_rates=[0, 0, 0],
                       multiples=[1, 1, 1], anchor="PE")
    assert "error" in r


def test_scenario_count_limited():
    """超过3个增长率/倍数只取前3个。"""
    r = three_scenario(price=10, shares_100m=10, growth_rates=[0, 0, 0, 0, 0],
                       multiples=[1, 1, 1, 1, 1], anchor="PE", eps=1)
    assert len(r["scenarios"]) == 3


def test_market_cap():
    """市值 = 目标价 × 股本(亿股)。"""
    r = three_scenario(price=10, shares_100m=100, growth_rates=[0.5, 0, 0],
                       multiples=[2, 2, 2], anchor="PE", eps=5)
    # 乐观 = 5×1.5×2 = 15元, 市值 = 15×100亿 = 1500亿
    assert r["scenarios"][0]["target_price"] == pytest.approx(15.0)
    assert r["scenarios"][0]["market_cap"] == pytest.approx(1500.0, abs=1)
