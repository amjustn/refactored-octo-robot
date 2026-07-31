#!/bin/bash
# AI Berkshire Web - Startup Script
# Run from within Hermes session to inherit LLM_API_KEY

cd ~/ai_berkshire_web
source venv/bin/activate
exec uvicorn app.main:app --host 0.0.0.0 --port 8001 --log-level info
