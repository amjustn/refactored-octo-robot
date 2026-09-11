#!/usr/bin/env python3
"""决策日志历史脏条目归一（批B 补充：迁移或标记，保证匹配层不被污染）

问题：7 条历史条目的标题是句子残片（'你看一下还'、'能不能买300308'…），
标题是历史匹配/去重/后续冲突检测的 key —— 脏标题会让"同股"永远匹配不上，
也会让无关条目被错误命中。

处理方式（有据可依，不猜、不删）：
  1) 用条目里的 task_id 反查 data/reports/*.meta.json 拿到当时的 arguments，
     从中提取规范标的（代码优先）→ 改写标题为规范标的，
     原标题保留在 `- **原标题(历史)**: ...` 字段里（审计不丢）。
  2) 查不到依据（如宏观/主题类分析）→ 不猜，标 `- **标的识别**: 非标的或主题分析`
     —— 它不会参与同股匹配，但仍留在日志里。
  3) 能从标题/正文恢复代码的，补写 `- **代码**: <code>`，让条目至少可按代码匹配。

用法:
    venv/bin/python scripts/normalize_decision_log.py            # dry-run
    venv/bin/python scripts/normalize_decision_log.py --apply
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data" / "reports"
_spec = importlib.util.spec_from_file_location("_dl", ROOT / "app" / "harness" / "decision_log.py")
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

RE_TITLE = re.compile(r"^## (.+?) — (.+)$")
RE_FIELD = re.compile(r"^- \*\*(.+?)\*\*: *(.*)$", re.M)


def _args_by_task_id() -> dict:
    """task_id → 当时的 arguments（来自报告 meta.json）。"""
    out = {}
    for meta_path in REPORTS.glob("*.meta.json"):
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        tid = str(m.get("task_id") or "")
        if tid and m.get("arguments"):
            out.setdefault(tid, m["arguments"])
    return out


THEME_WORDS = {
    "银行", "房地产", "地产", "公用事业", "保险", "券商", "证券", "白酒", "新能源",
    "半导体", "芯片", "医药", "煤炭", "有色", "钢铁", "化工", "军工", "消费", "科技",
    "光伏", "储能", "家电", "汽车", "机械", "传媒", "计算机", "通信", "宏观", "策略",
}


def _classify(args_text: str, title: str, cur_code: str) -> tuple[str, str, str]:
    """判断该死条目属于哪一类，返回 (新标题, 依据, 供补写的代码)。

    只认有据可依的依据：当时 arguments、条目已有代码、标题里的代码。
    多标的/主题类不去猜具体公司，直接归到"非单一标的"。
    """
    segs = [s.strip() for s in re.split(r"[，,、;；\s]+", args_text or "") if len(s.strip()) >= 2]
    codes = sorted({c for c in (dl._extract_stock_code(s) for s in segs) if c})
    code = dl._extract_stock_code(cur_code) or (codes[0] if len(codes) == 1 else "") \
        or dl._extract_stock_code(title)
    name = dl._extract_stock_name(args_text or "")
    if name and not dl._is_credible_name(name):
        name = ""

    if len(codes) > 1 or len(segs) >= 2 or (name in THEME_WORDS):
        return "", "组合/主题类分析（非单一标的，不参与同股匹配）", code
    if name:
        return name, "task_id → 报告 meta.arguments", code
    if code:
        return code, "由已有代码/arguments/标题恢复", code
    return "", "无代码且名称不可信", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    log_path = dl.DECISION_LOG_PATH
    content = log_path.read_text(encoding="utf-8")
    by_tid = _args_by_task_id()
    parts = re.split(r"(\n(?=## ))", content)

    plan = []
    for i, sec in enumerate(parts):
        if not sec.startswith("## "):
            continue
        head_line, _, body = sec.partition("\n")
        m = RE_TITLE.match(head_line.strip())
        if not m:
            continue
        title, ts = m.group(1).strip(), m.group(2).strip()
        if dl._is_credible_name(title):
            continue
        # 注意：RE_FIELD 已用 re.M 编译，findall 的第二个位置参数是「起始位置」而不是 flags
        # —— 之前误传 re.M 导致从第 8 个字符开始匹配，字段解析不全。
        fields = dict(RE_FIELD.findall(body))
        if "标的识别" in fields:      # 幂等：已处理过
            continue
        tid = (fields.get("任务ID") or "").strip()
        args_text = by_tid.get(tid, "")
        cur_code = (fields.get("代码") or "").strip()
        cur_code = "" if cur_code in ("未指定", "") else cur_code
        new_title, why, code = _classify(args_text, title, cur_code)
        plan.append({
            "idx": i, "old": title, "ts": ts, "task_id": tid,
            "args": args_text, "new": new_title, "why": why, "code": code,
            "sec": sec,
        })

    print(f"待处理历史条目：{len(plan)} 条\n")
    for p in plan:
        target = p["new"] or "（保持原样，标记为非标的）"
        print(f"  原标题='{p['old']}'  ts={p['ts']}")
        print(f"    task_id={p['task_id'] or '(无)'} 当时arguments='{p['args'][:40]}'")
        print(f"    → 新标题={target}   依据={p['why']}  代码={p['code'] or '(无)'}")

    if not args.apply:
        print("\n[dry-run] 未写入。加 --apply 执行。")
        return 0

    bak = ROOT / "_archive" / "bak_20260911_batchA" / f"decision-log.md.pre-normalize"
    shutil.copy2(log_path, bak)
    print(f"\n备份：{bak}")

    changed = 0
    for p in plan:
        sec = p["sec"]
        head_line, _, body = sec.partition("\n")
        notes = []
        if p["new"]:
            new_head = f"## {p['new']} — {p['ts']}"
            insert = (f"\n- **原标题(历史)**: {p['old']}\n"
                      f"- **标的识别**: 已迁移（依据：{p['why']}）\n")
            sec2 = new_head + insert + body
            changed += 1
        else:
            insert = "- **标的识别**: 非标的或主题分析（原标题不可信，不参与同股匹配）\n"
            sec2 = head_line + "\n" + insert + body.lstrip("\n")
            changed += 1
        # 补写代码字段（原来是"未指定"时）
        if p["code"]:
            sec2 = re.sub(r"- \*\*代码\*\*: *未指定", f"- **代码**: {p['code']}", sec2, count=1)
        parts[p["idx"]] = sec2

    new_content = "".join(parts)
    log_path.write_text(new_content, encoding="utf-8")
    print(f"已处理 {changed} 条。")

    # 复核：活跃日志里还有多少"可直接匹配"的脏标题
    left = [t for t in re.findall(r"^## (.+?) — ", new_content, re.M)
            if not dl._is_credible_name(t) and "标的识别" not in new_content]
    titles = re.findall(r"^## (.+?) — ", new_content, re.M)
    dirty_now = [t for t in titles if not dl._is_credible_name(t)]
    marked = len(re.findall(r"\*\*标的识别\*\*", new_content))
    print(f"复核：条目总数 {len(titles)}（应保持不变）；不可信标题 {len(dirty_now)} 条，"
          f"其中已标记 {marked} 条 → 未标记的脏标题 {max(0, len(dirty_now) - marked)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
