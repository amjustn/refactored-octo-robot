#!/usr/bin/env python3
"""批C 验收：注入层「同标的聚合 / 完全重复合并 / 立场冲突标记」

用法: venv/bin/python scripts/verify_history_grouping.py

设计（2026-09-27 修假红）:
- 行为断言全部打在**仓库内固定夹具** scripts/fixtures/history_decision_fixture.md
  上，可复现、不随生产 decision-log.md 增长而变化。
- 冲突标记不再是写死的 True/False，而是用**源数据自己算**出期望
  （同组有效条目里是否出现 ≥2 种明确立场、且最后一版立场与上一版不同）；
  实际输出必须与期望一致。
- 生产日志只保留一条**冒烟断言**：读取成功 + 头部数字关系自洽
  （历史 − 合并 = 有效），不锁死任何具体条数/时间。
- 纯函数用例（立场判定、重复判定）不依赖日志。
- 收尾仍校验生产 decision-log.md sha256 未变（只读）。
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


def check(name: str, ok: bool, detail) -> None:
    RESULTS.append((name, bool(ok), detail))


def fmt(v) -> str:
    """detail 可能是 str/list/dict —— 统一转成可打印文本（修 TypeError 崩溃）。"""
    return v if isinstance(v, str) else repr(v)


LOG = dl.DECISION_LOG_PATH
FIXTURE = ROOT / "scripts" / "fixtures" / "history_decision_fixture.md"
SHA_BEFORE = hashlib.sha256(LOG.read_bytes()).hexdigest() if LOG.exists() else ""

FIX_TEXT = FIXTURE.read_text(encoding="utf-8")
FIX_SECTIONS = [s for s in re.split(r"\n(?=## )", FIX_TEXT)
                if s.strip().startswith("## ")]


def body(out: str) -> str:
    """取注入文本的正文部分（时间线起）—— 头部含用户输入写法，不参与跨写法比对。"""
    i = out.find("### 时间线")
    return out[i:] if i >= 0 else out


def timeline_rows(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if re.match(r"^\d+\. ", ln)]


def conclusions(out: str) -> list[str]:
    return re.findall(r"^\s+上次结论: (.*)$", out, re.M)


def expected_conflict(query: str, log_text: str) -> bool:
    """用源数据算「该标的同组是否应出现立场冲突块」——不读死期望值。

    复刻 load_decisions 的分组/合并/立场判定口径：
    同组有效条目里 ≥2 种明确立场，且最后一版立场与上一版不同 → 应标冲突。
    """
    sections = [s for s in re.split(r"\n(?=## )", log_text)
                if s.strip().startswith("## ")]
    index = dl._alias_index(sections)
    _code, keys = dl._stock_group_keys(query, index)
    group: list[dict] = []
    for sec in sections:
        if not dl._section_matches(sec, keys):
            continue
        parsed = dl._parse_section(sec)
        if not parsed:
            continue
        parsed["stance"] = dl._classify_stance(parsed)
        group.append(parsed)
    effective: list[dict] = []
    by_dupkey: dict = {}
    for e in group:
        dk = dl._dup_key(e)
        if dk and dk in by_dupkey:
            continue
        if dk:
            by_dupkey[dk] = e
        effective.append(e)
    if not effective:
        return False
    labels = {e["stance"] for e in effective if e["stance"] != "unknown"}
    if len(labels) < 2:
        return False
    last = effective[-1]
    return any(e["stance"] != last["stance"] for e in effective[:-1])


_fix_orig = dl.DECISION_LOG_PATH
dl.DECISION_LOG_PATH = FIXTURE
try:
    # ── 1. 同标的聚合：打名字 / 打代码 / 打带后缀代码，正文必须一致 ──────
    GROUPS = [
        ("冲突股", "000905", "000905.SZ"),
        ("平静股", "000906", "000906.SZ"),
        ("重复股", "000907", "000907.SZ"),
    ]
    for grp in GROUPS:
        outs = [dl.load_decisions(x) for x in grp]
        ok = all(o and body(o) == body(outs[0]) for o in outs)
        check(f"1-聚合 {grp[0]}：{' / '.join(grp)} 命中同一组",
              ok,
              f"条数={[len(timeline_rows(o)) for o in outs]} 正文一致={ok}")

    # ── 2. 规范代码写入报告头 ────────────────────────────────────────
    out_c = dl.load_decisions("冲突股")
    check("2-报告头带规范代码", "（规范代码 000905.SZ）" in out_c,
          out_c.splitlines()[2] if len(out_c.splitlines()) > 2 else "空")

    # ── 3. 跨月历史同时在场（夹具 2026-01 与 2026-02） ───────────────
    out_c_all = dl.load_decisions("冲突股", max_entries=1000)
    rows_all = " ".join(timeline_rows(out_c_all))
    check("3-跨月历史同时在场（01 与 02）",
          "2026-01" in rows_all and "2026-02" in rows_all,
          f"全量含 01+02={'2026-01' in rows_all and '2026-02' in rows_all}"
          f"（{len(timeline_rows(out_c_all))} 条）")

    # ── 4. 名称 → 规范代码解析 ─────────────────────────────────────
    index = dl._alias_index(FIX_SECTIONS)
    canon_by_name = {n: dl._stock_group_keys(n, index)[0] for n in
                     ["冲突股", "平静股", "重复股", "描述股"]}
    check("4-名称解析到规范代码",
          canon_by_name == {"冲突股": "000905.SZ", "平静股": "000906.SZ",
                            "重复股": "000907.SZ", "描述股": "000908.SZ"},
          fmt(canon_by_name))
    check("4b-代码归一（去后缀 → 带后缀）",
          dl._stock_group_keys("000905", index)[0] == "000905.SZ"
          and dl._stock_group_keys("000907", index)[0] == "000907.SZ",
          f"000905→{dl._stock_group_keys('000905', index)[0]}")

    # ── 5. 重复合并：数字自洽（历史 − 合并 = 有效） ───────────────────
    out_dup = dl.load_decisions("重复股")
    _m5 = re.search(
        r"同组历史 (\d+) 条 → 有效 (\d+) 条（合并 (\d+) 条完全重复，腾出 (\d+) 个注入名额）",
        out_dup,
    )
    check("5-头部声明合并重复与腾出名额（历史−合并=有效）",
          bool(_m5)
          and int(_m5.group(1)) - int(_m5.group(3)) == int(_m5.group(2))
          and int(_m5.group(3)) == 1
          and int(_m5.group(4)) == int(_m5.group(3)),
          (_m5.group(0) if _m5 else "未找到头部声明"))
    dup_lines = [ln for ln in dl.load_decisions("重复股", max_entries=1000).splitlines()
                 if "条完全重复，已合并" in ln]
    check("5b-重复条目标注（含重复条目时间）",
          len(dup_lines) == 1 and "2026-01-07 11:30" in dup_lines[0],
          dup_lines[0] if dup_lines else "未找到合并标注")

    # ── 6. 合并后腾出的名额 / max_entries 约束 ───────────────────────
    merged_conclusions = conclusions(out_dup)
    normed = [dl._norm_conclusion({"conclusion": c}) for c in merged_conclusions]
    check("6-注入条目不再含重复结论",
          len(normed) == len(set(normed)) == 1,
          f"条数={len(normed)} 去重后={len(set(normed))}")
    long_blk = dl.load_decisions("长文股")
    check("6b-注入条数受 max_entries 约束",
          len(timeline_rows(long_blk)) == 5
          and len(timeline_rows(dl.load_decisions("长文股", max_entries=2))) == 2,
          f"默认={len(timeline_rows(long_blk))} max_entries=2 时="
          f"{len(timeline_rows(dl.load_decisions('长文股', max_entries=2)))}")

    # ── 7. 重复判定语义：结论 + 记录价都相同才算重复 ─────────────────
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

    # ── 8. 冲突标记（夹具 冲突股：同组并列相反口径） ─────────────────
    y_lines = out_c.splitlines()
    conf_idx = next((i for i, ln in enumerate(y_lines) if "⚠ 立场冲突" in ln), -1)
    check("8-冲突股注入含冲突块", conf_idx >= 0, f"行号={conf_idx}")
    conf_block = "\n".join(y_lines[conf_idx:conf_idx + 6]) if conf_idx >= 0 else ""
    check("8b-冲突块标出最后一版与上一版",
          "最后一版 2026-02-05 10:00" in conf_block
          and "上一版   2026-01-05 10:00" in conf_block
          and "看多" in conf_block and "谨慎/看空" in conf_block,
          conf_block.splitlines()[1:3] if conf_block else "无")
    check("8c-冲突块含差异一句话（原文对照 + 口径变化）",
          "差异一句话" in conf_block and "口径由" in conf_block and "变为" in conf_block,
          (conf_block.splitlines()[3][:110] if len(conf_block.splitlines()) > 3 else "无"))
    check("8d-冲突块给出以最后一版为准的处置",
          "以最后一版为准" in conf_block, "")

    # ── 9. 冲突标记与源数据立场自洽（期望值由源数据算，不写死） ───────
    fix_cases = ["冲突股", "平静股", "重复股", "描述股"]
    fix_mismatch = []
    for name in fix_cases:
        actual = "⚠ 立场冲突" in dl.load_decisions(name)
        exp = expected_conflict(name, FIX_TEXT)
        if actual != exp:
            fix_mismatch.append((name, exp, actual))
    # 夹具应同时覆盖「该冲突」与「不该冲突」两种情形，避免断言空洞
    fix_expects = {n: expected_conflict(n, FIX_TEXT) for n in fix_cases}
    check("9-冲突标记与源数据立场自洽（夹具，期望由源数据算）",
          not fix_mismatch
          and fix_expects["冲突股"] and not fix_expects["平静股"]
          and not fix_expects["重复股"] and not fix_expects["描述股"],
          f"期望={fix_expects} 不符={fix_mismatch or '无'}")

    # ── 10. 报告头「上次立场 + 改口须说明」 ───────────────────────────
    check("10-报告头写上次立场与改口要求",
          "上次立场: 谨慎/看空（2026-02-05 10:00 UTC 那一版）" in out_c
          and "不要沉默改口" in out_c,
          [ln for ln in y_lines[:6] if "上次立场" in ln])
    out_desc = dl.load_decisions("描述股")
    check("10b-历史无明确口径时给明确提示",
          "历史各版未给出明确买卖口径" in out_desc
          and "本次请给出明确立场" in out_desc,
          [ln for ln in out_desc.splitlines()[:6] if "未给出明确买卖口径" in ln])

    # ── 11. 立场判定（真实条目 + 边界） ─────────────────────────────
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

    # ── 12. 极性相反（bear + bull）标注为「方向相反」 ────────────────
    check("12-极性相反标注为方向相反",
          "⚠ 立场冲突" in out_c and "（方向相反）" in out_c
          and "口径由「看多」变为「谨慎/看空」" in out_c,
          [ln for ln in out_c.splitlines() if "口径由" in ln][:1])
    out_win = dl.load_decisions("冲突股", max_entries=1)
    check("12b-名额裁剪后冲突信息仍完整",
          "⚠ 立场冲突" in out_win and len(timeline_rows(out_win)) == 1,
          f"max_entries=1 → 时间线 {len(timeline_rows(out_win))} 条，冲突块保留="
          f"{'⚠ 立场冲突' in out_win}")

    # ── 13. 脏标题不入组（批B 闸门在分组后仍生效，临时日志） ─────────
    tmp = Path(tempfile.mkdtemp(prefix="batchc-verify-"))
    dirty_log = tmp / "dirty.md"
    dirty_log.write_text(
        "# log\n\n"
        "## 能不能买300308 — 2026-09-07 02:34 UTC\n- **代码**: 未指定\n"
        "- **结论**: 与中际旭创无关的历史脏条目\n- **状态**: pending\n\n---\n",
        encoding="utf-8",
    )
    dl.DECISION_LOG_PATH = dirty_log
    try:
        check("13-脏标题条目不进组（按名称查询）",
              dl.load_decisions("中际旭创") == ""
              and dl._section_matches(dirty_log.read_text(encoding="utf-8"),
                                      ["中际旭创", "300308"]) is False,
              "脏标题 '能不能买300308' 必须不被名称/代码 key 命中")
    finally:
        dl.DECISION_LOG_PATH = FIXTURE

    # ── 14. 解析同源：注入层与 list_decisions 字段一致 ───────────────
    sec = next(s for s in FIX_SECTIONS if s.startswith("## 冲突股"))
    parsed = dl._parse_section(sec)
    from_api = [e for e in dl.list_decisions(max_entries=1000)
                if e.get("task_id") == parsed.get("task_id")
                and e.get("time") == parsed.get("time")]
    check("14-注入层与前端解析同源（字段一致）",
          bool(from_api) and all(
              from_api[0].get(k) == parsed.get(k)
              for k in ["stock", "time", "code", "conclusion", "status", "task_id", "review"]),
          f"task_id={parsed.get('task_id')} 一致={bool(from_api)}")

    # ── 15. 幂等（夹具） ─────────────────────────────────────────────
    check("15-幂等（连续两次注入一致）",
          dl.load_decisions("冲突股") == dl.load_decisions("冲突股"),
          "两次调用文本逐字节相同")

    # ── 16. 回归：未知标的返回空串（tests/test_harness 的保护线） ────
    check("16-未知标的返回空串",
          dl.load_decisions("UnknownCo") == "" and dl.load_decisions("不存在的标的XYZ123") == "",
          "无匹配 → 空串（不注入空壳历史）")
finally:
    dl.DECISION_LOG_PATH = _fix_orig

# ── 17. 生产日志冒烟：只读成功 + 头部数字关系自洽（不锁死具体数字） ───
smoke_out = dl.load_decisions("中际旭创")
_m_smoke = re.search(r"同组历史 (\d+) 条 → 有效 (\d+) 条(?:（合并 (\d+) 条完全重复)?",
                     smoke_out)
check("17-生产日志冒烟（读取成功 + 历史−合并=有效，不锁死数字）",
      bool(smoke_out) and bool(_m_smoke)
      and int(_m_smoke.group(1)) - int(_m_smoke.group(3) or 0) == int(_m_smoke.group(2))
      and bool(dl.list_decisions(max_entries=1)),
      (_m_smoke.group(0) if _m_smoke else "未读取到生产历史"))

# ── 18. 生产标的「不误报」：期望由生产源数据自己算，不写死 True/False ──
PROD_TEXT = LOG.read_text(encoding="utf-8")
prod_cases = ["腾讯", "平安银行", "金山办公", "中际旭创", "688981"]
prod_mismatch = []
for name in prod_cases:
    actual = "⚠ 立场冲突" in dl.load_decisions(name)
    exp = expected_conflict(name, PROD_TEXT)
    if actual != exp:
        prod_mismatch.append((name, exp, actual))
check("18-无冲突标的不误报（生产：标记与源数据立场自洽）",
      not prod_mismatch,
      f"不符={prod_mismatch or '无'}")

SHA_AFTER = hashlib.sha256(LOG.read_bytes()).hexdigest()
check("19-生产 decision-log.md 只读未改", SHA_BEFORE == SHA_AFTER,
      f"sha256 {SHA_AFTER[:12]}…")

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + fmt(detail)))
print(f"\n批C 注入层检查：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
