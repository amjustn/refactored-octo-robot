"""reverse_gordon（反向戈登 / DDM 敏感性）测试。

原则（对着"禁止口算、必须真算"这条硬规则设计）:
  * 本文件里**不出现任何写死的期望数字**。所有期望值由 tests 内部的
    Fraction 精确分数运算参考实现当场算出（与生产代码的 Decimal 路径独立），
    再用 pytest.approx 对拍。
  * 覆盖: 反解公式 / 目标价公式 / 三档默认网格 / 单调性 /
    r>g 铁律 / 不分红拒绝 / 非法输入拒绝 / 风险补偿 / 异常高股息率警告 /
    负隐含增长率警告 / 股本市值 / 假设回显。
"""
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.tools.financial_rigor import reverse_gordon, three_scenario

# ── 参考实现（精确分数，独立于生产 Decimal 路径） ─────────────


def F(x):
    """把 int/float/str 转成精确分数（用 str() 避免二进制浮点噪声）。"""
    return Fraction(str(x))


def ref_div_yield_pct(price, dps):
    return F(dps) / F(price) * 100


def ref_implied_growth_pct(price, dps, r):
    """g = r - D1/P，单位 %。"""
    return (F(r) - F(dps) / F(price)) * 100


def ref_target_price(dps, r, g):
    """P = D1/(r-g)，单位元。"""
    return F(dps) / (F(r) - F(g))


def ref_upside_pct(price, dps, r, g):
    return (ref_target_price(dps, r, g) - F(price)) / F(price) * 100


# ── 常用算例（仅参数，无期望值） ─────────────────────────────

BANK = dict(price=5.30, dps=0.31, rates=[0.08, 0.09, 0.10], shares=3564.0)   # 类银行高股息
UTIL = dict(price=27.50, dps=0.90, rates=[0.065, 0.075, 0.085], shares=244.0)  # 类水电


# ── 1. 反解与目标价公式 ─────────────────────────────────────

def test_implied_growth_matches_fraction_reference():
    res = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"])
    assert "error" not in res
    assert len(res["implied"]) == len(BANK["rates"])
    for item, r in zip(res["implied"], BANK["rates"]):
        assert item["implied_growth_pct"] == pytest.approx(
            float(ref_implied_growth_pct(BANK["price"], BANK["dps"], r)), abs=0.005)
        assert item["discount_rate_pct"] == pytest.approx(float(F(r) * 100), abs=0.005)


def test_target_price_matches_fraction_reference():
    grid = [0.01, 0.02, 0.03]
    res = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"], growth_grid=grid)
    assert "error" not in res
    for row, r in zip(res["sensitivity"], BANK["rates"]):
        for cell, g in zip(row["cells"], grid):
            assert cell["model_failed"] is False
            assert cell["target_price"] == pytest.approx(
                float(ref_target_price(BANK["dps"], r, g)), abs=0.01)
            assert cell["upside_pct"] == pytest.approx(
                float(ref_upside_pct(BANK["price"], BANK["dps"], r, g)), abs=0.05)


def test_roundtrip_price_times_r_minus_g_equals_dividend():
    """每格回代: 目标价 × (r-g) 必须等于每股分红（相对误差 <1%）。"""
    res = reverse_gordon(UTIL["price"], UTIL["dps"], UTIL["rates"])
    assert "error" not in res
    for row in res["sensitivity"]:
        r = F(row["discount_rate_pct"]) / 100
        for cell in row["cells"]:
            if cell["model_failed"]:
                continue
            g = F(cell["growth_pct"]) / 100
            lhs = F(cell["target_price"]) * (r - g)
            assert abs(lhs - F(UTIL["dps"])) / F(UTIL["dps"]) < Fraction(1, 100)


# ── 2. 默认网格与内部自洽 ───────────────────────────────────

def test_default_grid_spans_one_point_percent():
    """不传 growth_grid 时，网格 = 中档贴现率的隐含增长率 ±1 个百分点。"""
    res = reverse_gordon(UTIL["price"], UTIL["dps"], UTIL["rates"])
    center_r = UTIL["rates"][len(UTIL["rates"]) // 2]
    center_g = ref_implied_growth_pct(UTIL["price"], UTIL["dps"], center_r)
    assert len(res["growth_grid_pct"]) == 3
    for got, exp in zip(res["growth_grid_pct"], [center_g - 1, center_g, center_g + 1]):
        assert got == pytest.approx(float(exp), abs=0.005)


def test_sensitivity_center_cell_reproduces_price():
    """中档贴现率 × 隐含增长率那一格必须复现当前市价（模型自洽的硬校验）。"""
    res = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"])
    center_row = res["sensitivity"][len(BANK["rates"]) // 2]
    center_cell = center_row["cells"][1]
    assert center_cell["model_failed"] is False
    assert center_cell["target_price"] == pytest.approx(BANK["price"], abs=0.02)
    assert center_cell["upside_pct"] == pytest.approx(0.0, abs=0.5)


# ── 3. 单调性（用代码判定，不靠人眼） ───────────────────────

def test_monotonic_in_growth():
    res = reverse_gordon(UTIL["price"], UTIL["dps"], UTIL["rates"], growth_grid=[0.005, 0.02, 0.035])
    for row in res["sensitivity"]:
        prices = [c["target_price"] for c in row["cells"]]
        assert prices == sorted(prices)
        assert prices[0] < prices[-1]


def test_monotonic_in_discount_rate():
    grid = [0.01, 0.02, 0.03]
    res = reverse_gordon(UTIL["price"], UTIL["dps"], UTIL["rates"], growth_grid=grid)
    first_growth_prices = [row["cells"][0]["target_price"] for row in res["sensitivity"]]
    assert first_growth_prices == sorted(first_growth_prices, reverse=True)
    assert first_growth_prices[0] > first_growth_prices[-1]


# ── 4. 铁律与输入校验 ───────────────────────────────────────

def test_zero_dividend_rejected():
    res = reverse_gordon(10.0, 0.0, [0.08, 0.09])
    assert "error" in res
    assert "无意义" in res["error"]
    assert "sensitivity" not in res  # 拒绝时不产出任何编造的目标价


def test_negative_dividend_rejected():
    res = reverse_gordon(10.0, -0.5, [0.08, 0.09])
    assert "error" in res
    assert "sensitivity" not in res


def test_nonpositive_price_rejected():
    assert "error" in reverse_gordon(0.0, 0.5, [0.08])
    assert "error" in reverse_gordon(-3.0, 0.5, [0.08])


def test_nonpositive_discount_rate_rejected():
    assert "error" in reverse_gordon(10.0, 0.5, [0.0, 0.08])
    assert "error" in reverse_gordon(10.0, 0.5, [-0.02, 0.08])


def test_empty_rates_rejected():
    assert "error" in reverse_gordon(10.0, 0.5, [])
    assert "error" in reverse_gordon(10.0, 0.5, [0.08], growth_grid=[])


def test_g_ge_r_cell_marked_failed():
    res = reverse_gordon(10.0, 0.5, [0.05, 0.08], growth_grid=[0.02, 0.06, 0.09])
    assert "error" not in res
    for row, r in zip(res["sensitivity"], [0.05, 0.08]):
        for cell in row["cells"]:
            if F(cell["growth_pct"]) / 100 >= F(r):
                assert cell["model_failed"] is True
                assert "target_price" not in cell
                assert "reason" in cell
            else:
                assert cell["model_failed"] is False
                assert "target_price" in cell


def test_all_cells_failed_returns_error():
    res = reverse_gordon(10.0, 0.5, [0.03], growth_grid=[0.05, 0.06, 0.07])
    assert "error" in res
    assert "g >= r" in res["error"] or "戈登模型失效" in res["error"]


# ── 5. 风险补偿/利差、警告、股本 ─────────────────────────────

def test_risk_free_spread_and_premium():
    rf = 0.018
    res = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"], risk_free_rate=rf)
    assert "error" not in res
    assert res["risk_free_rate_pct"] == pytest.approx(float(F(rf) * 100), abs=0.005)
    for item, r in zip(res["implied"], BANK["rates"]):
        assert item["risk_premium_pct"] == pytest.approx(float((F(r) - F(rf)) * 100), abs=0.01)
        expected_spread = ref_div_yield_pct(BANK["price"], BANK["dps"]) - F(rf) * 100
        assert item["dividend_yield_spread_pct"] == pytest.approx(float(expected_spread), abs=0.01)
    assert res["dividend_yield_pct"] == pytest.approx(
        float(ref_div_yield_pct(BANK["price"], BANK["dps"])), abs=0.005)


def test_high_dividend_yield_warns():
    # 股息率 = dps/price 自动算出超过 12%（不写死数字：用参考实现判定）
    price, dps = 10.0, 1.5
    assert float(ref_div_yield_pct(price, dps)) > 12
    res = reverse_gordon(price, dps, [0.08, 0.10, 0.12])
    assert "error" not in res
    assert any("特别股息" in w or "口径" in w for w in res["warnings"])


def test_negative_implied_growth_warns_and_still_prices():
    price, dps, rates = 10.0, 0.9, [0.04, 0.06, 0.08]
    assert all(float(ref_implied_growth_pct(price, dps, r)) < 0 for r in rates)
    res = reverse_gordon(price, dps, rates)
    assert "error" not in res
    assert any("负的隐含增长率" in w for w in res["warnings"])
    assert all(c["target_price"] > 0 for row in res["sensitivity"] for c in row["cells"])


def test_market_cap_when_shares_given():
    res = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"],
                         shares_100m=BANK["shares"])
    assert "error" not in res
    for row, r in zip(res["sensitivity"], BANK["rates"]):
        for cell in row["cells"]:
            if cell["model_failed"]:
                continue
            expected = ref_target_price(BANK["dps"], r, F(cell["growth_pct"]) / 100) * F(BANK["shares"])
            assert cell["market_cap"] == pytest.approx(float(expected), rel=0.01)
    # 不传股本时不产生该字段（避免无依据的数字）
    res2 = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"])
    assert res2["shares_100m"] is None
    assert all("market_cap" not in c for row in res2["sensitivity"] for c in row["cells"])


def test_assumptions_echoed():
    """假设必须回显（r 档位、网格、模型出处），便于报告留痕。"""
    rf = 0.018
    res = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"], risk_free_rate=rf)
    assert res["discount_rates_pct"] == [pytest.approx(float(F(r) * 100), abs=0.005) for r in BANK["rates"]]
    assert res["model"].startswith("Gordon growth (1959)")
    assert res["current_price"] == pytest.approx(BANK["price"], abs=0.001)
    assert res["dividend_per_share"] == pytest.approx(BANK["dps"], abs=0.001)


# ── 6. three_scenario 的 DDM 锚（接入面） ────────────────────

def test_ddm_anchor_delegates_to_reverse_gordon():
    grid = [0.01, 0.02, 0.03]
    res = three_scenario(price=BANK["price"], shares_100m=BANK["shares"],
                         growth_rates=grid, anchor="DDM",
                         dps=BANK["dps"], discount_rates=BANK["rates"])
    assert res["anchor"] == "DDM"
    assert res["function"] == "three-scenario"
    assert "implied" in res and "sensitivity" in res
    assert "scenarios" not in res  # DDM 不给单点目标价情景
    direct = reverse_gordon(BANK["price"], BANK["dps"], BANK["rates"],
                            growth_grid=grid, shares_100m=BANK["shares"])
    for key in ("implied", "sensitivity", "dividend_yield_pct", "growth_grid_pct"):
        assert res[key] == direct[key]


def test_ddm_anchor_default_grid_center_reproduces_price():
    res = three_scenario(price=BANK["price"], shares_100m=BANK["shares"],
                         growth_rates=[], anchor="DDM",
                         dps=BANK["dps"], discount_rates=BANK["rates"])
    assert "error" not in res
    assert len(res["growth_grid_pct"]) == 3
    center = res["sensitivity"][len(BANK["rates"]) // 2]["cells"][1]
    assert center["target_price"] == pytest.approx(BANK["price"], abs=0.02)


def test_ddm_anchor_requires_dps_and_rates():
    r1 = three_scenario(price=5.3, shares_100m=3564.0, growth_rates=[0.01], anchor="DDM")
    assert "error" in r1 and "dps" in r1["error"]
    r2 = three_scenario(price=5.3, shares_100m=3564.0, growth_rates=[0.01],
                        anchor="DDM", dps=0.31)
    assert "error" in r2 and "discount_rates" in r2["error"]


def test_ddm_anchor_rejects_zero_dividend():
    res = three_scenario(price=10.0, shares_100m=100.0, growth_rates=[0.02],
                         anchor="DDM", dps=0.0, discount_rates=[0.08])
    assert "error" in res
    assert res["anchor"] == "DDM"
    assert "sensitivity" not in res


def test_multiple_anchors_unchanged():
    """PE/PB/PS 三锚行为不变（接入 DDM 不得回归）。"""
    pe = three_scenario(price=39.8, eps=5.7, shares_100m=252.2,
                        growth_rates=[-0.03, 0.04, 0.08], pe_multiples=[5.5, 6.8, 8.0])
    assert pe["anchor"] == "PE"
    assert len(pe["scenarios"]) == 3
    pb = three_scenario(price=10.0, shares_100m=10.0, growth_rates=[0, 0, 0],
                        anchor="PB", bvps=5.0, multiples=[1, 1, 1])
    assert pb["anchor"] == "PB"
    assert len(pb["scenarios"]) == 3
    ps = three_scenario(price=10.0, shares_100m=10.0, growth_rates=[0, 0, 0],
                        anchor="PS", revenue_per_share=2.0, multiples=[5, 5, 5])
    assert ps["anchor"] == "PS"
    assert ps["scenarios"][0]["target_price"] == pytest.approx(10.0, abs=0.01)
