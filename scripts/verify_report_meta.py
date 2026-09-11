#!/usr/bin/env python3
"""批D 验收：报告 meta 审计字段（模型版本 / 降级 / 数据状态）

用法: venv/bin/python scripts/verify_report_meta.py

用真实 save_report 代码路径写进**临时目录**（monkeypatch persist.REPORTS_DIR），
断言 meta.json 的形状；再对前端/调用点做静态契约检查。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.harness import persist  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, bool(ok), detail))


class _DummyRepo:
    """save_report 不依赖实例状态，用哑对象直接调真实实现。"""


def _save(tmp: Path, **kw) -> dict:
    persist.REPORTS_DIR = tmp
    path = persist.Repository.save_report(_DummyRepo(), **kw)
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    return meta


orig_dir = persist.REPORTS_DIR
try:
    # 1) 正常完成的报告（无降级告警）
    tmp = Path(tempfile.mkdtemp(prefix="meta-ok-"))
    meta = _save(
        tmp, skill_name="investment-team", arguments="英维克", report="# R" * 50,
        duration_seconds=306.2, tokens={"total_tokens": 100, "by_model": {"deepseek-v4-flash": {}}},
        task_id="t1", summary="一句话结论", model="deepseek-v4-flash",
        data_status={"state": "ok", "gates": []},
    )
    check("正常报告写入 model/data_status", meta.get("model") == "deepseek-v4-flash"
          and meta.get("data_status", {}).get("state") == "ok",
          f"model={meta.get('model')} data_status={meta.get('data_status')}")
    check("正常报告不写 guard_warnings", "guard_warnings" not in meta,
          f"guard_warnings={'存在' if 'guard_warnings' in meta else '不存在'}")

    # 2) 带降级告警的报告
    tmp2 = Path(tempfile.mkdtemp(prefix="meta-deg-"))
    meta2 = _save(
        tmp2, skill_name="investment-team", arguments="中际旭创", report="# R" * 50,
        duration_seconds=100.0, tokens={"total_tokens": 50, "by_model": {"deepseek-v4-flash": {}}},
        model="deepseek-v4-flash",
        guard_warnings=[{"gate": "llm_fallback", "detail": "工具调用回退到服务端默认模型"},
                        {"gate": "synthesis_degraded", "detail": "综合环节失败，报告为无综合降级版"}],
        data_status={"state": "degraded", "gates": ["llm_fallback", "synthesis_degraded"]},
    )
    gw = meta2.get("guard_warnings") or []
    check("降级报告写入 guard_warnings", len(gw) == 2 and gw[0].get("gate") == "llm_fallback",
          f"条数={len(gw)} gates={[g.get('gate') for g in gw]}")
    check("降级报告 data_status=degraded 且 gates 齐全",
          meta2.get("data_status", {}).get("state") == "degraded"
          and set(meta2["data_status"].get("gates", [])) == {"llm_fallback", "synthesis_degraded"},
          f"data_status={meta2.get('data_status')}")
    check("告警 detail 单行且截断", all("\n" not in g["detail"] for g in gw),
          "detail 无换行")

    # 3) 未显式传模型时从 tokens.by_model 推断（单模型 / 多模型混用）
    tmp3 = Path(tempfile.mkdtemp(prefix="meta-mix-"))
    meta3 = _save(tmp3, skill_name="x", arguments="a", report="# R" * 50,
                  tokens={"by_model": {"glm-4.7": {}, "deepseek-v4-flash": {}}})
    check("多模型推断为 mixed(...)", str(meta3.get("model", "")).startswith("mixed("),
          f"model={meta3.get('model')}")

    tmp4 = Path(tempfile.mkdtemp(prefix="meta-single-"))
    meta4 = _save(tmp4, skill_name="x", arguments="a", report="# R" * 50,
                  tokens={"by_model": {"kimi-k2.5": {}}})
    check("单模型直接取模型名", meta4.get("model") == "kimi-k2.5", f"model={meta4.get('model')}")
finally:
    persist.REPORTS_DIR = orig_dir

# ── 静态契约：调用点与前端渲染 ─────────────────────────────────────
init_py = (ROOT / "app/harness/__init__.py").read_text(encoding="utf-8")
ok = ("guard_warnings=guard_warnings" in init_py and "model=(spec.llm.model or LLM_MODEL)" in init_py
      and "data_status=" in init_py)
check("harness 调用点传入审计字段", ok,
      "asave_report(..., model=, guard_warnings=, data_status=) 已接入" if ok else "调用点未接线")

misc_py = (ROOT / "app/routers/misc.py").read_text(encoding="utf-8")
ok = all(k in misc_py for k in ("item[\"model\"]", "item[\"degraded\"]", "item[\"data_status\"]"))
check("报告 API 暴露 model/degraded/data_status", ok,
      "三个字段均透出" if ok else "字段缺失")

ra = (ROOT / "static/js/report-actions.js").read_text(encoding="utf-8")
ok = all(k in ra for k in ("warn-badge", "nodata-badge", "未记录（旧报告：无元数据）"))
check("前端渲染降级/未记录徽标与未记录提示", ok,
      "徽标与未记录兜底文案齐备" if ok else "缺少渲染分支")

core = (ROOT / "static/js/core.js").read_text(encoding="utf-8")
check("前端模型提取 helper 存在", "function dominantModel" in core,
      "core.js 内含 dominantModel(tokens)")

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + detail))
print(f"\n报告 meta 审计检查：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
