#!/usr/bin/env python3
"""验证脚本的自清理工具 —— 用户要求「验证垃圾以后要自动清」（2026-09-19）

端到端验证会真的建任务、写报告、留调用留痕，甚至可能写决策日志。这些东西不该
出现在「历史报告 / 历史决策」里。规矩：**谁造谁清，脚本自己在 finally 里清掉**，
本模块提供统一的清理实现 + 兜底扫描。

用法：
    venv/bin/python scripts/verify_cleanup.py --scan            # 只列不删（兜底排查残留）
    venv/bin/python scripts/verify_cleanup.py                   # 清理带「验证用例」标记的任务
    venv/bin/python scripts/verify_cleanup.py --task-id 1497117f3ad2

被其它脚本 import 时用 `cleanup_task(task_id)`。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCHPAD_DIR = ROOT / "data" / "scratchpad"
TASKS_DB = ROOT / "data" / "tasks.db"
DECISION_LOG = ROOT / "data" / "decision-log.md"
REPORTS_DIRS = [ROOT / "data" / "reports"]

# 验证脚本必须把 arguments 带上这个标记，兜底扫描才找得到残留
VERIFY_ARG_MARK = "【验证用例】"


def _report_dirs() -> list[Path]:
    dirs = [d for d in REPORTS_DIRS if d.exists()]
    return dirs


def _reports_for_task(task_id: str) -> list[Path]:
    """按 meta.json 里的 task_id 找回该任务落盘的报告（正文 + 元数据）。"""
    hits: list[Path] = []
    for d in _report_dirs():
        for meta in d.rglob("*.meta.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("task_id") or "") != task_id:
                continue
            hits.append(meta)
            # 落盘命名有两代：`<base>.md.meta.json` 与 `<base>.meta.json`
            # （正文分别是 <base>.md / <base>.html），两代都要认。
            stem = meta.name[: -len(".meta.json")]
            for cand in (meta.parent / stem, meta.parent / (stem + ".md"),
                         meta.parent / (stem + ".html")):
                if cand.exists() and cand.is_file():
                    hits.append(cand)
    return hits


def _decision_blocks_for_task(task_id: str) -> list[str]:
    """决策日志里的块以 `## 标题` 分隔，块内含 `- **任务ID**: <短id>`。"""
    if not DECISION_LOG.exists():
        return []
    text = DECISION_LOG.read_text(encoding="utf-8")
    blocks = text.split("\n## ")
    short = task_id[:8]
    return [b for b in blocks if f"任务ID**: {short}" in b]


def cleanup_task(task_id: str, *, dry_run: bool = False) -> dict:
    """删掉一次验证任务留下的全部痕迹。返回统计（不抛异常，清理失败只记日志）。"""
    result = {"task_id": task_id, "reports": [], "scratchpad": False,
              "db_rows": 0, "decision_blocks": 0, "errors": []}

    for p in _reports_for_task(task_id):
        try:
            result["reports"].append(p.name)
            if not dry_run:
                p.unlink()
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"{p.name}: {e}")

    pad = SCRATCHPAD_DIR / f"{task_id}.jsonl"
    if pad.exists():
        result["scratchpad"] = True
        if not dry_run:
            try:
                pad.unlink()
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"{pad.name}: {e}")

    if TASKS_DB.exists():
        # ⚠️ dry_run 必须只读：上一版漏了这层保护，--scan 会把 DB 行真删掉。
        try:
            con = sqlite3.connect(str(TASKS_DB), timeout=10)
            try:
                with con:
                    for table in ("task_artifacts", "task_checkpoints", "tasks"):
                        if dry_run:
                            n = con.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", (task_id,)
                            ).fetchone()[0]
                            result["db_rows"] += int(n or 0)
                        else:
                            cur = con.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
                            result["db_rows"] += cur.rowcount or 0
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"db: {e}")

    blocks = _decision_blocks_for_task(task_id)
    if blocks:
        result["decision_blocks"] = len(blocks)
        if not dry_run:
            try:
                text = DECISION_LOG.read_text(encoding="utf-8")
                for b in blocks:
                    text = text.replace("\n## " + b, "")
                DECISION_LOG.write_text(text, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"decision-log: {e}")

    return result


def find_marked_tasks() -> list[tuple[str, str]]:
    """兜底：DB 里 arguments 带验证标记的任务（脚本被强杀时的残留）。"""
    if not TASKS_DB.exists():
        return []
    con = sqlite3.connect(str(TASKS_DB), timeout=10)
    try:
        rows = con.execute(
            "SELECT task_id, arguments FROM tasks WHERE arguments LIKE ?",
            (f"%{VERIFY_ARG_MARK}%",),
        ).fetchall()
    finally:
        con.close()
    return [(r[0], r[1]) for r in rows]


def _fmt(r: dict) -> str:
    bits = [f"报告 {len(r['reports'])} 份", f"留痕 {'有' if r['scratchpad'] else '无'}",
            f"DB 行 {r['db_rows']}", f"决策块 {r['decision_blocks']}"]
    if r["errors"]:
        bits.append("错误 " + "; ".join(r["errors"])[:120])
    return f"{r['task_id']}: " + "，".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只列不删")
    ap.add_argument("--task-id", action="append", default=[], help="指定 task_id（可多次）")
    args = ap.parse_args()

    targets = list(args.task_id)
    marked = find_marked_tasks()
    if not targets:
        targets = [tid for tid, _ in marked]

    if not targets:
        print(f"没有带「{VERIFY_ARG_MARK}」标记的验证任务；如需按 id 清理请用 --task-id")
        return 0

    if marked and not args.task_id:
        print(f"扫到 {len(marked)} 个带标记的验证任务：")
        for tid, arg in marked:
            print(f"  {tid}  arguments={arg[:40]!r}")
    print(f"\n{'【只扫描，不删除】' if args.scan else '【执行清理】'}")
    bad = 0
    for tid in targets:
        r = cleanup_task(tid, dry_run=args.scan)
        print("  " + _fmt(r))
        bad += len(r["errors"])
    print(f"\n处理 {len(targets)} 个任务，错误 {bad} 条")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
