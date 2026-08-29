"""Eval scorer — five-dimension quality scoring.

| dimension   | weight | method                                                  |
|-------------|--------|---------------------------------------------------------|
| structure   | 25     | required-section hit rate (from the golden case)        |
| data        | 30     | numbers near labels vs golden data_points (2% tolerance)|
| citation    | 15     | citation gate rule reused from OutputGuard              |
| duration    | 10     | duration vs case max_duration_s                         |
| llm_judge   | 20     | LLM-as-judge (fixed model, temp 0); stub in --stub-llm  |
"""
from __future__ import annotations

import re
from typing import Optional

WEIGHTS = {
    "structure": 25,
    "data": 30,
    "citation": 15,
    "duration": 10,
    "llm_judge": 20,
}

_SOURCE_MARKERS = re.compile(
    r"(来源|数据来源|据|截至|报表|财报|公告|年报|季报|交易所|官网|"
    r"akshare|yfinance|yahoo|雅虎|腾讯|新浪|东财|东方财富|wind|同花顺|"
    r"20\d{2}[年/-]|Q[1-4])",
    re.I,
)
_NUMBER = re.compile(r"(?:¥|HK\$|\$)?(\d[\d,]*\.?\d*)\s?(?:亿|万|%|元|港元|美元|倍)?")


def _norm_num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def score_structure(report: str, case: dict) -> dict:
    sections = (case.get("expect") or {}).get("sections") or []
    if not sections:
        return {"score": WEIGHTS["structure"], "note": "no sections required"}
    hits = [s for s in sections if s in report]
    ratio = len(hits) / len(sections)
    return {
        "score": round(WEIGHTS["structure"] * ratio, 1),
        "hits": hits,
        "missing": [s for s in sections if s not in report],
    }


def score_data(report: str, case: dict) -> dict:
    points = (case.get("expect") or {}).get("data_points") or {}
    if not points:
        return {"score": WEIGHTS["data"], "note": "no data_points in case (full marks)"}
    results = {}
    hits = 0
    for label, expected in points.items():
        expected = float(expected)
        # 扫描标签所有出现位置 × 窗口内全部数字（而非仅首处首数字）：
        # 报告常在表头/增速语境先出现标签（"2025营收增速"窗口首数字是年份），
        # 多年对比表首列是旧年份金额（"| 营业收入 | 6,090 | ... | 7,518 |"），
        # 真实当期值在同窗口后续列。窗口内任一数字落入 2% 容差即判"报告在
        # 标签附近给出了期望数值"；found 报告命中值，未命中时报告首个候选值。
        candidates = []
        for m in re.finditer(re.escape(label), report):
            window = report[m.start(): m.start() + 80]
            for nm in _NUMBER.finditer(window):
                v = _norm_num(nm.group(1))
                if v is not None:
                    candidates.append(v)
        found = None
        ok = False
        for v in candidates:
            if expected != 0 and abs(v - expected) / abs(expected) <= 0.02:
                found = v
                ok = True
                break
        if found is None and candidates:
            found = candidates[0]
        if ok:
            hits += 1
        results[label] = {"expected": expected, "found": found, "ok": ok,
                          "candidates": candidates[:5]}
    ratio = hits / len(points)
    return {"score": round(WEIGHTS["data"] * ratio, 1), "points": results}


def score_citation(report: str, case: dict) -> dict:
    total = 0
    unsourced = 0
    for m in _NUMBER.finditer(report):
        token = m.group(0)
        if not re.search(r"(亿|万|%|元|港元|美元|倍|,)", token):
            continue
        total += 1
        window = report[max(0, m.start() - 80): m.end() + 80]
        if not _SOURCE_MARKERS.search(window):
            unsourced += 1
    if total == 0:
        return {"score": WEIGHTS["citation"], "note": "no financial numbers found"}
    ratio = 1 - unsourced / total
    return {
        "score": round(WEIGHTS["citation"] * ratio, 1),
        "financial_numbers": total,
        "unsourced": unsourced,
    }


def score_duration(duration_s: float, case: dict) -> dict:
    max_s = (case.get("expect") or {}).get("max_duration_s") or 600
    if duration_s <= max_s:
        return {"score": WEIGHTS["duration"], "duration_s": round(duration_s, 1)}
    ratio = max(0.0, 1 - (duration_s - max_s) / max_s)
    return {"score": round(WEIGHTS["duration"] * ratio, 1), "duration_s": round(duration_s, 1)}


async def score_llm_judge(report: str, case: dict, stub: bool = True) -> dict:
    """LLM-as-judge. In stub mode returns a fixed mid-range score so the
    pipeline is exercised without spending tokens. Real mode: judge with
    temp 0 on rubric_focus; call twice and average (see design §10)."""
    if stub:
        return {"score": 15.0, "note": "stub judge (fixed 15/20); run without --stub-llm for real judging"}

    from app.core.llm import chat_complete
    focus = (case.get("expect") or {}).get("rubric_focus") or []
    rubric = "、".join(focus) if focus else "整体质量"
    prompt = (
        "你是投研报告评审专家。按0-100给以下报告打分，只输出一个整数。\n"
        f"评分重点：{rubric}\n\n报告全文：\n" + report[:20000]
    )
    scores = []
    for _ in range(2):  # judge twice, average (design §10)
        try:
            out = await chat_complete("你是严格的评审。", prompt, temperature=0)
            m = re.search(r"\d+", out or "")
            if m:
                scores.append(min(100, max(0, int(m.group(0)))))
        except Exception:
            pass
    if not scores:
        return {"score": 0.0, "note": "judge failed"}
    avg = sum(scores) / len(scores)
    return {"score": round(WEIGHTS["llm_judge"] * avg / 100, 1), "judge_raw": scores}


async def score_report(report: str, case: dict, duration_s: float, stub_llm: bool = True) -> dict:
    dims = {
        "structure": score_structure(report, case),
        "data": score_data(report, case),
        "citation": score_citation(report, case),
        "duration": score_duration(duration_s, case),
        "llm_judge": await score_llm_judge(report, case, stub=stub_llm),
    }
    total = round(sum(d["score"] for d in dims.values()), 1)
    return {"total": total, "dimensions": dims}
