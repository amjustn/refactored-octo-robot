#!/usr/bin/env bash
# Local CI mirror: lint, pytest suite, script-style tests, offline eval replay.
# Usage: scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=venv/bin/python
RUFF=venv/bin/ruff

echo "== 1/4 ruff check app/ eval/ =="
"$RUFF" check app/ eval/

echo "== 2/4 pytest tests/ (script-style files excluded) =="
"$PY" -m pytest tests/ -q \
  --ignore=tests/test_bug_fixes_round1.py \
  --ignore=tests/test_bug_fixes_round2.py \
  --ignore=tests/test_skill_knowledge_full_chain.py

echo "== 3/4 bug-fix script tests =="
"$PY" tests/test_bug_fixes_round1.py
"$PY" tests/test_bug_fixes_round2.py

echo "== 4/5 eval replay (stub LLM, offline) =="
"$PY" eval/replay.py --all --stub-llm

echo "== 5/5 批C 注入层自检（只读生产决策日志） =="
"$PY" scripts/verify_history_grouping.py
"$PY" scripts/verify_history_trim_budget.py
"$PY" scripts/verify_history_injection_e2e.py --stub

echo "ALL CHECKS PASSED"
