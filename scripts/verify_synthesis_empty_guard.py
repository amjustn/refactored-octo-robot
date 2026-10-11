#!/usr/bin/env python
"""自检：综合结果为空时的降级兜底（app/harness/runner.py::degrade_empty_synthesis）。

背景：默认模型是混合思考模型，正文可能整段落在 reasoning 字段，
而 chat_stream 只读 content ⇒ 综合结果为空串；此前没有空值判据，
空串被原样拼进报告且任务仍标成功（实测 4 份历史报告，见技能文档）。

三段：
  纯函数层 —— 降级判据本身（空串/空白/None/正常）
  接线层   —— 调用点位置与告警接线（静态，变异时会变红）
  存量层   —— 用同一判据识别历史空正文报告，并确认完整报告不被误判

用法：cd /home/q/ai_berkshire_web && venv/bin/python scripts/verify_synthesis_empty_guard.py

判据：全部 PASS 且退出码 0。断言只按真实关系收紧，不为过检放宽。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def section_pure() -> None:
    from app.harness.runner import EMPTY_SYNTHESIS_TEXT, degrade_empty_synthesis

    print("\n[1] 纯函数层：降级判据")
    text, warn = degrade_empty_synthesis("")
    check("空串 → 降级文案", text == EMPTY_SYNTHESIS_TEXT and warn is not None)
    check("空串 → 告警 gate=synthesis_empty", (warn or {}).get("gate") == "synthesis_empty")

    for label, value in (("纯空白", "   \n\t "), ("None", None), ("多个空行", "\n\n\n")):
        t, w = degrade_empty_synthesis(value)
        check(f"{label} → 同样降级", t == EMPTY_SYNTHESIS_TEXT and (w or {}).get("gate") == "synthesis_empty")

    normal = "## 一、一句话结论\n澜起科技好生意坏价格。"
    t2, w2 = degrade_empty_synthesis(normal)
    check("正常内容 → 原样返回且不降级", t2 == normal and w2 is None)

    check("降级文案以 [错误] 开头（与既有约定一致）", EMPTY_SYNTHESIS_TEXT.startswith("[错误]"))
    check("降级文案指向附录（用户能看懂下一步）", "附录" in EMPTY_SYNTHESIS_TEXT)
    check("降级文案不含占位符残留", "None" not in EMPTY_SYNTHESIS_TEXT and "{" not in EMPTY_SYNTHESIS_TEXT)


def section_wiring() -> None:
    src = (ROOT / "app/harness/runner.py").read_text(encoding="utf-8")

    print("\n[2] 接线层：调用点位置与告警（静态）")
    call = src.find("degrade_empty_synthesis(synthesis)")
    check("run_multi 里真的调用了降级判据", call > 0)
    synth_assign = src.find("synthesis = await asyncio.wait_for(_synthesize()")
    check("调用点在综合结果赋值之后", 0 < synth_assign < call, f"assign@{synth_assign} call@{call}")
    ckpt = src.find('_save_checkpoint("synthesis"')
    check("调用点在检查点保存之前（不把空串/降级文案当检查点）", 0 < call < ckpt, f"call@{call} ckpt@{ckpt}")

    tail = src[call:call + 600]
    check("调用后接 extra_warnings.append", "self.extra_warnings.append" in tail)
    check("调用后接 progress 状态标记", '_progress("team-lead"' in tail)
    check("空综合会打 error 日志（可事后诊断）", 'logger.error("Team Lead synthesis returned empty content' in tail)

    check("gate 名全仓唯一（不与既有 gate 重名）",
          src.count('"gate": "synthesis_empty"') == 1,
          f"出现 {src.count(chr(34) + 'gate' + chr(34) + ': ' + chr(34) + 'synthesis_empty' + chr(34))} 次")

    check("既有超时兜底文案仍在", "[错误] 综合超时" in src)
    check("既有综合失败兜底文案仍在", "[错误] 综合环节失败" in src)
    check("既有重试服务器默认模型仍在", "Retrying synthesis with the server default model" in src)
    check("组装处仍用同一个 synthesis 变量", "{synthesis}" in src)


def _looks_like_missing_body(md_path: Path) -> bool:
    """判据：报告头之后直接就是「附录：各Agent独立研究报告」。"""
    head = md_path.read_text(encoding="utf-8", errors="ignore")[:2500]
    return bool(re.search(r"^## 附录：各Agent独立研究报告", head, re.M))


def section_existing() -> None:
    print("\n[3] 存量层：同一判据对真实报告成立")
    hits = sorted(p.name for p in (ROOT / "data/reports").glob("*investment-team_*.md") if _looks_like_missing_body(p))
    check("能识别历史空正文报告（≥3 份）", len(hits) >= 3, f"命中 {len(hits)} 份：{hits[-3:]}")
    check("2026-10-11 16:09 那份在内", any("20261011_160929" in h for h in hits))
    normals = [p.name for p in (ROOT / "data/reports").glob("*investment-team_*.md")
               if not _looks_like_missing_body(p) and p.stat().st_size > 20000]
    check("完整报告不被误判", any("20261011_145217" in n for n in normals) and len(normals) >= 5,
          f"完整报告 {len(normals)} 份")
    # 「只读」要用字节级对账证明，而不是同义反复的断言
    import hashlib

    reports = sorted((ROOT / "data/reports").glob("*investment-team_*.md"))
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in reports}
    for p in reports:
        _looks_like_missing_body(p)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in reports}
    check("存量扫描前后报告字节未变（只读验证）", before == after and len(before) >= 5, f"{len(before)} 份")


def main() -> int:
    section_pure()
    section_wiring()
    section_existing()
    print(f"\n结果：PASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
