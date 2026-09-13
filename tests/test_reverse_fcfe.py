"""reverse_fcfe（反向 FCFE / 股东自由现金流敏感性）测试。

原则（对着"禁止口算、必须真算"这条硬规则设计）:
  * 本文件里**不出现任何写死的期望数字**。所有期望值由 tests 内部的
    Fraction 精确分数运算参考实现当场算出（与生产代码的 Decimal 路径独立），
    再用 pytest.approx 对拍。
  * 覆盖: 反解公式 / 目标价公式 / 三档默认网格 / 单调性 / r>g 铁律 /
    FCFE<=0 拒绝 / 非法输入拒绝 / 风险补偿 / 异常高 FCFE 收益率警告 /
    负隐含增长率警告 / 股本市值 / 假设回显 / FCFE 锚委派 /
    与 DDM 的边界（低分红高现金流公司 FCFE 可用、DDM 被拒）。
"""
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.tools.financial_rigor import reverse_fcfe, reverse_gordon, three_scenario


def F(x):
    """把 int/float/str 转成精确分数（用 str() 避免二进制浮点噪声）。"""
    return Fraction(str(x))


def ref_fcfe_yield_pct(price, fcfe):
    return F(fcfe) / F(price) * 100


def ref_implied_growth_pct(price, fcfe, r):
    """g = r_e - FCFE1/P，单位 %。"""
    return (F(r) - F(fcfe) / F(price)) * 100


def ref_target_price(fcfe, r, g):
    """P = FCFE1/(r_e-g)，单位元。"""
    return F(fcfe) / (F(r) - F(g))


def ref_upside_pct(price, fcfe, r, g):
    return (ref_target_price(fcfe, r, g) - F(price)) / F(price) * 100


# ── 反解与目标价 ───────────────────────────────────────────

def test_implied_growth_matches_fraction_reference():
    price, fcfe, rates = 100.0, 6.0, [0.08, 0.10, 0.12]
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=rates)
    for item, r in zip(out["implied"], rates):
        assert item["implied_growth_pct"] == pytest.approx(
            float(ref_implied_growth_pct(price, fcfe, r)), abs=1e-9)


def test_target_price_matches_fraction_reference():
    price, fcfe, r, g = 100.0, 6.0, 0.10, 0.04
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[r],
                       growth_grid=[g])
    cell = out["sensitivity"][0]["cells"][0]
    assert cell["target_price"] == pytest.approx(float(ref_target_price(fcfe, r, g)), abs=0.005)
    assert cell["upside_pct"] == pytest.approx(float(ref_upside_pct(price, fcfe, r, g)), abs=0.05)


def test_roundtrip_price_times_r_minus_g_equals_fcfe():
    """反向解出的隐含 g 代回正向公式必须还原市价（容差来自 g 输出保留 2 位小数：
    ΔP ≈ P·Δg/(r−g)，本例 ≈ 58×1e-4/0.0776 ≈ 0.075 元）。"""
    price, fcfe, r = 58.0, 4.5, 0.11
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[r])
    g = Fraction(out["implied"][0]["implied_growth_pct"]) / 100
    reconstructed = float(F(fcfe) / (F(r) - g))
    assert reconstructed == pytest.approx(price, abs=0.15)
    # 未取整路径必须精确：用 Fraction 参考解反解，代回后应严格等于市价
    g_exact = (F(r) - F(fcfe) / F(price))
    assert float(F(fcfe) / (F(r) - g_exact)) == pytest.approx(price, abs=1e-9)


# ── 默认网格与单调性 ───────────────────────────────────────

def test_default_grid_spans_one_point_percent():
    price, fcfe, rates = 100.0, 6.0, [0.08, 0.10, 0.12]
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=rates)
    center = out["implied"][len(rates) // 2]["implied_growth_pct"]   # 已四舍五入到 2 位
    assert out["growth_grid_pct"] == [round(center - 1, 2), center, round(center + 1, 2)]


def test_sensitivity_center_cell_reproduces_price():
    price, fcfe, r = 100.0, 6.0, 0.10
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[r])
    center_cell = out["sensitivity"][0]["cells"][1]
    assert center_cell["target_price"] == pytest.approx(price, abs=0.01)


def test_monotonic_in_growth():
    price, fcfe, r = 100.0, 6.0, 0.10
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[r],
                       growth_grid=[0.02, 0.03, 0.04])
    targets = [c["target_price"] for c in out["sensitivity"][0]["cells"]]
    assert targets == sorted(targets) and len(set(targets)) == 3


def test_monotonic_in_discount_rate():
    price, fcfe, g = 100.0, 6.0, 0.03
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[0.08, 0.10, 0.12],
                       growth_grid=[g])
    targets = [row["cells"][0]["target_price"] for row in out["sensitivity"]]
    assert targets == sorted(targets, reverse=True)


def test_implied_growth_increases_with_discount_rate():
    """g_implied = r_e - FCFE/P：FCFE 收益率固定时，折现率越高，隐含增长率越高。"""
    price, fcfe = 100.0, 6.0
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[0.08, 0.10, 0.12])
    gs = [x["implied_growth_pct"] for x in out["implied"]]
    assert gs == sorted(gs)
    assert gs[0] == pytest.approx(float(ref_implied_growth_pct(price, fcfe, 0.08)), abs=1e-9)


# ── 铁律与拒绝 ─────────────────────────────────────────────

def test_zero_fcfe_rejected():
    out = reverse_fcfe(price=100.0, fcfe_per_share=0.0, discount_rates=[0.10])
    assert "error" in out and "FCFE" in out["error"]


def test_negative_fcfe_rejected():
    """负 FCFE（扩产期/周期底）必须被拒，并提示改用其它锚。"""
    out = reverse_fcfe(price=100.0, fcfe_per_share=-3.2, discount_rates=[0.10])
    assert "error" in out
    assert "PB/PE/PS" in out["error"] or "正常化" in out["error"]


def test_nonpositive_price_rejected():
    assert "error" in reverse_fcfe(price=0.0, fcfe_per_share=5.0, discount_rates=[0.10])
    assert "error" in reverse_fcfe(price=-1.0, fcfe_per_share=5.0, discount_rates=[0.10])


def test_nonpositive_discount_rate_rejected():
    assert "error" in reverse_fcfe(price=100.0, fcfe_per_share=5.0, discount_rates=[0.0])
    assert "error" in reverse_fcfe(price=100.0, fcfe_per_share=5.0, discount_rates=[-0.02])


def test_empty_rates_rejected():
    assert "error" in reverse_fcfe(price=100.0, fcfe_per_share=5.0, discount_rates=[])


def test_empty_growth_grid_rejected():
    out = reverse_fcfe(price=100.0, fcfe_per_share=5.0, discount_rates=[0.10], growth_grid=[])
    assert "error" in out


def test_g_ge_r_cell_marked_failed():
    price, fcfe, r = 100.0, 6.0, 0.05
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[r],
                       growth_grid=[0.04, 0.05, 0.06])
    cells = out["sensitivity"][0]["cells"]
    assert cells[0]["model_failed"] is False
    assert cells[1]["model_failed"] is True and "target_price" not in cells[1]
    assert cells[2]["model_failed"] is True and "target_price" not in cells[2]


def test_all_cells_failed_returns_error():
    out = reverse_fcfe(price=100.0, fcfe_per_share=0.5, discount_rates=[0.02],
                       growth_grid=[0.02, 0.03, 0.04])
    assert "error" in out and "失效" in out["error"]


# ── 警告与附加信息 ─────────────────────────────────────────

def test_risk_free_spread_and_premium():
    price, fcfe, rf = 100.0, 6.0, 0.02
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[0.08, 0.10],
                       risk_free_rate=rf)
    for item, r in zip(out["implied"], (0.08, 0.10)):
        assert item["risk_premium_pct"] == pytest.approx(float((F(r) - F(rf)) * 100), abs=1e-9)
        assert item["fcfe_yield_spread_pct"] == pytest.approx(
            float(ref_fcfe_yield_pct(price, fcfe) - F(rf) * 100), abs=1e-9)


def test_high_fcfe_yield_warns():
    out = reverse_fcfe(price=100.0, fcfe_per_share=18.0, discount_rates=[0.20])
    assert any("FCFE 收益率" in w for w in out["warnings"])


def test_negative_implied_growth_warns_and_still_prices():
    price, fcfe = 100.0, 12.0
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[0.05])
    assert any("负的隐含增长率" in w for w in out["warnings"])
    assert "sensitivity" in out and out["implied"][0]["implied_growth_pct"] < 0


def test_risk_free_above_rates_warns():
    out = reverse_fcfe(price=100.0, fcfe_per_share=3.0, discount_rates=[0.03],
                       risk_free_rate=0.045)
    assert any("无风险利率" in w for w in out["warnings"])


def test_market_cap_when_shares_given():
    price, fcfe, shares_100m = 100.0, 6.0, 10.0
    out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[0.10],
                       shares_100m=shares_100m)
    cell = out["sensitivity"][0]["cells"][1]
    assert cell["market_cap"] == pytest.approx(cell["target_price"] * shares_100m, rel=1e-6)


def test_assumptions_echoed():
    out = reverse_fcfe(price=100.0, fcfe_per_share=6.0, discount_rates=[0.09, 0.10, 0.11],
                       risk_free_rate=0.02, shares_100m=12.2, currency="CNY")
    assert out["current_price"] == 100.0
    assert out["fcfe_per_share"] == 6.0
    assert out["shares_100m"] == 12.2
    assert out["currency"] == "CNY"
    assert out["discount_rates_pct"] == [9.0, 10.0, 11.0]
    assert out["risk_free_rate_pct"] == 2.0
    assert out["model"].startswith("FCFE")


def test_fcfe_basis_echoed():
    out = reverse_fcfe(price=100.0, fcfe_per_share=6.0, discount_rates=[0.10],
                       fcfe_basis="TTM")
    assert out["fcfe_basis"] == "TTM"


# ── 与 three_scenario 的接线 ────────────────────────────────

def test_fcfe_anchor_delegates_to_reverse_fcfe():
    price, fcfe, rates, sh = 100.0, 6.0, [0.09, 0.10, 0.11], 12.2
    direct = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=rates, shares_100m=sh)
    out = three_scenario(price=price, shares_100m=sh, growth_rates=[],
                         anchor="FCFE", fcfe_per_share=fcfe, discount_rates=rates)
    assert out["function"] == "three-scenario" and out["anchor"] == "FCFE"
    assert out["implied"] == direct["implied"]
    assert out["sensitivity"] == direct["sensitivity"]


def test_fcfe_anchor_requires_fcfe_and_rates():
    a = three_scenario(price=100.0, shares_100m=1.0, growth_rates=[], anchor="FCFE",
                       discount_rates=[0.10])
    assert "error" in a and "fcfe_per_share" in a["error"]
    b = three_scenario(price=100.0, shares_100m=1.0, growth_rates=[], anchor="FCFE",
                       fcfe_per_share=6.0)
    assert "error" in b and "discount_rates" in b["error"]


def test_ddm_vs_fcfe_boundary_low_dividend_company():
    """边界用例（真实场景：澜起这类股息率 ~0.2% 的公司）。

    同一份价格/现金流下：DDM 因每股分红接近 0 会被拒或反解出极端噪声；
    FCFE 正常给出隐含增长率 —— 这正是加这个锚的意义。
    """
    price, d1, fcfe = 191.96, 0.39, 5.0
    ddm = reverse_gordon(price=price, dividend_per_share=d1, discount_rates=[0.09, 0.10, 0.11])
    fcfe_out = reverse_fcfe(price=price, fcfe_per_share=fcfe, discount_rates=[0.09, 0.10, 0.11])
    assert "error" not in fcfe_out
    # DDM 在极低分红下反解出的 g 会贴着折现率（≈ r - 0.2%），几乎等于"永续零增长-折现率"
    ddm_g = ddm["implied"][1]["implied_growth_pct"]
    fcfe_g = fcfe_out["implied"][1]["implied_growth_pct"]
    assert ddm_g > 9.0                    # DDM 反解的 g 被 0.2% 股息率顶到折现率附近
    assert fcfe_g < ddm_g                 # FCFE 因分子更大，反解出的 g 明显更低
