"""Bug fix verification tests — Round 1.

Tests the two bugs found in the first round of top-down/bottom-up analysis:
1. Bug #1: run_single_agent now builds context when none is provided
2. Bug #2: update_skill_knowledge preserves existing data when LLM returns non-JSON
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock, patch

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PASS = 0
FAIL = 0
ERRORS = []

def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  ✗ {name} — {detail}")


print("=" * 70)
print("Bug Fix Verification — Round 1")
print("=" * 70)

# ==================== Bug #1: run_single_agent builds context automatically ====================
print("\n🐛 [Bug #1] run_single_agent builds context when none is provided")

from app.harness.runner import run_single_agent

# Mock the LLM to capture the system prompt
captured_system_prompt = {"value": ""}

async def mock_chat_complete(system, user, llm_config=None):
    captured_system_prompt["value"] = system
    return "Mock analysis report"

with patch("app.harness.runner.chat_complete", new_callable=AsyncMock) as mock_cc:
    mock_cc.side_effect = mock_chat_complete

    try:
        report = asyncio.run(run_single_agent("investment-research", "腾讯"))
        test(
            "run_single_agent returns non-empty report",
            bool(report) and len(report) > 5,
            f"report: {repr(report[:50])}"
        )

        # The system prompt should contain context (time, skill knowledge, etc.)
        system_prompt = captured_system_prompt["value"]
        test(
            "System prompt contains time context (当前时间)",
            "当前时间" in system_prompt,
            f"missing 当前时间 in system prompt"
        )
        test(
            "System prompt contains skill knowledge (能力领域知识 or 分析框架)",
            "能力领域知识" in system_prompt or "分析框架" in system_prompt,
            f"missing skill knowledge in system prompt"
        )
        test(
            "System prompt contains investment master knowledge (四大师 or 巴菲特)",
            "四大师" in system_prompt or "巴菲特" in system_prompt or "投资" in system_prompt,
            f"missing master knowledge"
        )
    except Exception as e:
        test("run_single_agent with auto-context runs successfully", False, str(e))

# Test: when context IS provided, it's used (not rebuilt)
captured_system_prompt2 = {"value": ""}

async def mock_chat_complete2(system, user, llm_config=None):
    captured_system_prompt2["value"] = system
    return "Mock report 2"

with patch("app.harness.runner.chat_complete", new_callable=AsyncMock) as mock_cc2:
    mock_cc2.side_effect = mock_chat_complete2

    try:
        custom_context = "## CUSTOM CONTEXT MARKER\nThis is a test context."
        report = asyncio.run(run_single_agent("earnings-review", "腾讯 2025Q4", context=custom_context))
        test(
            "When context is provided, it's used directly",
            "CUSTOM CONTEXT MARKER" in captured_system_prompt2["value"],
            f"custom context not found in system prompt"
        )
    except Exception as e:
        test("run_single_agent with provided context runs successfully", False, str(e))


# ==================== Bug #2: update_skill_knowledge preserves data on non-JSON ====================
print("\n🐛 [Bug #2] update_skill_knowledge preserves existing data when LLM returns non-JSON")

from app.tools.skill_knowledge import (
    update_skill_knowledge,
    _ensure_all_default_knowledge,
    _load_skill_knowledge,
    get_skill_knowledge_for_prompt,
    SKILL_KNOWLEDGE_DIR,
)

# Simulate LLM returning non-JSON garbage
garbage_response = "Sorry, I cannot generate JSON right now. Please try again later."

with patch("app.tools.skill_knowledge.chat_complete", new_callable=AsyncMock) as mock_chat:
    mock_chat.return_value = garbage_response

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(sys.modules['app.tools.skill_knowledge'], 'SKILL_KNOWLEDGE_DIR', Path(tmpdir)):
            # First, create default knowledge
            _ensure_all_default_knowledge()

            # Verify default has structured fields
            before = _load_skill_knowledge("earnings-review")
            test(
                "Before update: has latest_frameworks",
                bool(before.get("latest_frameworks")),
                "missing latest_frameworks in default"
            )
            test(
                "Before update: has key_criteria",
                bool(before.get("key_criteria")),
                "missing key_criteria in default"
            )

            # Now try to update (will fail because LLM returns garbage)
            # Force update by setting last_updated to old date
            import app.tools.skill_knowledge as sk_module
            old_data = dict(before)
            old_data["last_updated"] = "2020-01-01T00:00:00"  # Old date to trigger update
            from app.tools.skill_knowledge import _save_skill_knowledge
            _save_skill_knowledge("earnings-review", old_data)

            result = asyncio.run(update_skill_knowledge("earnings-review"))

            test(
                "Update result is non-empty",
                bool(result),
                "empty result"
            )
            test(
                "Update result has _update_failed flag",
                result.get("_update_failed") == True,
                f"missing _update_failed flag"
            )

            # CRITICAL: structured fields should be preserved!
            after = _load_skill_knowledge("earnings-review")
            test(
                "After failed update: latest_frameworks is preserved (not empty)",
                bool(after.get("latest_frameworks")),
                "latest_frameworks was lost after bad LLM update!"
            )
            test(
                "After failed update: key_criteria is preserved (not empty)",
                bool(after.get("key_criteria")),
                "key_criteria was lost after bad LLM update!"
            )
            test(
                "After failed update: data_sources is preserved (not empty)",
                bool(after.get("data_sources")),
                "data_sources was lost after bad LLM update!"
            )
            test(
                "After failed update: update_notes is preserved (not empty)",
                bool(after.get("update_notes")),
                "update_notes was lost after bad LLM update!"
            )

            # CRITICAL: prompt injection should still work!
            prompt_text = get_skill_knowledge_for_prompt("earnings-review")
            test(
                "After failed update: prompt injection still returns non-empty",
                bool(prompt_text) and len(prompt_text) > 50,
                "prompt injection returned empty after bad update!"
            )
            test(
                "After failed update: prompt injection contains 分析框架",
                "分析框架" in prompt_text,
                f"missing 分析框架 in prompt: {prompt_text[:100]}"
            )


# ==================== Bug #2b: update_skill_knowledge with valid JSON but missing fields ====================
print("\n🐛 [Bug #2b] update_skill_knowledge fills missing fields from existing data")

partial_json_response = json.dumps({
    "skill_name": "earnings-review",
    "display_name": "财报精读",
    "latest_frameworks": "更新后的框架内容",
    # key_criteria, data_sources, update_notes are MISSING
    "last_updated": datetime.now().isoformat(),
})

with patch("app.tools.skill_knowledge.chat_complete", new_callable=AsyncMock) as mock_chat:
    mock_chat.return_value = partial_json_response

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(sys.modules['app.tools.skill_knowledge'], 'SKILL_KNOWLEDGE_DIR', Path(tmpdir)):
            _ensure_all_default_knowledge()

            # Set old date to trigger update
            old_data = dict(_load_skill_knowledge("earnings-review"))
            old_data["last_updated"] = "2020-01-01T00:00:00"
            _save_skill_knowledge("earnings-review", old_data)

            result = asyncio.run(update_skill_knowledge("earnings-review"))

            test(
                "Partial JSON update: has updated latest_frameworks",
                result.get("latest_frameworks") == "更新后的框架内容",
                f"wrong latest_frameworks: {result.get('latest_frameworks')}"
            )
            test(
                "Partial JSON update: key_criteria filled from existing",
                bool(result.get("key_criteria")),
                "key_criteria not filled from existing"
            )
            test(
                "Partial JSON update: data_sources filled from existing",
                bool(result.get("data_sources")),
                "data_sources not filled from existing"
            )


# ==================== Summary ====================
print("\n" + "=" * 70)
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print("=" * 70)

if ERRORS:
    print("\n❌ FAILED TESTS:")
    for e in ERRORS:
        print(f"  - {e}")

# pytest 兼容 (2026-08-27): 模块导入(收集)时不退出 — 顶层脚本仅直接运行时生效
if __name__ == "__main__":
    sys.exit(0 if FAIL == 0 else 1)
