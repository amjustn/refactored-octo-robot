#!/usr/bin/env python3
"""批F 验收：客户端来源白名单（零输入防护 —— 不用账号密码）

用法: venv/bin/python scripts/verify_client_guard.py

两层验证：
  1) 纯函数判定表（auto / strict / off 三种模式，各起一个独立进程，因为策略在 import 时读取）
  2) 端到端：临时实例分别用 auto / strict(把 loopback 排除) 起服务，实测 200 与 403
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / "venv/bin/python") if (ROOT / "venv/bin/python").exists() else sys.executable
UVICORN = str(ROOT / "venv/bin/uvicorn")

IPS = ["127.0.0.1", "::1", "172.27.208.1", "192.168.1.5", "10.0.0.7", "169.254.3.4",
       "8.8.8.8", "1.1.1.1", "203.0.113.9", "", "not-an-ip",
       # 双栈监听与 ASGI 测试客户端的两种特殊形态
       "::ffff:127.0.0.1", "::ffff:8.8.8.8", "testclient"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, bool(ok), detail))


def table_for(env_extra: dict) -> dict:
    """在独立进程里 import app.main，拿判定表（策略在 import 时定，必须换进程）。"""
    code = (
        "import json; from app.main import client_allowed, _WSL_GATEWAY, _GUARD_MODE;"
        f"ips={IPS!r};"
        "print(json.dumps({'gateway': _WSL_GATEWAY, 'mode': _GUARD_MODE,"
        " 'results': {i: client_allowed(i) for i in ips}}, ensure_ascii=False))"
    )
    env = dict(os.environ)
    env.pop("BERKSHIRE_CLIENT_ALLOWLIST", None)
    env.pop("BERKSHIRE_CLIENT_GUARD", None)
    env.update(env_extra)
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(f"子进程判定表提取失败: {r.stdout[-300:]} {r.stderr[-300:]}")


# ── 1. auto 模式（默认，不配置任何 env） ────────────────────────────
auto = table_for({})
res = auto["results"]
ok = (res["127.0.0.1"] and res["::1"] and res["172.27.208.1"] and res["192.168.1.5"]
      and res["10.0.0.7"] and res["169.254.3.4"])
check("auto：本机/私网/链路本地放行", ok,
      f"loopback={res['127.0.0.1']} 私网={res['192.168.1.5']} 链路本地={res['169.254.3.4']}")
ok = not (res["8.8.8.8"] or res["1.1.1.1"] or res["203.0.113.9"])
check("auto：公网来源拒绝", ok,
      f"8.8.8.8={res['8.8.8.8']} 1.1.1.1={res['1.1.1.1']} 203.0.113.9={res['203.0.113.9']}")
ok = (not res[""]) and (not res["not-an-ip"])
check("auto：无法解析的来源拒绝", ok, f"空串={res['']} 非法={res['not-an-ip']}")
check("auto：IPv4 映射地址按 IPv4 判定（双栈）",
      res["::ffff:127.0.0.1"] and not res["::ffff:8.8.8.8"],
      f"::ffff:127.0.0.1={res['::ffff:127.0.0.1']} ::ffff:8.8.8.8={res['::ffff:8.8.8.8']}")
check("auto：ASGI 测试客户端放行（不影响 pytest）", res["testclient"], f"testclient={res['testclient']}")

# ── 2. strict 模式（loopback,gateway） ─────────────────────────────
strict = table_for({"BERKSHIRE_CLIENT_ALLOWLIST": "loopback,gateway"})
sres = strict["results"]
gw = strict["gateway"]
check("strict：loopback 放行", sres["127.0.0.1"] and sres["::1"],
      f"127.0.0.1={sres['127.0.0.1']} ::1={sres['::1']}")
check("strict：网关(Windows 宿主)放行", bool(gw) and sres[gw],
      f"检测到网关={gw or '未检测到'} 放行={sres.get(gw) if gw else 'n/a'}")
check("strict：其他私网地址被拒", not (sres["192.168.1.5"] or sres["10.0.0.7"]),
      f"192.168.1.5={sres['192.168.1.5']} 10.0.0.7={sres['10.0.0.7']}")
check("strict：公网被拒", not (sres["8.8.8.8"] or sres["203.0.113.9"]),
      f"8.8.8.8={sres['8.8.8.8']}")

# ── 3. off 模式（逃生舱） ─────────────────────────────────────────
off = table_for({"BERKSHIRE_CLIENT_GUARD": "off"})
ores = off["results"]
check("off：全部放行（逃生舱可用）", all(ores[i] for i in ("127.0.0.1", "8.8.8.8", "203.0.113.9")),
      f"公网={ores['8.8.8.8']} 本机={ores['127.0.0.1']}")


# ── 4. 端到端：临时实例实测 200 / 403 ─────────────────────────────
def _free_port(start: int = 8031) -> int:
    for p in range(start, start + 25):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("无空闲端口")


def _probe(url: str, timeout: int = 5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def live_pair(env_extra: dict) -> int:
    port = _free_port()
    env = dict(os.environ, **env_extra)
    proc = subprocess.Popen(
        [UVICORN, "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    code = 0
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(40):
            c = _probe(base + "/health")
            if c == 200:
                code = 200
                break
            if c == 403:
                code = 403
                break
            time.sleep(0.5)
        if code == 200:  # 就绪后再确认一次，避免启动竞态
            code = _probe(base + "/health")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return code


code_auto = live_pair({})
check("端到端 auto：本机访问正常(200)", code_auto == 200, f"HTTP {code_auto}")
code_strict_local = live_pair({"BERKSHIRE_CLIENT_ALLOWLIST": "loopback,gateway"})
check("端到端 strict(含 loopback)：本机仍 200", code_strict_local == 200, f"HTTP {code_strict_local}")
code_blocked = live_pair({"BERKSHIRE_CLIENT_ALLOWLIST": "8.8.8.8"})
check("端到端 strict(排除本机)：本机被 403", code_blocked == 403, f"HTTP {code_blocked}（期望 403，证明拦截真的生效）")

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + detail))
print(f"\n来源白名单检查：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
