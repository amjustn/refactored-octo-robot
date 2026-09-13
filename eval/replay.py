#!/usr/bin/env python3
"""Eval replay — offline golden-set regression for the harness.

Usage:
    python eval/replay.py --all --stub-llm      # offline, zero real tokens
    python eval/replay.py --skill investment-team --stub-llm
    python eval/replay.py --all                 # real LLM (costs tokens!)

--stub-llm mode:
  * LLM calls (chat_stream / chat_complete / chat_complete_with_tools)
    are replaced with fixed canned responses built from the golden case
    (including the report-summary polish call in app.harness)
  * context collection is replaced with a fixed context string
  * ToolGateway runs in cassette playback mode (never touches network)
  * persistence is redirected to eval/runs/<ts>/ (tasks.db + reports)

Golden-case fields (additive):
  attachments: <single-line text>   # optional; passed to TaskSpec.build as
      spec.attachments (uploaded-reference injection). In stub mode the
      harness records every LLM user message; after the run the case fails
      unless the attachment text reached at least one agent prompt
      (reported as checks.attachments_injected).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).parent))

import miniyaml  # noqa: E402


def _load_cases(skill_filter: str = None) -> list:
    golden = Path(__file__).parent / "golden"
    cases = []
    for f in sorted(golden.glob("*.yaml")):
        case = miniyaml.load(str(f))
        case["_file"] = f.name
        if skill_filter and case.get("skill") != skill_filter:
            continue
        cases.append(case)
    return cases


def _load_cassettes() -> dict:
    cass_dir = Path(__file__).parent / "cassettes"
    playback = {}
    for f in sorted(cass_dir.glob("*.json")):
        try:
            playback.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[warn] bad cassette {f.name}: {e}")
    return playback


def _stub_blob(case: dict, required_sections: list) -> str:
    """Canned LLM output built from the golden case expectations."""
    expect = case.get("expect") or {}
    points = expect.get("data_points") or {}
    sections = list(dict.fromkeys((expect.get("sections") or []) + required_sections))

    cited = "；".join(f"{k} {v}亿元（来源：公司公告 2024年报）" for k, v in points.items()) or "营收 100亿元（来源：公司公告）"
    parts = []
    for s in sections:
        parts.append(f"## {s}\n\n（stub 分析段落）{cited}。截至2026年7月，数据显示基本面稳健，分析师给予正面评价（据2025年中报）。\n")
    parts.append("\n投资有风险，以上内容仅供参考，不构成投资建议。")
    return "\n".join(parts)


RECORD_SKIP_MARKER = ('"error"', '"_placeholder": true')


def _install_recorder(playback_sink: dict):
    """录制模式：包一层 ToolGateway._call_chain，把成功的真实工具结果记入 sink。

    key 规则与 ToolGateway._cache_key 完全一致（工具名+参数 JSON），
    跳过含 error / placeholder 的结果，避免把失败响应录进 cassette。
    """
    from app.harness.tools import ToolGateway, get_gateway

    gateway = get_gateway()
    orig = gateway._call_chain

    async def wrapped(tool, args, depth=0, seen=None):
        result = await orig(tool, args, depth=depth, seen=seen if seen is not None else set())
        if result.ok and depth == 0 and not any(m in result.content for m in RECORD_SKIP_MARKER):
            key = ToolGateway._cache_key(tool, args)
            if key not in playback_sink:
                playback_sink[key] = result.content
        return result

    gateway._call_chain = wrapped
    return gateway, orig


async def _verify_cassettes(playback: dict) -> tuple:
    """离线回放验证：逐 key 走 playback 模式的网关，确认全部命中 cassette
    且不触网（playback 短路一切真实执行）。返回 (hits, misses)。"""
    from app.harness.tools import get_gateway

    gateway = get_gateway()
    prev = gateway.playback
    gateway.playback = playback
    hits = misses = 0
    try:
        for key in sorted(playback):
            tool, _, args_json = key.partition(":")
            try:
                args = json.loads(args_json) if args_json else {}
            except Exception:
                args = {}
            r = await gateway._call_chain(tool, args, depth=0, seen=set())
            if r.ok and r.source == "cassette":
                hits += 1
                print(f"[cassette] HIT {key[:100]}")
            else:
                misses += 1
                print(f"[cassette] MISS {key[:100]}")
    finally:
        gateway.playback = prev
    print(f"[cassette] verify: {hits} hit / {misses} miss (offline, no network)")
    return hits, misses


async def run_case(case: dict, stub_llm: bool, playback: dict, scratch: Path) -> dict:
    from app.harness import TaskSpec
    from app.harness import run as harness_run
    from app.harness.events import get_bus
    from app.skills import get_skill

    skill = get_skill(case["skill"])
    if not skill:
        return {"case": case["_file"], "error": f"unknown skill {case['skill']}"}

    spec = TaskSpec.build(
        skill, case.get("arguments", "") or "", caller="eval", stream=True,
        attachments=case.get("attachments", "") or "",
    )

    restore = {}
    if stub_llm:
        import app.harness as harness_pkg
        from app.harness import context as context_mod
        from app.harness import runner as runner_mod
        from app.harness.guards import _required_sections
        from app.harness.tools import get_gateway

        blob = _stub_blob(case, _required_sections(case["skill"]))

        # Record every LLM-bound user message so attachment injection
        # (spec.attachments -> prompts) can be verified offline.
        sent_messages: list = []

        async def stub_chat_complete(system_prompt, user_message, model=None, temperature=0.7, llm_config=None):
            sent_messages.append(user_message)
            return blob

        async def stub_chat_with_tools(system_prompt, user_message, tools=None, tool_executor=None,
                                       model=None, temperature=0.7, llm_config=None):
            sent_messages.append(user_message)
            return blob

        def stub_chat_stream(system_prompt, user_message, model=None, temperature=0.7, llm_config=None):
            sent_messages.append(user_message)
            async def gen():
                for piece in (blob[: len(blob) // 2], blob[len(blob) // 2:]):
                    yield piece
            return gen()

        async def stub_full_context(arguments="", skill_name="", goal_hints=None):
            return "## 当前时间上下文\n\n**当前日期**：2026年07月31日\n\n## 最新市场指数\n\n- 上证指数: 3,500.12 (🔴+0.45%)\n\n*stub context*"

        async def stub_parse_goal(arguments, llm_config=None):
            return None

        # app.harness does its own module-level `chat_complete` import for the
        # one-line report summary; stub it too so --stub-llm stays fully offline.
        async def stub_summary(system_prompt, user_message, model=None, temperature=0.7, llm_config=None):
            return "stub 一句话结论"

        restore = {
            (runner_mod, "chat_stream"): runner_mod.chat_stream,
            (runner_mod, "chat_complete"): runner_mod.chat_complete,
            (runner_mod, "chat_complete_with_tools"): runner_mod.chat_complete_with_tools,
            (context_mod, "build_full_context"): context_mod.build_full_context,
            (context_mod, "parse_goal"): context_mod.parse_goal,
            (harness_pkg, "chat_complete"): harness_pkg.chat_complete,
        }
        runner_mod.chat_stream = stub_chat_stream
        runner_mod.chat_complete = stub_chat_complete
        runner_mod.chat_complete_with_tools = stub_chat_with_tools
        context_mod.build_full_context = stub_full_context
        context_mod.parse_goal = stub_parse_goal
        harness_pkg.chat_complete = stub_summary
        get_gateway().playback = playback

    bus = get_bus()
    queue = bus.subscribe(spec.task_id)
    events = []
    try:
        started = time.time()
        result = await harness_run(spec, bus=bus)
        while not queue.empty():
            try:
                events.append(queue.get_nowait().type)
            except Exception:
                break

        from scorer import score_report
        scores = await score_report(result.report, case, result.duration_seconds or (time.time() - started),
                                    stub_llm=stub_llm)
        out = {
            "case": case["_file"],
            "skill": case["skill"],
            "task_id": spec.task_id,
            "status": result.status,
            "report_chars": len(result.report),
            "duration_s": round(result.duration_seconds, 1),
            "events": [str(e) for e in events],
            "scores": scores,
        }
        # Attachment-injection check: the attachment text must have reached
        # at least one LLM user message during the run (stub mode only).
        if stub_llm and case.get("attachments"):
            out["checks"] = {
                "attachments_injected": any(case["attachments"] in m for m in sent_messages),
            }
        return out
    finally:
        for (mod, name), orig in restore.items():
            setattr(mod, name, orig)
        if stub_llm:
            from app.harness.tools import get_gateway
            get_gateway().playback = None
        bus.unsubscribe(spec.task_id, queue)


async def main_async(args) -> int:
    cases = _load_cases(skill_filter=args.skill)
    if not cases:
        print("no golden cases matched")
        return 2

    ts = time.strftime("%Y%m%d_%H%M%S")
    scratch = Path(__file__).parent / "runs" / ts
    scratch.mkdir(parents=True, exist_ok=True)

    # Redirect persistence away from production data/
    import app.harness.persist as persist_mod
    from app.core.task_store import TaskStore
    persist_mod._repo = persist_mod.Repository(TaskStore(db_path=scratch / "tasks.db"))
    persist_mod.REPORTS_DIR = scratch / "reports"
    (scratch / "reports").mkdir(exist_ok=True)

    playback = _load_cassettes()
    if args.stub_llm and playback:
        print(f"[replay] verifying {len(playback)} cassette keys (offline playback) ...")
        await _verify_cassettes(playback)

    recorder = None
    record_sink: dict = {}
    if args.record:
        from app.harness.tools import get_gateway  # noqa: F401
        recorder = _install_recorder(record_sink)
        print(f"[replay] recording tool results -> eval/cassettes/{args.record}_{ts}.json")

    results = []
    for case in cases:
        print(f"[replay] {case['_file']} (skill={case['skill']}) ...")
        r = await run_case(case, args.stub_llm, playback, scratch)
        results.append(r)
        if "error" in r:
            print(f"  ERROR: {r['error']}")
        else:
            dims = " ".join(f"{k}={v['score']}" for k, v in r["scores"]["dimensions"].items())
            print(f"  status={r['status']} score={r['scores']['total']} {dims}")
            for check, ok in (r.get("checks") or {}).items():
                print(f"  check {check}: {'PASS' if ok else 'FAIL'}")

    if args.record:
        cass_dir = Path(__file__).parent / "cassettes"
        out_cass = cass_dir / f"{args.record}_{ts}.json"
        out_cass.write_text(json.dumps(record_sink, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[replay] recorded {len(record_sink)} tool responses -> {out_cass}")
        if recorder:
            gw, orig = recorder
            gw._call_chain = orig

    out = scratch / "scores.json"
    out.write_text(json.dumps({
        "ts": ts, "stub_llm": args.stub_llm, "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[replay] scores written: {out}")

    def _case_ok(r: dict) -> bool:
        if "error" in r or r.get("status") != "completed":
            return False
        return all((r.get("checks") or {}).values())

    failed = [r for r in results if not _case_ok(r)]
    if failed:
        print(f"[replay] {len(failed)} case(s) failed (incomplete or failed check)")
        return 1
    print(f"[replay] all {len(results)} case(s) completed")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="run all golden cases")
    ap.add_argument("--skill", help="run only cases for one skill")
    ap.add_argument("--stub-llm", action="store_true", help="offline stub LLM (no tokens spent)")
    ap.add_argument("--record", metavar="NAME", help="record real tool responses into eval/cassettes/NAME_<ts>.json")
    args = ap.parse_args()
    if not args.all and not args.skill:
        ap.error("pass --all or --skill NAME")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
