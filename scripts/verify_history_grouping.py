#!/usr/bin/env python3
"""批C 验收：注入层「同标的聚合 / 完全重复合并 / 立场冲突标记」

用法: venv/bin/python scripts/verify_history_grouping.py

只加载 app/harness/decision_log.py（纯标准库，可独立加载），全部断言打在
**真实 decision-log.md** 上（只读，脚本收尾校验 sha256 未变）；
需要构造极端场景的用例一律写到临时目录，绝不碰生产日志。
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "_dl_batchc", ROOT / "app" / "harness" / "decision_log.py"
)
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, bool(ok), detail))


LOG = dl.DECISION_LOG_PATH
SHA_BEFORE = hashlib.sha256(LOG.read_bytes()).hexdigest() if LOG.exists() else ""


def body(out: str) -> str:
    """取注入文本的正文部分（时间线起）—— 头部含用户输入写法，不参与跨写法比对。"""
    i = out.find("### 时间线")
    return out[i:] if i >= 0 else out


def timeline_rows(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if re.match(r"^\d+\. ", ln)]


def conclusions(out: str) -> list[str]:
    return re.findall(r"^\s+上次结论: (.*)$", out, re.M)


# ── 1. 同标的聚合：打名字 / 打代码 / 打带后缀代码，正文必须一致 ──────────
GROUPS = [
    ("中际旭创", "300308", "300308.SZ"),
    ("英维克", "002837.SZ", "002837"),
    ("腾讯", "腾讯控股", "0700.HK"),
    ("平安银行", "000001", "000001.SZ"),
    ("金山办公", "688111", "688111.SH"),
]
for grp in GROUPS:
    outs = [dl.load_decisions(x) for x in grp]
    ok = all(o and body(o) == body(outs[0]) for o in outs)
    check(f"1-聚合 {grp[0]}：{' / '.join(grp)} 命中同一组",
          ok,
          f"条数={[len(timeline_rows(o)) for o in outs]} 正文一致={ok}")

# ── 2. 规范代码写入报告头 ────────────────────────────────────────────
out_xc = dl.load_decisions("中际旭创")
check("2-报告头带规范代码", "（规范代码 300308.SZ）" in out_xc,
      out_xc.splitlines()[2] if len(out_xc.splitlines()) > 2 else "空")

# ── 3. 8 月旧判断与 9 月最新判断同时在场（用户验收口径） ─────────────
rows_xc = " ".join(timeline_rows(out_xc))
check("3-跨月历史同时在场（08 与 09）", "2026-08" in rows_xc and "2026-09" in rows_xc,
      f"时间线={timeline_rows(out_xc)[:2]} … 末条={timeline_rows(out_xc)[-1:]}")

# ── 4. 名称 → 规范代码解析 ─────────────────────────────────────────
sections = [s for s in re.split(r"\n(?=## )", LOG.read_text(encoding="utf-8"))
            if s.strip().startswith("## ")]
index = dl._alias_index(sections)
canon_by_name = {n: dl._stock_group_keys(n, index)[0] for n in
                 ["中际旭创", "英维克", "腾讯", "平安银行"]}
check("4-名称解析到规范代码",
      canon_by_name == {"中际旭创": "300308.SZ", "英维克": "002837.SZ",
                        "腾讯": "0700.HK", "平安银行": "000001.SZ"},
      str(canon_by_name))
check("4b-代码归一（去后缀 → 带后缀）",
      dl._stock_group_keys("300308", index)[0] == "300308.SZ"
      and dl._stock_group_keys("002837", index)[0] == "002837.SZ",
      f"300308→{dl._stock_group_keys('300308', index)[0]}")

# ── 5. 重复合并：数量 + 标注 + 名额 ─────────────────────────────────
check("5-头部声明合并重复与腾出名额",
      "同组历史 6 条 → 有效 5 条（合并 1 条完全重复，腾出 1 个注入名额）" in out_xc,
      out_xc.splitlines()[2] if len(out_xc.splitlines()) > 2 else "空")
dup_lines = [ln for ln in out_xc.splitlines() if "条完全重复，已合并" in ln]
check("5b-重复条目标注（含重复条目时间）",
      len(dup_lines) == 1 and "2026-08-07 11:37" in dup_lines[0],
      dup_lines[0] if dup_lines else "未找到合并标注")

# ── 6. 合并后腾出的名额被真实历史占用（不是白丢） ───────────────────
merged_conclusions = conclusions(out_xc)
normed = [dl._norm_conclusion({"conclusion": c}) for c in merged_conclusions]
check("6-注入条目不再含重复结论",
      len(normed) == len(set(normed)) == 5,
      f"条数={len(normed)} 去重后={len(set(normed))}")
check("6b-注入条数受 max_entries 约束且等于有效条数",
      len(timeline_rows(out_xc)) == 5
      and len(timeline_rows(dl.load_decisions("中际旭创", max_entries=2))) == 2,
      f"默认={len(timeline_rows(out_xc))} max_entries=2 时="
      f"{len(timeline_rows(dl.load_decisions('中际旭创', max_entries=2)))}")

# ── 7. 重复判定语义：结论 + 记录价都相同才算重复 ─────────────────────
same = {"conclusion": "持有", "record_price": "10.00"}
diff_price = {"conclusion": "持有", "record_price": "11.00"}
diff_text = {"conclusion": "买入", "record_price": "10.00"}
check("7-重复判定：同结论+同价=重复，价格或结论不同=不重复",
      dl._dup_key(same) == dl._dup_key(dict(same))
      and dl._dup_key(same) != dl._dup_key(diff_price)
      and dl._dup_key(same) != dl._dup_key(diff_text),
      f"same={dl._dup_key(same)[0][:8]}… price_diff_key_相同="
      f"{dl._dup_key(same) == dl._dup_key(diff_price)}")
check("7b-无结论条目不参与合并",
      dl._dup_key({"conclusion": "", "record_price": "10"}) == (),
      "空结论 → 空键")

# ── 8. 冲突标记（英维克：同组并列相反口径） ─────────────────────────
out_ywk = dl.load_decisions("英维克")
y_lines = out_ywk.splitlines()
conf_idx = next((i for i, ln in enumerate(y_lines) if "⚠ 立场冲突" in ln), -1)
check("8-英维克注入含冲突块", conf_idx >= 0, f"行号={conf_idx}")
conf_block = "\n".join(y_lines[conf_idx:conf_idx + 6]) if conf_idx >= 0 else ""
check("8b-冲突块标出最后一版与上一版",
      "最后一版 2026-09-11 05:49" in conf_block
      and "上一版   2026-09-11 05:49" in conf_block
      and "持有/中性" in conf_block and "谨慎/看空" in conf_block,
      conf_block.splitlines()[1:3] if conf_block else "无")
check("8c-冲突块含差异一句话（原文对照 + 口径变化）",
      "差异一句话" in conf_block and "口径由" in conf_block and "变为" in conf_block,
      (conf_block.splitlines()[3][:110] if len(conf_block.splitlines()) > 3 else "无"))
check("8d-冲突块给出以最后一版为准的处置",
      "以最后一版为准" in conf_block, "")

# ── 9. 不误报：无相反口径的标的不出现冲突块 ─────────────────────────
false_pos = [name for name in ["腾讯", "平安银行", "金山办公", "中际旭创", "688981"]
             if "⚠ 立场冲突" in dl.load_decisions(name)]
check("9-无冲突标的不误报", not false_pos, f"误报={false_pos or '无'}")

# ── 10. 报告头「上次立场 + 改口须说明」 ─────────────────────────────
check("10-报告头写上次立场与改口要求",
      "上次立场: 持有/中性（2026-09-11 05:49 UTC 那一版）" in out_ywk
      and "不要沉默改口" in out_ywk,
      [ln for ln in y_lines[:5] if "上次立场" in ln])
check("10b-历史无明确口径时给明确提示",
      "历史各版未给出明确买卖口径" in out_xc
      and "本次请给出明确立场" in out_xc,
      [ln for ln in out_xc.splitlines()[:5] if "未给出明确买卖口径" in ln])

# ── 11. 立场判定（真实条目 + 边界） ─────────────────────────────────
cases = [
    ({"conclusion": "不建议在当前位置新买入并一直持有；已持有者需设置硬性减仓纪律"}, "bear"),
    ({"conclusion": "⚡持有（附条件）"}, "neutral"),
    ({"conclusion": "HOLD（持有/等待）"}, "neutral"),
    ({"conclusion": "不建议买入", "target": "未指定"}, "bear"),
    ({"conclusion": "这是一个典型的“技术驱动型B2B硬件制造”生意"}, "unknown"),
    ({"conclusion": "建议买入，显著低估", "target": "未指定"}, "bull"),
]
wrong = [(c["conclusion"][:16], exp, dl._classify_stance(c))
         for c, exp in cases if dl._classify_stance(c) != exp]
check("11-立场判定（否定口径优先于正向词）", not wrong, f"不符={wrong or '全部符合'}")

# ── 12. 极性相反（bear + bull）标注为「方向相反」（临时日志） ────────
tmp = Path(tempfile.mkdtemp(prefix="batchc-verify-"))
orig_path = dl.DECISION_LOG_PATH
try:
    tmp_log = tmp / "direction.md"
    tmp_log.write_text(
        "# log\n\n"
        "## 测试股 — 2026-01-01 10:00 UTC\n- **代码**: 000001.SZ\n"
        "- **结论**: 建议买入，显著低估\n- **状态**: pending\n\n---\n\n"
        "## 测试股 — 2026-02-01 10:00 UTC\n- **代码**: 000001.SZ\n"
        "- **结论**: 不建议买入，已经高估\n- **状态**: pending\n\n---\n",
        encoding="utf-8",
    )
    dl.DECISION_LOG_PATH = tmp_log
    out_dir = dl.load_decisions("测试股")
    check("12-极性相反标注为方向相反",
          "⚠ 立场冲突" in out_dir and "（方向相反）" in out_dir
          and "口径由「看多」变为「谨慎/看空」" in out_dir,
          [ln for ln in out_dir.splitlines() if "口径由" in ln][:1])
    # 12b. 冲突信息不因注入名额裁剪而丢失
    out_win = dl.load_decisions("测试股", max_entries=1)
    check("12b-名额裁剪后冲突信息仍完整",
          "⚠ 立场冲突" in out_win and len(timeline_rows(out_win)) == 1,
          f"max_entries=1 → 时间线 {len(timeline_rows(out_win))} 条，冲突块保留="
          f"{'⚠ 立场冲突' in out_win}")

    # ── 13. 脏标题不入组（批B 闸门在分组后仍生效） ─────────────────
    dirty_log = tmp / "dirty.md"
    dirty_log.write_text(
        "# log\n\n"
        "## 能不能买300308 — 2026-09-07 02:34 UTC\n- **代码**: 未指定\n"
        "- **结论**: 与中际旭创无关的历史脏条目\n- **状态**: pending\n\n---\n",
        encoding="utf-8",
    )
    dl.DECISION_LOG_PATH = dirty_log
    check("13-脏标题条目不进组（按名称查询）",
          dl.load_decisions("中际旭创") == ""
          and dl._section_matches(dirty_log.read_text(encoding="utf-8"),
                                  ["中际旭创", "300308"]) is False,
          "脏标题 '能不能买300308' 必须不被名称/代码 key 命中")
finally:
    dl.DECISION_LOG_PATH = orig_path

# ── 14. 解析同源：注入层与 list_decisions 字段一致 ──────────────────
sec = next(s for s in sections if s.startswith("## 英维克"))
parsed = dl._parse_section(sec)
from_api = [e for e in dl.list_decisions(max_entries=1000)
            if e.get("task_id") == parsed.get("task_id")
            and e.get("time") == parsed.get("time")]
check("14-注入层与前端解析同源（字段一致）",
      bool(from_api) and all(
          from_api[0].get(k) == parsed.get(k)
          for k in ["stock", "time", "code", "conclusion", "status", "task_id", "review"]),
      f"task_id={parsed.get('task_id')} 一致={bool(from_api)}")

# ── 15. 幂等 + 生产日志只读 ────────────────────────────────────────
check("15-幂等（连续两次注入一致）",
      dl.load_decisions("英维克") == dl.load_decisions("英维克"),
      "两次调用文本逐字节相同")

# ── 16. 回归：未知标的返回空串（tests/test_harness 的保护线） ────────
check("16-未知标的返回空串",
      dl.load_decisions("UnknownCo") == "" and dl.load_decisions("不存在的标的XYZ123") == "",
      "无匹配 → 空串（不注入空壳历史）")

SHA_AFTER = hashlib.sha256(LOG.read_bytes()).hexdigest()
check("17-生产 decision-log.md 只读未改", SHA_BEFORE == SHA_AFTER,
      f"sha256 {SHA_AFTER[:12]}…")

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + detail))
print(f"\n批C 注入层检查：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
