#!/usr/bin/env python3
"""扰动探针：改一位重跑，验证报告是否真的跟随工具数据（2026-10-08）。

做什么：
  1. 对同一个问题用**真实数据源**跑两遍：
       pass 1 = 原样返回工具数据；
       pass 2 = 对 get_stock_price 的返回注入**一处**扰动（price += 1234.56）。
  2. 让模型必须调用该工具，并把 price 原值写进报告；比较两份报告里被扰动的
     那个数字有没有跟着变。可能结论：
       跟随    —— 报告数字随扰动而变（数据通路可信）
       未跟随  —— 扰动后报告仍写旧值（报告未真正使用工具数据 → 非零退出）
       无法判定 —— 该数字未被报告引用（模型没调工具 / 没写这个数）
  3. 机器判定 + 退出码：未跟随 → 退出码 1；其它 → 0；脚本自身错误 → 2。

安全与成本：
  - 只在独立临时目录里写报告/账本，绝不写 data/reports/ 正式目录，不污染历史。
  - 运行前打印预计模型调用次数；整轮 token 预算 20000，预计或实际超限即停，
    不重试。

用法:
    venv/bin/python scripts/probe_perturbation.py
    venv/bin/python scripts/probe_perturbation.py --stub   # 无 LLM 时用确定性替身
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKEN_BUDGET = 20000
EST_MODEL_CALLS = 4          # 2 轮 ×（1 次工具轮 + 1 次收尾）
MAX_TOKENS_PER_CALL = 2500   # 保守上限，用于运行前预算判断

PERTURB_TOOL = "get_stock_price"
PERTURB_ARGS = {"symbol": "600519"}
PERTURB_DELTA = 1234.56
_TOL = 0.02                  # 2% 相对容差（模型可能四舍五入）

SYSTEM_PROMPT = (
    "你是数据核对助手。收到查询后，必须调用 get_stock_price 工具获取行情，"
    "拿到结果后只输出一行，格式严格为：\n"
    "数值=<工具返回里 price 字段的原始数字>\n"
    "不要计算、不要推测、不要添加单位、解释或其它任何文字。"
)
USER_PROMPT = "请查询 600519 的股价，并输出其 price 字段的数值。"


def _safe_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract_price(raw: str):
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict):
        for key in ("price", "current_price", "latest_price", "close"):
            val = data.get(key)
            if isinstance(val, (int, float)):
                return float(val)
    return None


def _perturb(raw: str):
    """对工具返回注入一处扰动，返回 (new_raw, original, perturbed)。

    找不到可扰动的数值字段时原样返回（原值/扰动值均为 None）。
    """
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw, None, None
    if isinstance(data, dict):
        for key in ("price", "current_price", "latest_price", "close"):
            val = data.get(key)
            if isinstance(val, (int, float)):
                orig = float(val)
                new = round(orig + PERTURB_DELTA, 2)
                data[key] = new
                return json.dumps(data, ensure_ascii=False, default=str), orig, new
    return raw, None, None


def _reported_number(text: str):
    """从报告里抽出结论数字：优先 `数值=<n>`，否则取第一个数字。"""
    m = re.search(r"数值\s*[=:：]\s*(-?[0-9]+(?:\.[0-9]+)?)", text or "")
    if m:
        return float(m.group(1))
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", text or "")
    return float(m.group(0)) if m else None


def _close(a, b) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs(b) * _TOL, 0.01)


async def _run_pass(runner, perturb: bool, use_stub: bool, index: int, tools):
    """跑一遍：真实/扰动工具数据 → 报告文本 + 观测元数据 + token 用量。"""
    from app.core.llm import chat_complete_with_tools, get_usage, set_usage_context

    inner = runner._tool_executor("probe")
    meta = {"real_calls": 0, "orig": None, "perturbed": None, "injected": False}

    async def executor(name, args):
        raw = await inner(name, args)
        if name == PERTURB_TOOL:
            meta["real_calls"] += 1
            real = _extract_price(raw)
            if perturb:
                new_raw, orig, new = _perturb(raw)
                meta["orig"], meta["perturbed"] = orig, new
                if new is not None:
                    meta["injected"] = True
                    return new_raw
            else:
                meta["orig"] = real
        return raw

    task_id = f"probe-pass{index}"
    set_usage_context(task_id)
    if use_stub:
        return await _stub_report(meta, perturb, executor, task_id)

    try:
        text = await chat_complete_with_tools(
            SYSTEM_PROMPT, USER_PROMPT,
            tools=tools, tool_executor=executor,
        )
    finally:
        usage = get_usage(task_id)
    return text, meta, usage


async def _stub_report(meta, perturb, executor, task_id):
    """无 LLM 时的确定性替身：调用工具并把 price 原样写成报告。

    它验证的是「工具数据 → 报告」这条通路在扰动后是否跟着变；
    模型行为本身不在替身覆盖范围内（结论会显式标注 stub）。
    """
    raw = await executor(PERTURB_TOOL, dict(PERTURB_ARGS))
    price = _extract_price(raw)
    text = f"数值={price}"
    from app.core.llm import get_usage
    return text, meta, get_usage(task_id)


async def _amain(use_stub: bool, tmp: Path):
    import app.harness.tools as tools_mod
    from app.harness import runner as runner_mod
    from app.harness.runner import AgentRunner

    # 工具账本/scratchpad 一律落临时目录，不污染 data/
    runner_mod.TOOL_LEDGER_DIR = tmp / "ledger"
    runner_mod.SCRATCHPAD_DIR = tmp / "scratchpad"
    # 只挂载被扰动的那一个工具 schema：全套 22 个 schema 每轮都要几万 token
    # （实测 4 轮 ~1.97 万，逼近预算）；探针只需要一个工具就够。
    tools = [
        s for s in tools_mod.ALL_TOOL_SCHEMAS
        if (s.get("function") or {}).get("name") == PERTURB_TOOL
    ] or tools_mod.ALL_TOOL_SCHEMAS

    print(f"[probe] 预计模型调用次数：{EST_MODEL_CALLS} 次"
          f"（2 轮 × 2；stub 模式为 0）")
    projected = EST_MODEL_CALLS * MAX_TOKENS_PER_CALL
    print(f"[probe] token 预算 {TOKEN_BUDGET}，最坏估计 {projected}")
    if not use_stub and projected > TOKEN_BUDGET:
        print("[probe] 预计超限，停止（不重试）")
        return 2

    total_tokens = 0
    results = []
    for i, perturb in enumerate((False, True), start=1):
        runner = AgentRunner(task_id=f"probe-pass{i}")
        try:
            text, meta, usage = await _run_pass(runner, perturb, use_stub, i, tools)
        except Exception as e:  # noqa: BLE001
            print(f"[probe] pass{i} 执行失败：{e}")
            return 2
        total_tokens += int(usage.get("total_tokens") or 0)
        results.append({"text": text, "meta": meta})

    if total_tokens > TOKEN_BUDGET:
        print(f"[probe] 实际用量 {total_tokens} 超出预算 {TOKEN_BUDGET}，停止（不重试）")
        # 成本超限单独报错（退出码 2），不冒充数据判定
        return 2

    r1, r2 = results[0], results[1]
    pass1_num = _reported_number(r1["text"])
    pass2_num = _reported_number(r2["text"])
    real_price = r1["meta"].get("orig")
    perturbed_price = r2["meta"].get("perturbed")

    print("\n" + "=" * 60)
    print("扰动探针结果")
    print("=" * 60)
    print(f"工具           : {PERTURB_TOOL}{PERTURB_ARGS}")
    print(f"pass1 工具原值 : {real_price}")
    print(f"pass2 扰动值   : {perturbed_price}（原值 + {PERTURB_DELTA}）")
    print(f"pass1 报告数字 : {pass1_num}")
    print(f"pass2 报告数字 : {pass2_num}")
    print(f"token 用量     : {total_tokens} / {TOKEN_BUDGET}")
    print("-" * 60)
    print(f"pass1 报告片段 : {(r1['text'] or '').strip()[:200]}")
    print(f"pass2 报告片段 : {(r2['text'] or '').strip()[:200]}")
    print("-" * 60)

    if use_stub:
        print("（stub 模式：报告由确定性替身生成，验证工具数据→报告通路）")

    # 注入失败 / 工具没被调用 → 无法判定
    if not r2["meta"].get("injected") or perturbed_price is None:
        verdict = "无法判定（该数字未被报告引用）"
        code = 0
    elif real_price is None:
        verdict = "无法判定（该数字未被报告引用）"
        code = 0
    elif pass1_num is None or not _close(pass1_num, real_price):
        verdict = "无法判定（该数字未被报告引用）"
        code = 0
    elif _close(pass2_num, perturbed_price):
        verdict = "跟随"
        code = 0
    elif _close(pass2_num, real_price):
        verdict = "未跟随"
        code = 1
    else:
        verdict = "无法判定（该数字未被报告引用）"
        code = 0

    print(f"判定：{verdict}")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="扰动探针：改一位重跑，验证报告是否跟随工具数据")
    ap.add_argument("--stub", action="store_true",
                    help="无 LLM 时用确定性替身生成报告（仍走真实工具数据）")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="probe-perturb-") as td:
        tmp = Path(td)
        print(f"[probe] 临时目录：{tmp}（报告/账本均写在此，不碰 data/reports/）")
        return asyncio.run(_amain(args.stub, tmp))


if __name__ == "__main__":
    sys.exit(main())
