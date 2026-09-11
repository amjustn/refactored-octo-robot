#!/usr/bin/env python3
"""批B 验收：决策链路补漏（结论抽取 / 标的归一 / 失败留痕）

用法: venv/bin/python scripts/verify_decision_pipeline.py

直接加载 app/harness/decision_log.py（该模块只依赖标准库，所以独立加载，
不触发 app.harness 包的重量级 import），用**真实报告文件**验证抽取效果。
所有会写盘的用例一律 monkeypatch 到临时目录，绝不碰生产 decision-log.md。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── 独立加载 decision_log.py（无相对 import，可单独跑） ──────────────
_spec = importlib.util.spec_from_file_location(
    "_dl_under_test", ROOT / "app" / "harness" / "decision_log.py"
)
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, bool(ok), detail))


REPORTS = ROOT / "data" / "reports"
# 批B 的靶子样本：英维克两篇（历史结论相反、且旧抽取器全部漏掉）
SAMPLES = [
    ("investment-team_20260903_215408.md", "英维克你看一下在半年之内能不能一直持有"),
    ("investment-team_20260903_215412.md", "英维克你看一下在半年之内能不能一直持有"),
    ("investment-team_20260908_214458.md", "300308 中际旭创"),
    ("investment-team_20260828_110248.md", "平安银行"),
]

# ── 1. 结论抽取：真实报告必须抽出结论 ─────────────────────────────
for fname, args in SAMPLES:
    p = REPORTS / fname
    if not p.exists():
        check(f"抽取 {fname}", False, "样本文件不存在")
        continue
    d = dl._extract_decision(p.read_text(encoding="utf-8"), args)
    ok = bool(d and d.get("conclusion"))
    detail = (f"命中={d.get('matched')} 结论={d.get('conclusion', '')[:50]}"
              if d else "未抽出结论（旧版行为）")
    check(f"抽取 {fname.split('_')[1]}_{fname.split('_')[2][:6]}", ok, detail)

# ── 2. 名称可信度闸门：脏 hint 必须被拒 ───────────────────────────
DIRTY = ["你看一下还", "能不能买300308", "工商银行17.5%，长江电力1", "分析一下", "英维克你看一下在半年之内能不能一直持有吗"]
CLEAN = ["英维克", "中际旭创", "300308", "300308.SZ", "平安银行", "腾讯控股", "0700.HK"]
bad = [s for s in DIRTY if dl._is_credible_name(s)]
miss = [s for s in CLEAN if not dl._is_credible_name(s)]
check("脏标的全部被拒", not bad, f"漏放={bad or '无'}")
check("正常标的全部放行", not miss, f"误拒={miss or '无'}")

# ── 3. 标的抽取：口语化提问尾巴被剥离 ─────────────────────────────
cases = {
    "英维克你看一下在半年之内能不能一直持有": "英维克",
    "中际旭创的投资价值": "中际旭创",
    "300308 中际旭创": "300308",
    "帮我看看贵州茅台的基本面": "贵州茅台",
}
wrong = {k: (v, dl._extract_stock_name(k)) for k, v in cases.items() if dl._extract_stock_name(k) != v}
check("标的抽取（含口语尾巴）", not wrong, f"不符={wrong or '全部符合'}")

# ── 4. 多写法 key：代码 + 名称都要认 ──────────────────────────────
keys = dl._stock_match_keys("中际旭创 300308")
check("多写法 key 含代码与名称", ("300308" in keys and "300308.SZ" in keys and "中际旭创" in keys),
      f"keys={keys}")

# ── 5. 条目匹配不误伤：正文提到别的公司不算同股 ───────────────────
entry_paybank = ("## 平安银行 — 2026-09-03 10:00 UTC\n- **代码**: 000001.SZ\n"
                 "- **结论**: 与中际旭创相比估值更低\n")
entry_innolight = "## 中际旭创 — 2026-09-08 10:00 UTC\n- **代码**: 300308.SZ\n"
check("按名称匹配：正文提及不误伤", dl._section_matches(entry_paybank, ["中际旭创"]) is False,
      "平安银行条目在 keys=['中际旭创'] 下必须不匹配")
check("按代码匹配：正文代码字段命中", dl._section_matches(entry_innolight, ["300308.SZ"]) is True,
      "300308.SZ 条目必须被代码 key 命中")
check("代码不带后缀也命中", dl._section_matches(entry_innolight, ["300308"]) is True,
      "300308 必须命中 300308.SZ 条目")

# ── 6/7. 落库行为：临时目录里跑真实报告 + 脏 hint ─────────────────
tmp = Path(tempfile.mkdtemp(prefix="dl-verify-"))
orig_log, orig_skip = dl.DECISION_LOG_PATH, dl.DECISION_SKIP_PATH
try:
    dl.DECISION_LOG_PATH = tmp / "decision-log.md"
    dl.DECISION_SKIP_PATH = tmp / "decision-skip-log.jsonl"

    # 6a. 无结论标记 → 不入库 + 留痕
    ok_saved = dl.save_decision("测试无结论文本", "x" * 200, skill_name="verify", task_id="t1")
    skipped = (tmp / "decision-skip-log.jsonl").exists()
    reason = ""
    if skipped:
        reason = json.loads((tmp / "decision-skip-log.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])["reason"]
    check("抽取失败不再静默（留痕）", (not ok_saved) and skipped and reason == "no_conclusion_marker",
          f"saved={ok_saved} 留痕={skipped} reason={reason}")

    # 6b. 脏 company_hint + 真实英维克报告 → 必须入库且标的干净
    report = (REPORTS / SAMPLES[0][0]).read_text(encoding="utf-8")
    saved = dl.save_decision(SAMPLES[0][1], report, skill_name="investment-team",
                             task_id="t2", company_hint="你看一下还")
    log_text = (tmp / "decision-log.md").read_text(encoding="utf-8") if (tmp / "decision-log.md").exists() else ""
    title = re.search(r"^## (.+?) — ", log_text, re.M)
    title = title.group(1) if title else ""
    check("脏 hint 被拒、标题回退到启发式", saved and title == "英维克",
          f"saved={saved} 标题='{title}'（期望 英维克）")

    # 6c. 入库条目必须带规范代码字段（用户给定代码时）
    dl.save_decision("平安银行 000001 的估值", (REPORTS / SAMPLES[3][0]).read_text(encoding="utf-8"),
                     skill_name="investment-team", task_id="t3")
    log_text = (tmp / "decision-log.md").read_text(encoding="utf-8")
    check("代码字段规范化写入", "- **代码**: 000001.SZ" in log_text,
          "期望出现 '- **代码**: 000001.SZ'")

    # 6d. 同股多写法自动销账（旧写法 300308 / 新写法 中际旭创 应互相覆盖）
    tmp_log = tmp / "dedupe-log.md"
    tmp_log.write_text(
        "# log\n\n## 300308 — 2026-09-01 10:00 UTC\n- **代码**: 300308.SZ\n"
        "- **状态**: pending\n- **验证**: 待验证\n\n---\n", encoding="utf-8")
    dl.DECISION_LOG_PATH = tmp_log
    dl.save_decision("中际旭创 300308", (REPORTS / SAMPLES[2][0]).read_text(encoding="utf-8"),
                     skill_name="investment-team", task_id="t4", stock_code="300308.SZ")
    after = tmp_log.read_text(encoding="utf-8")
    check("跨写法自动销账（旧 300308 ↔ 新 中际旭创）",
          "已被" in after and "自动销账" in after,
          "旧条目标题 300308 应被新条目（中际旭创/300308.SZ）销账")
finally:
    dl.DECISION_LOG_PATH, dl.DECISION_SKIP_PATH = orig_log, orig_skip

# ── 8. 生产日志：历史脏条目的归一与匹配层隔离（硬断言） ───────────
try:
    real = dl.DECISION_LOG_PATH.read_text(encoding="utf-8")
    titles = re.findall(r"^## (.+?) — ", real, re.M)
    marked = len(re.findall(r"\*\*标的识别\*\*", real))
    dirty = [t for t in titles if not dl._is_credible_name(t)]
    unmarked_dirty = max(0, len(dirty) - marked)
    check("生产日志：无「未标记的脏标题」", unmarked_dirty == 0,
          f"条目 {len(titles)} 条 | 不可信标题 {len(dirty)} 条 | 已标记 {marked} 条 | 未标记脏标题 {unmarked_dirty} 条")
    entries = dl.list_decisions(max_entries=1000)
    legacy = [e for e in entries if e.get("legacy")]
    check("生产日志：历史条目已带 legacy 标记并可审计",
          len(legacy) >= 7 and all(e.get("legacy_note") for e in legacy),
          f"legacy 条目 {len(legacy)} 条（示例：{legacy[0].get('stock') if legacy else 'n/a'} / {legacy[0].get('legacy_note','')[:24] if legacy else ''}）")
    migrated = [e for e in entries if "已迁移" in (e.get("legacy_note") or "")]
    check("生产日志：可按代码匹配的条目已迁移到规范标的",
          len(migrated) >= 3 and all(re.match(r"^\d{4,6}(\.|$)", e["stock"]) for e in migrated),
          "迁移条目：" + "、".join(f"{e['stock']}({e.get('legacy_title','')[:8]})" for e in migrated))
except Exception as e:
    check("生产日志：历史脏条目归一", False, f"读取失败: {e}")

# ── 9. 匹配层回归：脏标题不得再被命中 ─────────────────────────────
dirty_entry = ("## 能不能买300308 — 2026-09-07 02:34 UTC\n- **技能**: investment-team\n"
               "- **代码**: 未指定\n- **结论**: 与中际旭创无关的历史脏条目\n")
good_entry = "## 中际旭创 — 2026-09-07 02:34 UTC\n- **代码**: 300308.SZ\n- **结论**: 规范条目\n"
keys = dl._stock_match_keys("中际旭创 300308")
check("匹配层：脏标题(能不能买300308)不再被命中",
      dl._section_matches(dirty_entry, keys) is False,
      f"keys={keys} → dirty={dl._section_matches(dirty_entry, keys)}")
check("匹配层：规范条目仍可按代码/名称命中",
      dl._section_matches(good_entry, keys) is True,
      f"keys={keys} → good={dl._section_matches(good_entry, keys)}")

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + detail))
print(f"\n决策链路检查：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
