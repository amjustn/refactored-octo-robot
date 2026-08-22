#!/usr/bin/env python3
"""Team Lead 综合环节独立模型（synthesis_model）功能测试。

用 mock 验证：
1. 设置 synthesis_model 后，Team Lead 综合调用收到的 llm_config.model
   为新模型，研究 Agent / post_synthesis Agent 仍为 base 模型；
2. 不设置（或与 base 相同）时，所有调用模型一致（旧版行为回归）；
3. LLMConfig.to_synthesis_config_dict 的单元行为；
4. main._validate_llm_config 对 synthesis_model 的清洗与白名单校验。

"""
import os
import sys
import asyncio

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


BASE_MODEL = "deepseek-v4-flash"
SYNTHESIS_MODEL = "deepseek-v4-pro"


# ==================== mock 工具 ====================

@pytest.fixture
def chat_calls(monkeypatch):
    """拦截 runner 的三个 LLM 入口，记录 (kind, system_prompt, llm_config)。"""
    from app.harness import runner as runner_mod
    from app.harness.context import ContextBuilder

    calls = []

    async def fake_chat_stream(system, user, llm_config=None, **kw):
        calls.append(("stream", system, dict(llm_config) if llm_config else None))
        yield "流式综合内容"

    async def fake_chat_complete(system, user, llm_config=None, **kw):
        calls.append(("complete", system, dict(llm_config) if llm_config else None))
        return "完整输出"

    async def fake_chat_complete_with_tools(system, user, tools=None, tool_executor=None, llm_config=None, **kw):
        calls.append(("tools", system, dict(llm_config) if llm_config else None))
        return "研究结论"

    monkeypatch.setattr(runner_mod, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(runner_mod, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(runner_mod, "chat_complete_with_tools", fake_chat_complete_with_tools)

    class _FakeBundle:
        context = ""

    async def _fake_build(self, arguments, skill_name=None, llm_config=None):
        return _FakeBundle()

    monkeypatch.setattr(ContextBuilder, "build", _fake_build)
    return calls


def _split_calls(calls):
    """按 system_prompt 区分 Team Lead 综合调用与其余 Agent 调用。

    P5 交叉辩论阶段会产生额外的 chat_complete 调用（system 为辩论 prompt），
    它们既不是综合调用也不是研究 Agent 调用，先按辩论标记剔除再拆分。
    """
    from app.harness.prompts import TEAM_LEAD_PROMPT
    lead_prefix = TEAM_LEAD_PROMPT[:40]
    debate_marker = "请从你的专业角度审视这份报告"
    non_debate = [c for c in calls if debate_marker not in c[1]]
    lead = [c for c in non_debate if c[1].startswith(lead_prefix)]
    others = [c for c in non_debate if not c[1].startswith(lead_prefix)]
    return lead, others


def _run_multi(chat_calls, skill_name, llm_override):
    from app.harness.spec import TaskSpec
    from app.harness.runner import AgentRunner
    from app.skills import get_skill

    skill = get_skill(skill_name)
    spec = TaskSpec.build(skill, "测试目标", llm_override=llm_override)
    report = asyncio.run(AgentRunner().run(spec))
    assert "流式综合内容" in report  # 综合结果确实进入最终报告
    return _split_calls(chat_calls)


# ==================== 多Agent 流程（mock LLM） ====================

class TestSynthesisModelRouting:
    def test_team_lead_uses_synthesis_model_researchers_use_base(self, chat_calls):
        """设置 synthesis_model：Team Lead 用新模型，4 个研究 Agent 仍用 base。"""
        lead, others = _run_multi(chat_calls, "investment-team", {
            "model": BASE_MODEL,
            "synthesis_model": SYNTHESIS_MODEL,
        })
        assert len(lead) == 1, "Team Lead 综合应恰好调用一次"
        assert lead[0][2] is not None and lead[0][2].get("model") == SYNTHESIS_MODEL
        assert len(others) == 4, "investment-team 应有 4 个研究 Agent 调用"
        for _, _, cfg in others:
            assert cfg is not None and cfg.get("model") == BASE_MODEL

    def test_post_synthesis_agents_keep_base_model(self, chat_calls):
        """earnings-team：post_synthesis（编辑/评审）也保持 base 模型。"""
        lead, others = _run_multi(chat_calls, "earnings-team", {
            "model": BASE_MODEL,
            "synthesis_model": SYNTHESIS_MODEL,
        })
        assert len(lead) == 1
        assert lead[0][2].get("model") == SYNTHESIS_MODEL
        # 4 研究 Agent + editor + reader-reviewer
        assert len(others) == 6
        for _, _, cfg in others:
            assert cfg is not None and cfg.get("model") == BASE_MODEL

    def test_no_synthesis_model_keeps_single_model(self, chat_calls):
        """回归：不设置 synthesis_model 时全部调用用同一模型。"""
        lead, others = _run_multi(chat_calls, "investment-team", {"model": BASE_MODEL})
        assert len(lead) == 1
        assert lead[0][2] is not None and lead[0][2].get("model") == BASE_MODEL
        assert len(others) == 4
        for _, _, cfg in others:
            assert cfg is not None and cfg.get("model") == BASE_MODEL

    def test_no_override_at_all(self, chat_calls):
        """回归：完全不传 llm_config 时所有调用均为 None（服务器默认）。"""
        lead, others = _run_multi(chat_calls, "investment-team", None)
        assert len(lead) == 1 and lead[0][2] is None
        assert len(others) == 4
        for _, _, cfg in others:
            assert cfg is None

    def test_synthesis_model_equal_to_base_is_noop(self, chat_calls):
        """synthesis_model 与 base 相同时退化为单模型（零行为变化）。"""
        lead, others = _run_multi(chat_calls, "investment-team", {
            "model": BASE_MODEL,
            "synthesis_model": BASE_MODEL,
        })
        assert len(lead) == 1
        assert lead[0][2].get("model") == BASE_MODEL
        for _, _, cfg in others:
            assert cfg.get("model") == BASE_MODEL


# ==================== LLMConfig 单元行为 ====================

class TestSynthesisConfigDict:
    def test_unset_returns_base_dict(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({"model": BASE_MODEL})
        assert cfg.to_synthesis_config_dict() == {"model": BASE_MODEL}
        assert "synthesis_model" not in (cfg.to_llm_config_dict() or {})

    def test_equal_to_base_returns_base_dict(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({"model": BASE_MODEL, "synthesis_model": BASE_MODEL})
        assert cfg.to_synthesis_config_dict() == {"model": BASE_MODEL}

    def test_replaces_model_only(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({
            "model": BASE_MODEL,
            "synthesis_model": SYNTHESIS_MODEL,
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-x",
        })
        out = cfg.to_synthesis_config_dict()
        assert out == {
            "api_key": "sk-x",
            "base_url": "https://api.deepseek.com",
            "model": SYNTHESIS_MODEL,
        }
        # base dict 不受影响
        assert cfg.to_llm_config_dict()["model"] == BASE_MODEL

    def test_synthesis_only_uses_server_connection(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({"synthesis_model": SYNTHESIS_MODEL})
        assert cfg.to_llm_config_dict() is None  # base 无覆盖
        assert cfg.to_synthesis_config_dict() == {"model": SYNTHESIS_MODEL}

    def test_whitespace_stripped(self):
        from app.harness.spec import LLMConfig
        cfg = LLMConfig.from_override({"model": f"  {BASE_MODEL} ", "synthesis_model": f" {SYNTHESIS_MODEL}  "})
        assert cfg.synthesis_model == SYNTHESIS_MODEL
        assert cfg.to_synthesis_config_dict()["model"] == SYNTHESIS_MODEL


# ==================== 请求校验 ====================

class TestValidateLlmConfig:
    def test_accepts_synthesis_model(self):
        from app.main import _validate_llm_config
        out = _validate_llm_config({"model": BASE_MODEL, "synthesis_model": SYNTHESIS_MODEL})
        assert out == {"model": BASE_MODEL, "synthesis_model": SYNTHESIS_MODEL}

    def test_synthesis_model_sanitized(self):
        from app.main import _validate_llm_config
        out = _validate_llm_config({"synthesis_model": f"  {SYNTHESIS_MODEL}  "})
        assert out == {"synthesis_model": SYNTHESIS_MODEL}

    def test_unsupported_synthesis_model_rejected(self):
        from fastapi import HTTPException
        from app.main import _validate_llm_config
        from app.core.config import LLM_SUPPORTED_MODELS
        if not LLM_SUPPORTED_MODELS:
            pytest.skip("服务器未配置模型白名单")
        with pytest.raises(HTTPException) as exc:
            _validate_llm_config({"synthesis_model": "not-a-real-model"})
        assert exc.value.status_code == 400

    def test_synthesis_model_skips_list_check_with_custom_key(self):
        from app.main import _validate_llm_config
        out = _validate_llm_config({
            "synthesis_model": "any-third-party-model",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-x",
        })
        assert out["synthesis_model"] == "any-third-party-model"

    def test_base_model_validation_unchanged(self):
        from fastapi import HTTPException
        from app.main import _validate_llm_config
        from app.core.config import LLM_SUPPORTED_MODELS
        if not LLM_SUPPORTED_MODELS:
            pytest.skip("服务器未配置模型白名单")
        with pytest.raises(HTTPException):
            _validate_llm_config({"model": "not-a-real-model"})
        out = _validate_llm_config({"model": BASE_MODEL})
        assert out == {"model": BASE_MODEL}
