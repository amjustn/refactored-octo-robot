"""Financial Rigor Tools - Adapted for AI Berkshire Web
Provides precise decimal arithmetic for financial calculations.
"""
import ast
import operator
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

# Safe AST-based expression evaluator
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Pow: operator.pow,
}


def _safe_eval(node):
    """Recursively evaluate an AST node to a float."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"Unsupported expression element: {type(node).__name__}")


def verify_market_cap(price: float, shares: float, reported: float, currency: str = "USD") -> dict:
    """Verify market cap = price * shares. Returns deviation and pass/fail.

    If reported is 0, the check fails (cannot verify against zero).
    """
    p = Decimal(str(price))
    s = Decimal(str(shares))
    r = Decimal(str(reported))

    calc = p * s

    # If reported is 0, we can't compute deviation — should fail
    if r == 0:
        return {
            "function": "verify-market-cap",
            "price": float(p),
            "shares": float(s),
            "calculated": float(calc.quantize(Decimal('0.01'), ROUND_HALF_UP)),
            "reported": float(r),
            "deviation_pct": 100.0,
            "currency": currency,
            "passed": False,
            "message": "❌ FAIL (reported market cap is 0, cannot verify)"
        }

    deviation = abs(calc - r) / r

    return {
        "function": "verify-market-cap",
        "price": float(p),
        "shares": float(s),
        "calculated": float(calc.quantize(Decimal('0.01'), ROUND_HALF_UP)),
        "reported": float(r),
        "deviation_pct": round(float(deviation * 100), 4),
        "currency": currency,
        "passed": deviation <= Decimal('0.01'),
        "message": f"✅ PASS (deviation: {float(deviation*100):.2f}%)" if deviation <= Decimal('0.01')
        else f"❌ FAIL (deviation: {float(deviation*100):.2f}%)"
    }


def verify_valuation(price: float, eps: Optional[float] = None,
                     bvps: Optional[float] = None,
                     fcf_per_share: Optional[float] = None,
                     dividend: Optional[float] = None) -> dict:
    """Calculate key valuation ratios."""
    p = Decimal(str(price))
    result = {"function": "verify-valuation", "price": float(p)}

    if eps and Decimal(str(eps)) != 0:
        pe = p / Decimal(str(eps))
        result["PE"] = round(float(pe), 2)
    if bvps and Decimal(str(bvps)) != 0:
        pb = p / Decimal(str(bvps))
        result["PB"] = round(float(pb), 2)
    if fcf_per_share and Decimal(str(fcf_per_share)) != 0:
        fcf_yield = (Decimal(str(fcf_per_share)) / p) * 100
        result["FCF_Yield"] = round(float(fcf_yield), 2)
    if dividend and p != 0:
        div_yield = (Decimal(str(dividend)) / p) * 100
        result["Dividend_Yield"] = round(float(div_yield), 2)

    return result


def cross_validate(field: str, values: dict, unit: str = "") -> dict:
    """Cross-validate a metric from multiple sources.

    Compares ALL pairs of sources and reports the maximum deviation.
    Supports 2+ sources (previous version only compared first 2).
    """
    vals = {k: Decimal(str(v)) for k, v in values.items()}
    sources = list(vals.keys())

    if len(sources) < 2:
        return {"function": "cross-validate", "field": field, "error": "Need at least 2 sources"}

    # Compare all pairs and find maximum deviation
    max_dev_pct = Decimal('0')
    max_pair = ""
    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            v1, v2 = vals[sources[i]], vals[sources[j]]
            if v1 == 0 and v2 == 0:
                continue
            base = v1 if v1 != 0 else v2
            dev = abs(v1 - v2) / abs(base)
            if dev > max_dev_pct:
                max_dev_pct = dev
                max_pair = f"{sources[i]} vs {sources[j]}"

    dev_pct = round(float(max_dev_pct * 100), 2)

    if dev_pct <= 1:
        status = "✅ PASS"
    elif dev_pct <= 5:
        status = "⚠️ WARN (possible GAAP/non-GAAP or exchange rate)"
    else:
        status = "❌ FAIL (verify from primary source)"

    return {
        "function": "cross-validate",
        "field": field,
        "unit": unit,
        "sources": {k: float(v) for k, v in vals.items()},
        "max_deviation_pct": dev_pct,
        "max_deviation_pair": max_pair,
        "source_count": len(sources),
        "status": status
    }


def three_scenario(price: float, shares_100m: float, growth_rates: list,
                   multiples: Optional[list] = None, anchor: str = "PE",
                   eps: Optional[float] = None, bvps: Optional[float] = None,
                   revenue_per_share: Optional[float] = None, pe_multiples: Optional[list] = None,
                   currency: str = "CNY", dps: Optional[float] = None,
                   discount_rates: Optional[list] = None,
                   risk_free_rate: Optional[float] = None) -> dict:
    """三情景估值: 乐观/中性/悲观, 返回目标价和上涨空间。

    支持四种估值锚(按公司类型动态选择, 与 financial-analyst 的
    "估值指标行业适配表"对应):
      anchor="PE" — 目标价 = 未来EPS × PE倍数(传统重资产/制造/消费)
      anchor="PB" — 目标价 = 未来每股净资产(BVPS) × PB倍数(银行/保险/周期股)
      anchor="PS" — 目标价 = 未来每股营收 × PS倍数(轻资产/互联网/软件)
      anchor="DDM" — 反向戈登(1959)敏感性: 需 dps(预期未来12个月每股分红 D1)
                     + discount_rates(三档贴现率), growth_rates 作为增长率网格
                     (可省, 默认=中档贴现率的隐含增长率 ±1 个百分点);
                     返回"隐含增长率 + 敏感性矩阵", 不返回单点目标价;
                     不分红/象征性分红的标的会被直接拒绝(DDM 对其无意义)

    向后兼容: 旧调用(eps + pe_multiples 风格)自动识别为 PE 锚。
    """
    # DDM(反向戈登)分支: 输出结构与倍数锚不同(隐含 + 敏感性矩阵), 需前置处理
    if (anchor or "").upper() == "DDM":
        if dps is None:
            return {"function": "three-scenario", "anchor": "DDM",
                    "error": "anchor=DDM 需要提供 dps(预期未来 12 个月的每股分红 D1)"}
        if not discount_rates:
            return {"function": "three-scenario", "anchor": "DDM",
                    "error": "anchor=DDM 需要提供 discount_rates(贴现率档, 建议三档)"}
        out = reverse_gordon(price=price, dividend_per_share=dps,
                             discount_rates=discount_rates,
                             risk_free_rate=risk_free_rate,
                             growth_grid=growth_rates if growth_rates else None,
                             shares_100m=shares_100m, currency=currency)
        out["function"] = "three-scenario"
        out["anchor"] = "DDM"
        return out

    # 兼容旧调用: 传了 pe_multiples 即按 PE 锚处理
    if pe_multiples is not None:
        multiples = pe_multiples
        anchor = "PE"
    if multiples is None:
        return {"function": "three-scenario", "error": "缺少 multiples(估值倍数数组)"}

    anchor = (anchor or "PE").upper()
    if anchor == "PE":
        if eps is None:
            return {"function": "three-scenario", "error": "anchor=PE 需要提供 eps"}
        base_name, base = "eps", Decimal(str(eps))
        multiple_name = "pe_multiple"
    elif anchor == "PB":
        if bvps is None:
            return {"function": "three-scenario", "error": "anchor=PB 需要提供 bvps(每股净资产)"}
        base_name, base = "bvps", Decimal(str(bvps))
        multiple_name = "pb_multiple"
    elif anchor == "PS":
        if revenue_per_share is None:
            return {"function": "three-scenario", "error": "anchor=PS 需要提供 revenue_per_share(每股营收)"}
        base_name, base = "revenue_per_share", Decimal(str(revenue_per_share))
        multiple_name = "ps_multiple"
    else:
        return {"function": "three-scenario", "error": f"未知 anchor: {anchor}(可选 PE/PB/PS/DDM)"}

    p = Decimal(str(price))
    sh = Decimal(str(shares_100m))

    labels = ["optimistic", "neutral", "pessimistic"]
    scenarios = []

    for i, (growth, mult) in enumerate(zip(growth_rates, multiples)):
        if i >= 3:
            break
        future_base = base * (1 + Decimal(str(growth)))
        target_price = future_base * Decimal(str(mult))
        upside = ((target_price - p) / p * 100) if p != 0 else Decimal('0')
        market_cap = target_price * sh

        scenarios.append({
            "scenario": labels[i] if i < len(labels) else f"scenario_{i}",
            "growth_rate": float(growth),
            multiple_name: float(mult),
            f"future_{base_name}": round(float(future_base), 2),
            "target_price": round(float(target_price), 2),
            "upside_pct": round(float(upside), 1),
            "market_cap": round(float(market_cap), 0),
        })

    return {
        "function": "three-scenario",
        "anchor": anchor,
        "current_price": float(p),
        f"current_{base_name}": float(base),
        "shares_100m": float(sh),
        "currency": currency,
        "scenarios": scenarios
    }


def reverse_gordon(price: float, dividend_per_share: float, discount_rates: list,
                   risk_free_rate: Optional[float] = None,
                   growth_grid: Optional[list] = None,
                   shares_100m: Optional[float] = None,
                   currency: str = "CNY") -> dict:
    """反向戈登(DDM)敏感性分析 — Gordon(1959) 常数增长模型。

    模型: P = D1 / (r - g)，等价 r = D1 / P + g
      * 正向: 给定 r、g 算目标价(sensitivity 部分)
      * 反向: 给定市价 P 与每股分红 D1，解出市场隐含增长率 g = r - D1/P

    为什么必须反向用: 正向单点目标价对 (r - g) 极度敏感(g 挪 0.5 个百分点, 估值差
    10%+)，单点数字等于给假设化妆；反向解出隐含假设 + 敏感性矩阵，才能回答
    "这个价格里市场信了什么、这个假设站不站得住"。

    口径与铁律:
      * dividend_per_share 必须是**预期未来 12 个月**的每股分红(D1)，不是已派发历史
      * D1 <= 0(不分红/象征性分红) → 直接拒绝: DDM 对这类公司无意义, 应改用 PB/PE/股息率
      * 每格必须满足 r > g; g >= r 的格子标记 model_failed 且不给目标价(模型发散)
      * 所有格子都失效 → 整体报错; r <= 0 / price <= 0 / rates 为空 → 报错
      * 股息率 > 12% → warning(可能含特别股息或分红口径有误)
      * 默认网格 = 中档贴现率下的隐含增长率 ± 1 个百分点(三点)
    """
    p = Decimal(str(price))
    if p <= 0:
        return {"function": "reverse-gordon", "error": "price 必须为正数"}

    dps = Decimal(str(dividend_per_share))
    if dps <= 0:
        return {"function": "reverse-gordon",
                "error": "不分红或每股分红 <= 0，戈登/DDM 模型无意义（应改用 PB/PE/股息率锚）"}

    if not discount_rates:
        return {"function": "reverse-gordon", "error": "缺少 discount_rates(贴现率数组)"}

    rates = [Decimal(str(x)) for x in discount_rates]
    if any(x <= 0 for x in rates):
        return {"function": "reverse-gordon", "error": "贴现率必须为正数"}

    div_yield = dps / p * 100
    implied = [r - dps / p for r in rates]

    if growth_grid is None:
        center = implied[len(rates) // 2]
        grid = [center - Decimal("0.01"), center, center + Decimal("0.01")]
    else:
        if not growth_grid:
            return {"function": "reverse-gordon", "error": "growth_grid 不能为空数组"}
        grid = [Decimal(str(x)) for x in growth_grid]

    warnings = []
    if div_yield > Decimal("12"):
        warnings.append(
            f"股息率 {float(div_yield):.2f}% 异常高：请核实是否含特别股息/一次性分红，"
            "或每股分红口径有误（D1 须为预期未来 12 个月）"
        )
    if any(g < 0 for g in implied):
        warnings.append(
            f"反解出现负的隐含增长率（最低 {float(min(implied)) * 100:.2f}%）："
            "按该贴现率市场已 price-in 分红下降，需与公司分红政策对照"
        )

    rows = []
    valid_cells = 0
    for r in rates:
        cells = []
        for g in grid:
            if g >= r:
                cells.append({
                    "growth_pct": round(float(g) * 100, 2),
                    "model_failed": True,
                    "reason": "g >= r，戈登模型发散(估值趋于无穷)，该格不给目标价",
                })
                continue
            target = dps / (r - g)
            upside = (target - p) / p * 100
            cell = {
                "growth_pct": round(float(g) * 100, 2),
                "target_price": round(float(target), 2),
                "upside_pct": round(float(upside), 1),
                "model_failed": False,
            }
            if shares_100m is not None:
                cell["market_cap"] = round(float(target * Decimal(str(shares_100m))), 0)
            cells.append(cell)
            valid_cells += 1
        rows.append({
            "discount_rate_pct": round(float(r) * 100, 2),
            "cells": cells,
        })

    if valid_cells == 0:
        return {"function": "reverse-gordon",
                "error": "全部情景满足 g >= r，戈登模型失效（检查增长/贴现率假设，或改用其它估值锚）"}

    rf = Decimal(str(risk_free_rate)) if risk_free_rate is not None else None
    implied_out = []
    for r, g in zip(rates, implied):
        item = {
            "discount_rate_pct": round(float(r) * 100, 2),
            "implied_growth_pct": round(float(g) * 100, 2),
        }
        if rf is not None:
            item["risk_premium_pct"] = round(float(r - rf) * 100, 2)
            item["dividend_yield_spread_pct"] = round(float(div_yield - rf * 100), 2)
        implied_out.append(item)

    if rf is not None and rf >= min(rates):
        warnings.append("无风险利率 >= 最低贴现率档，风险补偿为负，请检查输入")

    return {
        "function": "reverse-gordon",
        "model": "Gordon growth (1959): P = D1/(r-g)；反向解 g_implied = r - D1/P",
        "current_price": float(p),
        "dividend_per_share": float(dps),
        "dividend_yield_pct": round(float(div_yield), 2),
        "shares_100m": float(shares_100m) if shares_100m is not None else None,
        "currency": currency,
        "discount_rates_pct": [round(float(x) * 100, 2) for x in rates],
        "growth_grid_pct": [round(float(x) * 100, 2) for x in grid],
        "risk_free_rate_pct": round(float(rf) * 100, 2) if rf is not None else None,
        "implied": implied_out,
        "sensitivity": rows,
        "warnings": warnings,
    }


def calc(expression: str) -> dict:
    """Execute a precise financial calculation using safe AST parsing."""
    try:
        # Replace ^ with ** for convenience
        safe_expr = expression.replace('^', '**')
        tree = ast.parse(safe_expr, mode='eval')
        result = _safe_eval(tree.body)
        return {
            "function": "calc",
            "expression": expression,
            "result": float(Decimal(str(result)).quantize(Decimal('0.0001'), ROUND_HALF_UP))
        }
    except Exception as e:
        return {"function": "calc", "expression": expression, "error": str(e)}


def benford(numbers: list) -> dict:
    """Benford's Law check for detecting anomalies in financial data."""
    from collections import Counter
    if not numbers:
        return {"function": "benford", "error": "No numbers provided"}

    # Extract first digits
    first_digits = []
    for n in numbers:
        try:
            # Handle scientific notation properly: convert to plain decimal string
            val = abs(float(n))
            if val == 0:
                continue  # Skip zeros
            # Convert to string without scientific notation
            if val >= 1:
                s = f"{val:.0f}"
            else:
                # For numbers < 1, strip leading zeros after decimal point
                s = f"{val:.15f}".lstrip('0.')
            if s and s[0].isdigit():
                first_digits.append(int(s[0]))
        except (ValueError, TypeError):
            continue  # Skip invalid numbers

    if not first_digits:
        return {"function": "benford", "error": "No valid numbers"}

    total = len(first_digits)
    counts = Counter(first_digits)

    # Benford's expected distribution (log10 using math since int has no __log10__)
    import math
    benford_expected = {d: round(100 * math.log10(1 + 1/d), 1) for d in range(1, 10)}

    actual = {}
    for d in range(1, 10):
        actual[d] = round(100 * counts.get(d, 0) / total, 1) if total > 0 else 0

    # Check for significant deviations
    warnings = []
    for d in range(1, 10):
        diff = abs(actual[d] - benford_expected[d])
        if diff > 10:
            warnings.append(f"Digit {d}: expected {benford_expected[d]}%, actual {actual[d]}% (diff: {diff}%)")

    return {
        "function": "benford",
        "sample_size": total,
        "expected_pct": benford_expected,
        "actual_pct": actual,
        "warnings": warnings,
        "suspicious": len(warnings) > 2
    }
