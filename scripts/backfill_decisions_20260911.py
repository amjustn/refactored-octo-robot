#!/usr/bin/env python3
"""批B 回灌：把历史漏掉的报告结论补进决策日志（一次性、幂等）

背景：英维克两篇报告（2026-09-03）结论相反，却因为旧抽取器只认 5 种
"结论标记"而**一条都没进决策日志** —— 于是既没有历史复盘注入，也没有
任何冲突可见性。批B 修好抽取链路后，这里把这两篇补进去（用它们各自的
meta.json 里的 task_id 做幂等键，重复执行不会重复写入）。

用法:
    venv/bin/python scripts/backfill_decisions_20260911.py --dry-run
    venv/bin/python scripts/backfill_decisions_20260911.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data" / "reports"

_spec = importlib.util.spec_from_file_location("_dl", ROOT / "app" / "harness" / "decision_log.py")
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

# 目标：2026-09-03 的英维克投资团队报告（同目标、同日、结论相反的一对）
TARGETS = [
    "investment-team_20260903_215408.md",
    "investment-team_20260903_215412.md",
]


def _price_from_report(text: str) -> float | None:
    """从报告正文取"现价/股价"用于记录价（拿不到就交给验证任务回填）。"""
    m = re.search(r"\*\*现价\*\*[：:]\s*([\d.]+)", text) or re.search(r"股价[^\d]{0,6}([\d.]+)\s*元", text)
    try:
        return float(m.group(1)) if m else None
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log_text = dl.DECISION_LOG_PATH.read_text(encoding="utf-8") if dl.DECISION_LOG_PATH.exists() else ""
    before_titles = re.findall(r"^## (.+?) — ", log_text, re.M)
    existing_tids = set(re.findall(r"\*\*任务ID\*\*: *(\w+)", log_text))
    print(f"回灌前：{len(before_titles)} 条，已知 task_id {len(existing_tids)} 个")

    added = 0
    for name in TARGETS:
        path = REPORTS / name
        if not path.exists():
            print(f"  跳过（文件不存在）: {name}")
            continue
        meta_path = path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        task_id = str(meta.get("task_id") or "")
        arguments = meta.get("arguments") or ""
        if task_id and task_id in existing_tids:
            print(f"  跳过（已入库 task_id={task_id}）: {name}")
            continue

        text = path.read_text(encoding="utf-8")
        code = dl._extract_stock_code(text) or dl._extract_stock_code(arguments)
        price = _price_from_report(text)
        decision = dl._extract_decision(text, arguments)
        print(f"  {name}: 代码={code or '未识别'} 记录价={price or '待回填'} "
              f"命中标记={decision.get('matched') if decision else '无'}")
        if not decision:
            print("    !! 仍抽不到结论，跳过（会写进 decision-skip-log.jsonl）")
        if args.dry_run:
            continue
        ok = dl.save_decision(arguments=arguments, report=text, skill_name="investment-team",
                              task_id=task_id, stock_code=code, record_price=price)
        print(f"    {'已入库' if ok else '未入库'}")
        added += 1 if ok else 0

    if args.dry_run:
        print("\n[dry-run] 未写入任何内容。")
        return 0

    after = dl.DECISION_LOG_PATH.read_text(encoding="utf-8")
    titles = re.findall(r"^## (.+?) — ", after, re.M)
    dirty_before = [t for t in before_titles if not dl._is_credible_name(t)]
    dirty_after = [t for t in titles if not dl._is_credible_name(t)]
    print(f"\n回灌后：{len(titles)} 条（新增 {added}）")
    print(f"标题不可信：{len(dirty_after)} 条（回灌前 {len(dirty_before)} 条）"
          f"—— 存量脏条目属历史遗留，不在本脚本改写范围内")
    if titles:
        print(f"  最新条目（文件末尾）: {titles[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
