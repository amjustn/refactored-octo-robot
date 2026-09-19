#!/usr/bin/env python3
"""超时抢救 / 600 秒上限 / 缺 Key 前置拦截 —— 交付验证（2026-09-19）

对应线上故障：task 9d717fe98c67（investment-team 300308，4 个 Agent 全撞 300s 上限，
产出只剩 4 行错误）+ task a031a4ac291e / 3ca21a69f0ac（自定义 base_url 没带 Key → 401）。

两类验证：
  * 自底向上（R1–R6）：配置关系、校验函数行为、用**线上真实 scratchpad** 做抢救实测、
    源码接线断言、pytest 回归、服务端下发检查。
  * 自顶向下（R7）：真发一次 HTTP 研究请求（故意给「自定义 base_url 但没 Key」），
    断言它**快速失败**且错误说清原因 —— 而不是 4 个 Agent 各吃一次 401 再等 5 分钟。

用法：venv/bin/python scripts/verify_timeout_and_keyguard.py [--base http://127.0.0.1:8001]
退出码：有 FAIL → 1。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))          # 复用自清理 helper
from verify_cleanup import VERIFY_ARG_MARK, cleanup_task  # noqa: E402
sys.path.insert(0, str(ROOT))          # 让 app.* 可导入（脚本必须能直接跑）

RESULTS: list[tuple[str, str, str, str]] = []
SALVAGE_TASK = "9d717fe98c67"          # 线上那次全超时的任务，用它做真实抢救实测


def record(cid: str, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((cid, name, "PASS" if ok else "FAIL", detail))


def http_json(url: str, payload: dict | None = None, jwt: str | None = None, timeout: int = 30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    if jwt:
        req.add_header("Authorization", "Bearer " + jwt)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore")), round(time.time() - t0, 2)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body, round(time.time() - t0, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    args = ap.parse_args()

    from app.core import config as C
    from app.core.llm import custom_base_without_key
    from app.harness.runner import AgentRunner, PARTIAL_SALVAGE_MARK

    # ── R1 超时预算：Agent 600s 起，单次请求超时必须更短 ──
    ok = C.AGENT_TIMEOUT >= 600 and 0 < C.LLM_REQUEST_TIMEOUT < C.AGENT_TIMEOUT
    record("R1", "Agent 上限≥600s 且单请求超时更短", ok,
           f"AGENT_TIMEOUT={C.AGENT_TIMEOUT}s LLM_REQUEST_TIMEOUT={C.LLM_REQUEST_TIMEOUT}s")

    # ── R2 缺 Key 校验函数行为 ──
    cases = {
        "服务端网关(无 override)": (None, False),
        "只给 model": ({"model": "deepseek-v4-flash"}, False),
        "服务端 base_url": ({"base_url": C.LLM_BASE_URL + "/"}, False),
        "自定义地址+自带Key": ({"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                          "api_key": "sk-own"}, False),
        "自定义地址+无Key(线上事故)": ({"model": "qwen3.8-max",
                              "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}, True),
        "token-plan 地址+无Key(线上事故)": ({"model": "qwen3.8-max",
                                   "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"}, True),
    }
    rows = []
    ok = True
    for label, (cfg, should_block) in cases.items():
        err = custom_base_without_key(cfg)
        blocked = bool(err)
        ok = ok and blocked == should_block
        rows.append(f"{label}→{'拦下' if blocked else '放行'}")
    record("R2", "缺 Key 校验（只拦自定义地址、不误伤服务端）", ok, "；".join(rows))

    # ── R3 用线上真实 scratchpad 做抢救实测（超时数据能不能捞回来）──
    pad = ROOT / "data" / "scratchpad" / f"{SALVAGE_TASK}.jsonl"
    if pad.exists():
        import types
        fake = types.SimpleNamespace(task_id=SALVAGE_TASK)
        out = AgentRunner._salvage_tool_trace(fake, "industry-researcher", 600)  # type: ignore[arg-type]
        calls = len(re.findall(r"\n\d+\. `", "\n" + out))
        # 只捞该 Agent 自己的调用：别的 Agent 的工具名不得出现
        ok = (out.startswith(PARTIAL_SALVAGE_MARK) and calls > 0
              and "quant_factor_signal" not in out)
        record("R3", "线上超时任务能捞回原始数据", ok,
               f"来源 {pad.name}：生成 {len(out)} 字符 / {calls} 次工具调用，"
               f"含缓存留痕提示={'data/scratchpad/' in out}")
    else:
        record("R3", "线上超时任务能捞回原始数据", False, f"找不到 {pad}（无法实测）")

    # ── R4 源码接线断言（改错文件 / 漏接线的常见坑）──
    ru = (ROOT / "app/harness/runner.py").read_text(encoding="utf-8")
    init = (ROOT / "app/harness/__init__.py").read_text(encoding="utf-8")
    js = (ROOT / "static/js/llm-config.js").read_text(encoding="utf-8")
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    checks = {
        "超时分支调抢救": "_salvage_tool_trace(name, AGENT_TIMEOUT)" in ru,
        "报告区分部分成果": "PARTIAL_SALVAGE_MARK" in ru and "partial_count" in ru,
        "全员超时仍交数据": "超时中断·仅原始数据" in ru,
        "resume 排除部分成果": "_is_reusable_artifact" in init,
        "前端 base_url 兜底": "必须填写它自己的 API Key" in js,
        "前端缓存版本号已换": not re.search(r"llm-config\.js\?v=(20260911|20260917|20260919b)\b", html),
    }
    bad = [k for k, v in checks.items() if not v]
    record("R4", "前后端接线齐备", not bad, f"缺失={bad or '无'}")

    # ── R5 pytest 回归（含新契约测试）──
    r = subprocess.run(["venv/bin/python", "-m", "pytest", "tests/test_timeout_salvage.py", "-q"],
                       cwd=str(ROOT), capture_output=True, text=True)
    tail = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""
    record("R5", "tests/test_timeout_salvage.py 全绿", r.returncode == 0, tail[:120])

    # ── R6 服务端确实下发了新前端（不重启就不会生效）──
    try:
        with urllib.request.urlopen(args.base + "/app", timeout=10) as resp:
            page = resp.read().decode("utf-8", "ignore")
        m = re.search(r"llm-config\.js\?v=([0-9a-z]+)", page)
        ver = m.group(1) if m else "-"
        with urllib.request.urlopen(f"{args.base}/static/js/llm-config.js?v={ver}", timeout=10) as resp:
            served = resp.read().decode("utf-8", "ignore")
        ok = "必须填写它自己的 API Key" in served
        record("R6", "/app 下发的 llm-config.js 含新守卫", ok,
               f"版本={ver} 含兜底文案={ok}（后端改动需重启服务才生效）")
    except Exception as e:  # noqa: BLE001
        record("R6", "/app 下发的 llm-config.js 含新守卫", False, f"请求失败：{e}")

    # ── R7 端到端：缺 Key 的请求必须秒失败并说清原因 ──
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    secret = env.get("BERKSHIRE_API_TOKEN", "")
    if not secret:
        record("R7", "端到端：缺 Key 请求秒失败", False, "未读到 BERKSHIRE_API_TOKEN")
    else:
        st, body, _ = http_json(args.base + "/api/auth/login", {"token": secret})
        jwt = (body or {}).get("access_token", "") if isinstance(body, dict) else ""
        if not jwt:
            record("R7", "端到端：缺 Key 请求秒失败", False, f"登录失败 HTTP {st}")
        else:
            payload = {
                "skill_name": "investment-research",
                "arguments": f"{VERIFY_ARG_MARK}缺 Key 拦截（请勿当真）",
                "llm_config": {"model": "qwen3.8-max",
                               "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
            }
            st, body, dt = http_json(args.base + "/api/research", payload, jwt=jwt, timeout=120)
            err = str((body or {}).get("error", "")) if isinstance(body, dict) else str(body)
            status = (body or {}).get("status") if isinstance(body, dict) else None
            ok = st == 200 and status == "failed" and "缺少 API Key" in err and dt < 20
            tid = (body or {}).get("task_id") if isinstance(body, dict) else None
            cleanup_note = ""
            if tid:
                cl = cleanup_task(tid)
                cleanup_note = (f"；已自动清理 {tid}（报告 {len(cl['reports'])} 份、"                                f"DB 行 {cl['db_rows']}）")
            record("R7", "端到端：缺 Key 请求秒失败（不再吃 401/不耗 5 分钟）", ok,
                   f"HTTP={st} status={status} 用时={dt}s error={err[:60]!r}{cleanup_note}")

    # ── R8 数据源熔断：摘除不是永久生效，冷却后能半开探活恢复 ──
    from app.harness.tools import CircuitBreaker, get_gateway
    cb = CircuitBreaker("verify-probe", fail_threshold=1, recovery_probe_after=0.05)
    cb.record_failure("boom")
    blocked = not cb.allow_call()
    time.sleep(0.08)
    allowed = cb.allow_call()
    half = cb.state == "half_open"
    cb.record_success()
    record("R8", "熔断摘除后能自动半开恢复（不是永久失效）",
           blocked and allowed and half and cb.state == "closed",
           f"失败后拒绝={blocked} 冷却后可探活={allowed} 探活成功→{cb.state}")

    # ── R9 当前熔断实况（哪些数据源此刻被摘除，供排查取数异常）──
    try:
        states = get_gateway().breaker_states()
        open_srcs = [k for k, v in states.items() if v.get("state") == "open"]
        record("R9", "当前熔断实况可查", True, f"{len(states)} 个熔断器，open={open_srcs or '无'}")
    except Exception as e:  # noqa: BLE001
        record("R9", "当前熔断实况可查", False, f"读取失败：{e}")

    width = max(len(r[1]) for r in RESULTS)
    print("=" * 86)
    print("超时抢救 + 缺 Key 前置拦截 — 验证")
    print("=" * 86)
    for cid, name, status, detail in RESULTS:
        print(f"[{status}] {cid} {name.ljust(width)} | {detail}")
    fails = [r for r in RESULTS if r[2] == "FAIL"]
    print("-" * 86)
    print(f"合计 {len(RESULTS)} 项，PASS {len(RESULTS) - len(fails)}，FAIL {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
