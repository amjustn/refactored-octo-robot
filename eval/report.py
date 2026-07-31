#!/usr/bin/env python3
"""Eval report — compare two replay runs (quality gate).

Usage:
    python eval/report.py                      # compare latest two runs
    python eval/report.py runA/scores.json runB/scores.json

Gate (design §8.4): total score >= baseline - 5, and the data-accuracy
dimension must not regress.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RUNS = Path(__file__).parent / "runs"


def _latest_two() -> tuple:
    files = sorted(RUNS.glob("*/scores.json"))
    if len(files) < 2:
        return None, None
    return files[-2], files[-1]


def main() -> int:
    if len(sys.argv) == 3:
        fa, fb = Path(sys.argv[1]), Path(sys.argv[2])
    else:
        fa, fb = _latest_two()
        if fa is None:
            print("need two runs under eval/runs/ (or pass two score files)")
            return 2

    a = json.loads(fa.read_text(encoding="utf-8"))
    b = json.loads(fb.read_text(encoding="utf-8"))
    print(f"baseline: {fa}")
    print(f"current : {fb}\n")

    amap = {r["case"]: r for r in a["results"]}
    bmap = {r["case"]: r for r in b["results"]}

    exit_code = 0
    for case, rb in bmap.items():
        ra = amap.get(case)
        if ra is None:
            print(f"{case}: new case, total={rb['scores']['total']}")
            continue
        ta, tb = ra["scores"]["total"], rb["scores"]["total"]
        da = ra["scores"]["dimensions"]["data"]["score"]
        db = rb["scores"]["dimensions"]["data"]["score"]
        status = "OK"
        if tb < ta - 5:
            status = "FAIL (total dropped >5)"
            exit_code = 1
        if db < da:
            status = "FAIL (data accuracy regressed)"
            exit_code = 1
        print(f"{case}: total {ta} -> {tb} (Δ{round(tb - ta, 1)}) | data {da} -> {db} | {status}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
