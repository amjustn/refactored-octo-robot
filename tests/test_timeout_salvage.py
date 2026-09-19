"""超时抢救 + LLM 配置守卫 — 回归测试（2026-09-19）

线上故障 task 9d717fe98c67：investment-team 的 4 个 Agent 全部撞上 300s 上限，
落盘只剩 4 行「[错误] Agent 超时 (300s)」，已经取到的一手数据全丢。
这里固定四条契约：
  1) 自定义 base_url 但没带 api_key → 必须在发请求之前给出可读错误，
     不能拿服务端 DeepSeek Key 去请求别人家（task a031a4ac291e / 3ca21a69f0ac 的 401）。
  2) 超时中断时，该 Agent 已执行的工具原始数据要能捞回成「部分成果」，
     且不得被当成完整成果参与综合（success_count 不增）。
  3) resume 复用判定必须把「部分成果」视为要重跑。
  4) 单次 LLM 请求超时必须小于 Agent 总预算，否则一次 hang 吃掉整个 Agent。
"""
import json
import types

from app.core import config as core_config
from app.core.llm import custom_base_without_key
from app.harness import _is_reusable_artifact
from app.harness import runner as runner_mod
from app.harness.runner import PARTIAL_SALVAGE_MARK, AgentRunner


def test_custom_base_without_key_blocks_foreign_endpoint():
    err = custom_base_without_key({
        "model": "qwen3.8-max",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    })
    assert err, "自定义服务商没带 Key 时必须报错"
    assert "API Key" in err and "dashscope" in err, err


def test_custom_base_without_key_allows_server_gateway_and_own_key():
    assert custom_base_without_key() is None
    assert custom_base_without_key({"model": "deepseek-v4-flash"}) is None
    # 与服务端网关同址（含尾斜杠差异）→ 用服务端 Key，放行
    assert custom_base_without_key({"base_url": core_config.LLM_BASE_URL + "/"}) is None
    assert custom_base_without_key({
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "sk-own",
    }) is None


def test_llm_request_timeout_below_agent_budget():
    assert 0 < core_config.LLM_REQUEST_TIMEOUT < core_config.AGENT_TIMEOUT


def test_salvage_keeps_own_agent_tool_data_only(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "SCRATCHPAD_DIR", tmp_path)
    rows = [
        {"ts": 1, "agent": "business-analyst", "tool": "get_financial_data",
         "args": {"symbol": "300308"}, "result": '{"roe": 12}'},
        {"ts": 2, "agent": "risk-assessor", "tool": "quant_factor_signal",
         "args": {"codes": "300308"}, "result": "alert"},
        {"ts": 3, "agent": "business-analyst", "tool": "calc",
         "args": {"expression": "1+1"}, "result": "2"},
    ]
    (tmp_path / "t123.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    out = AgentRunner._salvage_tool_trace(  # type: ignore[arg-type]
        types.SimpleNamespace(task_id="t123"), "business-analyst", 600)
    assert out.startswith(PARTIAL_SALVAGE_MARK)
    assert "get_financial_data" in out and "calc" in out
    assert "quant_factor_signal" not in out, "别的 Agent 的数据不能混进来"
    assert "600s" in out


def test_salvage_returns_empty_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "SCRATCHPAD_DIR", tmp_path)
    fake = types.SimpleNamespace(task_id="nope")
    assert AgentRunner._salvage_tool_trace(fake, "business-analyst", 600) == ""  # type: ignore[arg-type]
    (tmp_path / "nope.jsonl").write_text(
        json.dumps({"agent": "other", "tool": "t", "result": "r"}), encoding="utf-8")
    assert AgentRunner._salvage_tool_trace(fake, "business-analyst", 600) == ""  # type: ignore[arg-type]


def test_partial_salvage_not_reused_on_resume():
    assert _is_reusable_artifact("正常报告") is True
    assert _is_reusable_artifact("[错误] Agent 超时 (600s)") is False
    assert _is_reusable_artifact(PARTIAL_SALVAGE_MARK + " 原始数据…") is False
    assert _is_reusable_artifact("") is False
