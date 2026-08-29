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
                   currency: str = "CNY") -> dict:
    """三情景估值: 乐观/中性/悲观, 返回目标价和上涨空间。

    支持三种估值锚(按公司类型动态选择, 与 financial-analyst 的
    "估值指标行业适配表"对应):
      anchor="PE" — 目标价 = 未来EPS × PE倍数(传统重资产/制造/消费)
      anchor="PB" — 目标价 = 未来每股净资产(BVPS) × PB倍数(银行/保险/周期股)
      anchor="PS" — 目标价 = 未来每股营收 × PS倍数(轻资产/互联网/软件)

    向后兼容: 旧调用(eps + pe_multiples 风格)自动识别为 PE 锚。
    """
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
        return {"function": "three-scenario", "error": f"未知 anchor: {anchor}(可选 PE/PB/PS)"}

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
