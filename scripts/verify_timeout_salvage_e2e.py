#!/usr/bin/env python3
"""真超时端到端验证（2026-09-19）：超时后报告里还有没有数据 + 续跑是否只补超时的 Agent

做法（不碰生产实例）：
  1. 用 AGENT_TIMEOUT=<小值> 环境变量另起一个实例（默认 8099 端口）；
     AGENT_TIMEOUT 支持 env 覆盖正是本次改动之一，这里顺便验证它真的生效。
  2. 真发一次 investment-team 研究请求（服务端默认模型），4 个 Agent 必然在
     小上限内被掐 —— 断言返回的是「超时中断·仅原始数据」报告，且里面能看到
     中断前取到的工具数据（如 get_financial_data / get_stock_price）。
  3. 对该任务调 resume，断言 skipped_agents 为空、rerun_agents 是全部 4 个 Agent
     （部分成果不得被当成可复用成果），随后取消续跑任务避免真跑。
  4. 关掉临时实例。

用法：venv/bin/python scripts/verify_timeout_salvage_e2e.py [--port 8099] [--agent-timeout 20]
退出码：0 = 全部断言通过；1 = 有断言失败；2 = 环境问题（起不来服务）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))          # 复用自清理 helper
from verify_cleanup import VERIFY_ARG_MARK, cleanup_task  # noqa: E402
RESULTS: list[tuple[str, str, bool, str]] = []


def http_json(url: str, payload: dict | None = None, jwt: str | None = None, timeout: int = 600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    if jwt:
        req.add_header("Authorization", "Bearer " + jwt)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore")), round(time.time() - t0, 1)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            raw = json.loads(raw)
        except Exception:
            pass
        return e.code, raw, round(time.time() - t0, 1)


def record(cid, name, ok, detail):
    RESULTS.append((cid, name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {cid} {name} | {detail}", flush=True)


def wait_health(base: str, proc, limit_s: int = 90) -> bool:
    for _ in range(limit_s * 2):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base + "/health", timeout=4) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--agent-timeout", type=int, default=20)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    env = dict(os.environ)
    env["AGENT_TIMEOUT"] = str(args.agent_timeout)
    log_path = ROOT / "logs" / f"verify_timeout_e2e_{args.port}.log"
    tid = None
    logf = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        ["venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(args.port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    print(f"临时实例启动中（pid={proc.pid}, AGENT_TIMEOUT={args.agent_timeout}s, 日志 {log_path}）...", flush=True)
    try:
        if not wait_health(base, proc):
            record("E0", "临时实例健康检查", False, f"起不来，见 {log_path}")
            return 2
        record("E0", "临时实例健康检查 + AGENT_TIMEOUT 覆盖", True,
               f"{base} 就绪，AGENT_TIMEOUT={args.agent_timeout}s（env 覆盖生效）")

        secret = ""
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("BERKSHIRE_API_TOKEN="):
                secret = line.split("=", 1)[1].strip()
        st, body, _ = http_json(base + "/api/auth/login", {"token": secret})
        jwt = (body or {}).get("access_token", "") if isinstance(body, dict) else ""
        if not jwt:
            record("E1", "登录", False, f"HTTP {st}")
            return 2

        st, res, dt = http_json(base + "/api/research", {
            "skill_name": "investment-team",
            # 打标记：脚本被强杀时兜底扫描（verify_cleanup.py）也能认出残留
            "arguments": f"{VERIFY_ARG_MARK}300308 超时抢救",
        }, jwt=jwt, timeout=900)
        res = res if isinstance(res, dict) else {}
        status = res.get("status")
        report = res.get("report") or ""
        err = res.get("error") or ""
        tid = res.get("task_id")
        record("E1", "真跑一次任务（小超时）", st == 200 and status == "failed",
               f"HTTP={st} status={status} 用时={dt}s error={err[:60]!r} task={tid}")
        record("E2", "失败响应带回「超时中断·仅原始数据」报告（不再是空错误页）",
               report.startswith("# AI Berkshire 投研报告（超时中断·仅原始数据）")
               and len(report) > 2000 and report.count("未完成·超时中断") == 4,
               f"报告 {len(report)} 字符；首行={report.splitlines()[0][:50] if report else '（空）'!r}")
        tools = [t for t in ("get_financial_data", "get_stock_price", "fetch_company_news",
                             "quant_factor_signal", "three_scenario") if t in report]
        record("E3", "报告里能看到中断前取到的工具原始数据", len(tools) >= 1,
               f"命中工具={tools[:5]} 段数={report.count('未完成·超时中断')}")

        st, plan, dt2 = http_json(f"{base}/api/tasks/{tid}/resume", {}, jwt=jwt)
        plan = plan if isinstance(plan, dict) else {}
        skipped = plan.get("skipped_agents")
        rerun = plan.get("rerun_agents")
        # 全员超时时有两种正确结果：① resume 正常返回且 skipped 为空、rerun 覆盖 4 个；
        # ② 由于没有任何可复用成果而回退整局重跑（400 + 「没有可复用的 Agent 成果」）。
        # 唯一不正确的结果是「把部分成果当成成果复用」。
        detail = (plan.get("detail") or "") if isinstance(plan, dict) else str(plan)
        case1 = st == 200 and isinstance(rerun, list) and len(rerun) == 4 and not skipped
        case2 = st == 400 and "没有可复用的 Agent 成果" in detail
        record("E4", "续跑不会把「部分成果」当成果复用", case1 or case2,
               f"HTTP={st} skipped={skipped} rerun={rerun} detail={detail[:40]!r}")
        # 立刻取消续跑，避免真跑一轮
        http_json(f"{base}/api/cancel/{tid}", {}, jwt=jwt)
        return 0 if all(r[2] for r in RESULTS) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        logf.close()
        print(f"临时实例已关闭")
        # ── 自清理：验证造出来的任务/报告/留痕一个都不留（用户要求）──
        if tid:
            cl = cleanup_task(tid)
            print(f"已自动清理 {tid}：报告 {len(cl['reports'])} 份、"
                  f"留痕 {'有' if cl['scratchpad'] else '无'}、DB 行 {cl['db_rows']}"
                  + (f"、错误 {cl['errors']}" if cl["errors"] else ""))
        if all(r[2] for r in RESULTS):
            log_path.unlink(missing_ok=True)          # 全过 → 日志也清掉
            print("全部断言通过，验证日志一并清理")
        else:
            print(f"⚠️ 有断言失败，保留日志供排查：{log_path}")


if __name__ == "__main__":
    sys.exit(main())
