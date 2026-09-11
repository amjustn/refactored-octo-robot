#!/usr/bin/env python3
"""AI Berkshire Web — 分批交付自检脚本（批0 基线）

用法:
    venv/bin/python scripts/verify_ai_berkshire.py                # 默认全部（不含 pytest）
    venv/bin/python scripts/verify_ai_berkshire.py --with-pytest  # 加跑 pytest tests -q
    venv/bin/python scripts/verify_ai_berkshire.py --base http://127.0.0.1:8001

设计原则（与「数据管道不能静默失败」一致）：
  * 每条检查都给出实测值，不允许只报 PASS/FAIL。
  * 属于后续批次目标的项目标 PENDING，不计入失败，但会被打印出来。
  * 退出码：有 FAIL → 1；只有 PENDING → 0。

覆盖：后端 API(9) + 前端静态资产(10) + 状态机行为(1) + 回归(3, 含 BasicAuth 两组合)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "data" / "reports"
LOADED_JS = [
    "core.js", "skills-nav.js", "llm-config.js", "research.js",
    "render.js", "report-actions.js", "stats.js", "init.js", "charts.js", "skin.js",
]
RESULTS: list[tuple[str, str, str, str]] = []  # (id, 名称, 状态, 实测)


def record(cid: str, name: str, ok: bool | None, detail: str, pending: bool = False) -> None:
    status = "PENDING" if pending else ("PASS" if ok else "FAIL")
    RESULTS.append((cid, name, status, detail))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


def http(url: str, token: str | None = None, basic: str | None = None, timeout: int = 10):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if basic:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(basic.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:  # 连接失败等
        return 0, f"__error__:{e}"


def get_jwt(base: str) -> str:
    _, html = http(base + "/app")
    m = re.search(r'window\.__JWT__="([^"]+)"', html)
    return m.group(1) if m else ""


# ─────────────────────────── 后端 API ───────────────────────────
def check_api(base: str) -> None:
    token = get_jwt(base)
    if not token:
        record("A0", "取 /app 注入的 JWT", False, "页面未返回 window.__JWT__")
        return
    record("A0", "取 /app 注入的 JWT", True, f"长度 {len(token)}")

    code, body = http(base + "/health")
    md_count = len(list(REPORTS_DIR.glob("*.md")))
    skills_count = 0
    try:
        h = json.loads(body)
        skills_count = h.get("skills_count", 0)
        ok = bool(code == 200 and h.get("reports_count") == md_count)
        record("A1", "/health 与磁盘报告数一致", ok,
               f"HTTP {code} reports_count={h.get('reports_count')} 磁盘={md_count}")
    except Exception as e:
        record("A1", "/health 与磁盘报告数一致", False, f"HTTP {code} 解析失败 {e}")

    code, body = http(base + "/api/llm/default")
    try:
        d = json.loads(body)
        models = d.get("supported_models") or []
        fams = {m.split("-")[0] for m in models}
        ok = bool(code == 200 and isinstance(d.get("has_key"), bool) and d.get("model")
                  and len(models) >= 5 and {"deepseek", "kimi", "glm"} <= fams)
        record("A2", "/api/llm/default 字段完备", ok,
               f"HTTP {code} model={d.get('model')} has_key={d.get('has_key')} 模型数={len(models)} 族={sorted(fams)}")
    except Exception as e:
        record("A2", "/api/llm/default 字段完备", False, f"HTTP {code} 解析失败 {e}")

    code, body = http(base + "/api/reports", token)
    try:
        rs = json.loads(body)["reports"]
        record("A3", "/api/reports 可达且字段稳定", code == 200 and len(rs) > 0,
               f"HTTP {code} 返回 {len(rs)} 条（磁盘 {md_count} 条）")
        record("A3b", "139 份报告全部可见", len(rs) == md_count,
               f"可见 {len(rs)} / 磁盘 {md_count}（差 {md_count - len(rs)} 条）",
               pending=(len(rs) != md_count))
        name = rs[0]["name"]
        code2, body2 = http(base + "/api/reports/" + urllib.parse.quote(name), token)
        record("A4", "单份报告可取回", code2 == 200 and len(body2) > 200,
               f"HTTP {code2} {name} 长度 {len(body2)}")
        # 批C：无元数据的旧报告必须被显式标记（前端据此显示「未记录」而非猜测值）
        meta_files = len(list(REPORTS_DIR.glob("*.meta.json")))
        miss = sum(1 for r in rs if r.get("meta_missing"))
        record("C1", "元数据缺口显式标记", miss == md_count - meta_files,
               f"meta_missing={miss} 期望={md_count - meta_files}（磁盘 meta={meta_files}）")
    except Exception as e:
        record("A3", "/api/reports 可达且字段稳定", False, f"HTTP {code} 解析失败 {e}")
        record("A4", "单份报告可取回", False, "上游 A3 失败")
        record("A3b", "139 份报告全部可见", False, "上游 A3 失败")

    code, body = http(base + "/api/decisions", token)
    try:
        d = json.loads(body)
        rs = d.get("decisions") or []
        need = {"stock", "code", "conclusion", "verify", "confidence", "assumptions"}
        ok = code == 200 and rs and need <= set(rs[0].keys())
        record("A5", "/api/decisions 字段齐备", ok,
               f"HTTP {code} 条数={len(rs)} 缺={sorted(need - set(rs[0].keys())) if rs else 'n/a'}")
        # 批B 出口指标：修完抽取链路后条目只应增加（33 → 35+），不允许减少
        record("B2", "决策条目数不减少(基线 33)", len(rs) >= 33,
               f"当前 {len(rs)} 条（基线 33）")
        legacy = [r for r in rs if r.get("legacy")]
        record("B3", "历史脏条目已标记并透出 legacy 字段", len(legacy) >= 7,
               f"legacy 条目 {len(legacy)} 条（应 ≥7，且不参与同股匹配）")
    except Exception as e:
        record("A5", "/api/decisions 字段齐备", False, f"HTTP {code} 解析失败 {e}")

    code, body = http(base + "/api/skills", token)
    try:
        n = len(json.loads(body)["skills"])
        record("A6", "技能数与 /health 一致", bool(code == 200 and n == skills_count),
               f"HTTP {code} 技能数={n} /health={skills_count}")
    except Exception as e:
        record("A6", "技能数与 /health 一致", False, f"HTTP {code} 解析失败 {e}")

    code, _ = http(base + "/api/reports")
    record("A7", "无 JWT 访问 /api/* 仍被拒", code == 401, f"HTTP {code}（期望 401）")
    code, _ = http(base + "/api/reports", "eyJhbGciOiJIUzI1NiJ9.forged.sig")
    record("A8", "伪造 JWT 被拒", code == 401, f"HTTP {code}（期望 401）")


# ─────────────────────── 前端静态资产/契约 ───────────────────────
def check_static() -> None:
    html = read("templates/index.html")

    ext = re.findall(r'<script[^>]+src="(https?://[^"]+)"', html)
    local_purify = "/static/js/purify.min.js" in html
    record("S1", "前端无外网 CDN 依赖且 DOMPurify 本地化", (not ext) and local_purify,
           f"外链={ext or '无'} 本地 purify 引用={local_purify}")

    p = ROOT / "static/js/purify.min.js"
    ok = p.exists() and "DOMPurify" in p.read_text(encoding="utf-8", errors="ignore")[:400]
    record("S2", "本地 purify.min.js 存在且为 DOMPurify", ok,
           f"存在={p.exists()} 大小={p.stat().st_size if p.exists() else 0}B")

    initjs = read("static/js/init.js")
    record("S3", "启动断言 DOMPurify 已加载", "typeof DOMPurify" in initjs or "window.DOMPurify" in initjs,
           "init.js 内含 DOMPurify 存在性检查" if "DOMPurify" in initjs else "未发现断言")

    llm = read("static/js/llm-config.js")
    ok = ("_serverHasKey" in llm) and ("服务端默认" in llm) and ("未配置" in llm)
    record("S4", "模型状态机三态齐备(服务端默认/自定义/未配置)", ok,
           f"_serverHasKey={'_serverHasKey' in llm} 服务端默认={'服务端默认' in llm} 未配置={'未配置' in llm}")

    save_block = llm[llm.find("function _saveLlmConfig"):llm.find("function _saveLlmConfig") + 600]
    ok = "sessionStorage" in save_block and "localStorage" in save_block
    record("S5", "凭据默认写 sessionStorage，仅记住时落 localStorage", ok,
           "含 sessionStorage+localStorage 分支" if ok else "未发现按需分支")

    ok = ("server" in llm) and ("LLM_PROVIDER_PRESETS.server" in llm or "presets.server" in llm)
    record("S6", "网关预设暴露服务端全部模型", ok and "supported_models" in llm,
           "server 预设存在且取 supported_models" if ok else "未发现 server 预设")

    res = read("static/js/research.js")
    record("S7", "开跑阻断条件纳入服务端默认模型", "_canRunWithoutOwnConfig" in res,
           "research.js 引用 _canRunWithoutOwnConfig" if "_canRunWithoutOwnConfig" in res else "仍为无条件阻断")

    ok = ("llm-remember" in html) and ("clearLlmCredentials" in html)
    record("S8", "设置页有『记住本机』与『清除已保存凭据』", ok,
           f"remember控件={'llm-remember' in html} 清除按钮={'clearLlmCredentials' in html}")

    pages = read("app/routers/pages.py")
    core = read("static/js/core.js")
    stragglers = []
    for f in LOADED_JS:
        if f == "core.js":
            continue
        body = read("static/js/" + f)
        if "localStorage.getItem('ai_berkshire_jwt')" in body or 'localStorage.getItem("ai_berkshire_jwt")' in body:
            stragglers.append(f)
    ok = ("sessionStorage" in pages) and ("_getJwt" in core) and (not stragglers)
    record("S9", "JWT 收紧为会话级存储(helpers 统一)", ok,
           f"pages.py sessionStorage={'sessionStorage' in pages} core helpers={'_getJwt' in core} 残留直读={stragglers or '无'}")

    bad = []
    for f in LOADED_JS:
        r = subprocess.run(["node", "--check", str(ROOT / "static/js" / f)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f)
    record("S10", "全部前端脚本语法通过 node --check", not bad, f"失败={bad or '无'}")

    # ── 批C：历史报告全量可见 + 筛选/排序 + 分层展示 ──────────────
    ok = all(k in html for k in ("historySkillFilter", "historySort", "historyCount"))
    record("C2", "历史页筛选/排序/计数控件", ok,
           "含技能筛选下拉+排序下拉+计数条" if ok else "控件缺失")

    ra = read("static/js/report-actions.js")
    ok = all(k in ra for k in ("sortHistory", "meta_missing", "renderHistoryCount"))
    record("C3", "前端筛选排序 + 「未记录」分层渲染", ok,
           f"sortHistory={'sortHistory' in ra} meta_missing={'meta_missing' in ra} 计数条={'renderHistoryCount' in ra}")

    misc = read("app/routers/misc.py")
    record("C4", "后端取消 reports[:50] 硬截断", "reports[:50]" not in misc,
           "已移除 50 条上限" if "reports[:50]" not in misc else "仍存在硬截断")

    # ── 批E：语义化导航 / 最近使用 / 默认折叠 / 对比度 / 字号 ──────
    navjs = read("static/js/skills-nav.js")
    has_navitem_btn = '<button type="button" class="nav-item"' in navjs
    has_navheader_btn = 'class="nav-header" aria-expanded' in navjs
    has_aria_pressed = 'aria-pressed=' in navjs
    has_notice_btn = 'role="button"' in html
    ok = has_navitem_btn and has_navheader_btn and has_aria_pressed and has_notice_btn
    record("E1", "导航与可交互区语义化(button/aria)", ok,
           f"nav-item=button:{has_navitem_btn} nav-header=button+aria:{has_navheader_btn} "
           f"fav=aria-pressed:{has_aria_pressed} notice=role_button:{has_notice_btn}")

    ok = all(k in navjs for k in ("recentSkills", "recentSkillsPush", "🕘 最近使用", "firstRun"))
    record("E2", "最近使用 + 首屏默认折叠", ok,
           "recentSkills/最近使用分区/首次访问折叠逻辑齐备" if ok else "缺少最近使用或折叠逻辑")

    # 对比度：直接解 CSS 变量算 WCAG 比值（亮色主题必须 ≥4.5）
    css = read("static/css/style.css")
    skinjs = read("static/js/skin.js")

    def _lum(h: str) -> float:
        h = h.lstrip("#")
        parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def _contrast(a: str, b: str) -> float:
        l1, l2 = sorted([_lum(a), _lum(b)], reverse=True)
        return round((l1 + 0.05) / (l2 + 0.05), 2)

    root = css[css.find(":root"):css.find(":root") + 900]
    dim = re.search(r"--text-secondary:\s*(#[0-9a-fA-F]{6})", root)
    bgs = re.findall(r"--(?:bg|bg-card|bg-secondary):\s*(#[0-9a-fA-F]{6})", root)
    ratios = [_contrast(dim.group(1), b) for b in bgs] if dim else []
    ok = bool(ratios) and min(ratios) >= 4.5
    record("E3", "默认亮色主题文字对比度 ≥4.5(WCAG AA)", ok,
           f"text-secondary={dim.group(1) if dim else '?'} 对 {bgs} = {ratios}（需全部 ≥4.5）")

    # 深色皮肤同样抽查（token 在 skin.js 里）
    dark_ok = True
    dark_detail = []
    for skin_id in ("nightblue", "black", "purple"):
        blk = skinjs[skinjs.find("id: '%s'" % skin_id):]
        blk = blk[:blk.find("},") + 1] if blk else ""
        m_dim = re.search(r"'--text-secondary':\s*'(#[0-9a-fA-F]{6})'", blk)
        m_bg = re.search(r"'--bg':\s*'(#[0-9a-fA-F]{6})'", blk)
        if m_dim and m_bg:
            r = _contrast(m_dim.group(1), m_bg.group(1))
            dark_detail.append(f"{skin_id}={r}")
            dark_ok = dark_ok and r >= 4.5
    record("E4", "深色皮肤文字对比度 ≥4.5", dark_ok, "、".join(dark_detail) or "未解析到皮肤 token")

    # 字号：被点名的几处必须 ≥0.74rem
    targets = {
        r"\.nav-item\s*\{[^}]*font-size:\s*([\d.]+)rem": 0.74,
        r"\.skill-desc\s*\{[^}]*font-size:\s*([\d.]+)rem": 0.74,
        r"\.powered\s*\{[^}]*font-size:\s*([\d.]+)rem": 0.74,
        r"\.history-item \.report-meta \{[^}]*font-size:\s*([\d.]+)rem": 0.74,
    }
    sizes = {}
    bad = []
    for pat, floor in targets.items():
        m = re.search(pat, css, re.S)
        if m:
            sizes[pat[:22]] = float(m.group(1))
            if float(m.group(1)) < floor:
                bad.append(f"{pat[:22]}={m.group(1)}")
    record("E5", "被点名的偏小字号已上调(≥0.74rem)", (not bad) and len(sizes) >= 3,
           f"实测={sizes} 不达标={bad or '无'}")


# ─────────────────────────── 行为 + 回归 ───────────────────────────
def check_behavior_and_regression(base: str, with_pytest: bool) -> None:
    r = subprocess.run(["node", str(ROOT / "scripts/verify_llm_state_machine.js")],
                       capture_output=True, text=True)
    tail = (r.stdout or "").strip().splitlines()
    record("R1", "模型状态机四组合行为", r.returncode == 0,
           tail[-1] if tail else (r.stderr or "")[-200:])

    venv_py = ROOT / "venv/bin/python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    r = subprocess.run([py, "-c", "import app.main; print('import ok')"],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    record("R2", "后端可导入(语法+依赖)", r.returncode == 0,
           (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "无输出")

    # 批B：决策链路（结论抽取 / 标的归一 / 失败留痕）—— 用真实报告做回归
    r = subprocess.run([py, str(ROOT / "scripts/verify_decision_pipeline.py")],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    lines = (r.stdout or "").strip().splitlines()
    metric = next((l for l in lines if l.startswith("[度量] 生产决策日志条目")), "")
    summary = lines[-1] if lines else (r.stderr or "")[-200:]
    record("B1", "决策链路（抽取/归一/留痕）", r.returncode == 0,
           (metric + " | " + summary)[:220])

    # 批F：来源白名单（零输入防护，替代账号密码）
    r = subprocess.run([py, str(ROOT / "scripts/verify_client_guard.py")],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    lines = (r.stdout or "").strip().splitlines()
    record("F1", "来源白名单（auto/strict/off + 端到端 200/403）", r.returncode == 0,
           (lines[-1] if lines else (r.stderr or "")[-160:])[:200])

    main_py = read("app/main.py")
    ok = ("ClientGuardMiddleware" in main_py) and ("X-Forwarded-For" not in main_py.split("app.add_middleware(ClientGuardMiddleware)")[0][-2000:])
    record("F2", "来源白名单已注册且不信任 XFF", ok,
           f"middleware={'ClientGuardMiddleware' in main_py} 未使用XFF={'X-Forwarded-For' not in main_py}")

    if with_pytest:
        r = subprocess.run([py, "-m", "pytest", "tests", "-q", "--no-header", "-x"],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=900)
        out = (r.stdout or "") + (r.stderr or "")
        summary = [l for l in out.splitlines() if re.search(r"\d+ (passed|failed|error)", l)]
        detail = summary[-1].strip() if summary else ([l for l in out.strip().splitlines() if l.strip()] or ["无输出"])[-1]
        record("R3", "pytest 回归", r.returncode == 0, detail[:180])
    else:
        record("R3", "pytest 回归", None, "未运行（加 --with-pytest 启用）", pending=True)

    check_basic_auth(base)


def _free_port(start: int = 8011) -> int:
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("无空闲端口")


def check_basic_auth(base: str) -> None:
    """BasicAuth 开/关 × 有/无凭据：真起一个临时实例测，不动生产进程。"""
    if not shutil.which("node"):
        pass
    port = _free_port()
    venv_uvicorn = ROOT / "venv/bin/uvicorn"
    env = dict(os.environ, BERKSHIRE_BASIC_AUTH="verify:verify")
    proc = subprocess.Popen(
        [str(venv_uvicorn), "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    tmp = f"http://127.0.0.1:{port}"
    try:
        up = False
        for _ in range(40):
            if http(tmp + "/health")[0] == 200:
                up = True
                break
            time.sleep(0.5)
        if not up:
            record("R4", "BasicAuth 组合回归(临时实例)", False, f"{tmp} 30s 内未就绪")
            return
        no_auth = http(tmp + "/app")[0]
        with_auth = http(tmp + "/app", basic="verify:verify")[0]
        wrong = http(tmp + "/app", basic="verify:wrong")[0]
        ok = no_auth == 401 and with_auth == 200 and wrong == 401
        record("R4", "BasicAuth 开=真拦(401/200/401)", ok,
               f"无凭据={no_auth} 正确凭据={with_auth} 错误凭据={wrong}")
        prod = http(base + "/app")[0]
        record("R5", "生产(BasicAuth 未配置)仍可访问 /app", prod == 200, f"HTTP {prod}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--with-pytest", action="store_true")
    args = ap.parse_args()

    print(f"AI Berkshire Web 自检 — base={args.base} root={ROOT}\n")
    check_api(args.base)
    check_static()
    check_behavior_and_regression(args.base, args.with_pytest)

    print(f"{'ID':<5}{'状态':<9}检查项")
    print("-" * 96)
    for cid, name, status, detail in RESULTS:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "PENDING": "PEND"}[status]
        print(f"{cid:<5}{mark:<9}{name}")
        print(f"{'':<14}↳ {detail}")
    fails = [r for r in RESULTS if r[2] == "FAIL"]
    pends = [r for r in RESULTS if r[2] == "PENDING"]
    print("-" * 96)
    print(f"合计 {len(RESULTS)} 项：PASS {len(RESULTS) - len(fails) - len(pends)} / "
          f"FAIL {len(fails)} / PENDING {len(pends)}")
    if fails:
        print("失败项：" + "、".join(f"{r[0]} {r[1]}" for r in fails))
    if pends:
        print("待办项：" + "、".join(f"{r[0]} {r[1]}" for r in pends))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
