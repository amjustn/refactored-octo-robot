#!/bin/bash
# AI Berkshire Web - Startup Script
# Run from within Hermes session to inherit LLM_API_KEY

cd ~/ai_berkshire_web
source venv/bin/activate
# 注意：本脚本只是手动入口。线上 8001 由 systemd --user 托管
# （~/.config/systemd/user/ai_berkshire.service，enabled + Restart=always）。
# 重启请用：systemctl --user restart ai_berkshire
# 直接 kill + 跑本脚本会和 systemd 的自动拉起抢端口（2026-09-12 踩过一次）。
# 监听地址：保持 127.0.0.1（只接本机）。
# Windows 上的浏览器用 http://localhost:8001 照旧能开（WSL 自带 localhost 转发，
# 实测 200），不依赖 0.0.0.0。改成 0.0.0.0 会让 WSL 网段/Docker 网段都能连进来，
# 而 /app 会给任何打开它的人注入一枚可用 JWT —— 若确需对外（局域网/隧道/反代），
# 先把 BERKSHIRE_BASIC_AUTH 配上再改这里。
exec uvicorn app.main:app --host 127.0.0.1 --port 8001 --log-level info
