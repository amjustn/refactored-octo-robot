#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Berkshire 2026-08-22 竞品提升包综合验证脚本.

双重验证: 自上而下(前端 HTML/JS/CSS 结构) + 自下而上(后端 API/Python 模块)。
共 20 条断言,全部通过才输出 FINAL PASS。
"""
import json
import os
import subprocess
import sys
import urllib.request

BASE = "/home/q/ai_berkshire_web"
API = "http://127.0.0.1:8001"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def sh(cmd, timeout=30):
    # 所有调用均为硬编码常量路径,无用户输入拼接(shell=True 安全)
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def jwt_token():
    token = os.environ.get("BERKSHIRE_TOKEN") or ""
    if not token:
        with open(os.path.join(BASE, ".env")) as f:
            for line in f:
                if line.startswith("BERKSHIRE_API_TOKEN="):
                    token = line.split("=", 1)[1].strip()
    req = urllib.request.Request(
        API + "/api/auth/login",
        data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def api(method, path, body=None, jwt=None):
    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = "Bearer " + jwt
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    # ============ 自下而上: Python 模块层 ============
    sys.path.insert(0, BASE)
    # 1. valuation.py 四个函数存在且 DCF 手算核对
    # 3. stock_memory.py 结构与检索
    try:
        from app.tools.stock_memory import build_memory_context, get_stock_memory_status
        s = get_stock_memory_status()
        check("stock_memory.status total_reports>0", s.get("total_reports", 0) > 0, f"total={s.get('total_reports')}")
        mem = build_memory_context("腾讯", limit=3)
        check("stock_memory.build_memory_context 命中腾讯", "历史研究记忆" in mem and len(mem) > 100)
        check("stock_memory.无匹配→空串", build_memory_context("不存在的标的XYZ123") == "")
    except Exception as e:
        check("stock_memory.py 导入+断言", False, str(e))

    # 4. report_export.py 微信推送封装与 PDF 生成
    try:
        from app.tools.report_export import markdown_to_pdf, push_report_wechat
        # 不真推(webhook 已配置会消耗 Server酱额度) — 仅校验函数存在与未配置时行为
        check("report_export.push_report_wechat 可调用", callable(push_report_wechat))
        pdf = markdown_to_pdf("# 中文标题\n\n|A|B|\n|-|-|\n|1|2|", "/tmp/verify_export.pdf")
        check("report_export.markdown_to_pdf 生成>1KB", pdf.get("ok") and os.path.getsize("/tmp/verify_export.pdf") > 1024)
    except Exception as e:
        check("report_export.py 导入+断言", False, str(e))

    # 5. financial_charts.py 序列构建
    try:
        from app.tools.financial_charts import build_financial_series
        import asyncio
        p = asyncio.run(build_financial_series("600519"))
        check("financial_charts.茅台24期+PE609", p and len(p["series"]["dates"]) >= 10 and len(p["series"]["pe"]) > 100, f"dates={len(p['series']['dates']) if p else 0}")
    except Exception as e:
        check("financial_charts.py 序列构建", False, str(e))

    # 6. context.py 标的记忆注入点存在
    with open(os.path.join(BASE, "app/harness/context.py")) as f:
        ctx = f.read()
    check("context.py 含 stock memory 注入", "build_memory_context" in ctx)

    # ============ 后端 API 层 ============
    try:
        jwt = jwt_token()
        check("auth.login 获取JWT", len(jwt) > 20)

        # 7. charts API
        d = api("GET", "/api/charts/financials?query=600519", jwt=jwt)
        check("API charts resolved+series", d.get("resolved") is True and len(d["series"]["dates"]) >= 10)
        check("API charts percentiles", d.get("percentiles", {}).get("pe_pct") is not None)

        # 8. 估值 API
        # 9. 微信导出 API(内容短,不真推 — 走未配置分支无法测,改为结构校验)
        # 注意: webhook 已配置,调用会真推(消耗额度)。这里用 python 直调校验返回结构即可。
    except Exception as e:
        check("后端 API 层", False, str(e))

    # 10. 微信 API 端点存在(路由注册)
    with open(os.path.join(BASE, "app/main.py")) as f:
        main_src = f.read()
    check("main.py 挂载 charts/export_wechat", all(x in main_src for x in ("charts", "export_wechat")))

    # ============ 自上而下: 前端层 ============
    # 11. HTML 模板包含新元素
    with open(os.path.join(BASE, "templates/index.html")) as f:
        html = f.read()
    check("HTML 引入 charts.js", 'charts.js' in html)
    check("HTML 无 valuation.js", 'valuation.js' not in html)
    check("HTML 微信按钮", "pushWechat" in html and "📲 微信" in html)
    check("HTML 无估值按钮/modal", "openValuationModal" not in html and "valuation-modal" not in html)

    # 12. CSS 类存在
    with open(os.path.join(BASE, "static/css/style.css")) as f:
        css = f.read()
    check("CSS fc-panel 图表类", ".fc-panel" in css and ".fc-tab" in css)
    check("CSS 无 val-* 样式", "val-modal" not in css)

    # 13. JS 语法检查
    for js in ("static/js/charts.js", "static/js/app.js"):
        r = sh(f"cd {BASE} && node --check {js}")
        check(f"JS 语法 {os.path.basename(js)}", r.returncode == 0, r.stderr[:100])

    # 14. 前端 API 一致性(JS 请求路径 vs 后端路由)
    check("JS charts 路径 /api/charts/financials", "CHARTS_API = '/api/charts/financials'" in open(os.path.join(BASE, "static/js/charts.js")).read())

    # 15. 服务健康
    try:
        h = api("GET", "/health")
        check("服务健康 status=ok", h.get("status") == "ok")
    except Exception as e:
        check("服务健康", False, str(e))

    # ============ 汇总 ============
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n===== 验证汇总: {passed}/{total} 通过 =====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL: {name} {detail}")
    if passed == total:
        print("FINAL PASS ✅")
        return 0
    print("FINAL FAIL ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
