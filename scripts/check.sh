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

echo "== 4/4 eval replay (stub LLM, offline) =="
"$PY" eval/replay.py --all --stub-llm

echo "ALL CHECKS PASSED"
