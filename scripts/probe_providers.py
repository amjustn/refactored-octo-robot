#!/usr/bin/env python
"""Probe every LLM provider for chat + function-calling capability.

投研 Agent 依赖 OpenAI 标准 function calling 获取实时数据。本脚本对
模型设置里的每个 provider 跑两项检查：
  1. ping        — 普通对话补全是否可用
  2. tool-calling — 模型/端点是否吐结构化 tool_calls

Usage:
    venv/bin/python scripts/probe_providers.py            # 探测所有已配 key 的 provider
    venv/bin/python scripts/probe_providers.py zhipu kimi # 只探测指定 provider

Keys are read from .env (NEVER printed). 环境变量命名见 PROVIDERS 表。
注意：不要往 .env 写 OPENAI_API_KEY —— config.py 会把它当成服务器默认
LLM key，直接破坏服务器默认配置。OpenAI 测试请用 OPENAI_KEY。

Provider/model 目录与 static/js/app.js 的 LLM_PROVIDER_PRESETS 保持同步。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.core.llm import chat_complete, probe_tool_calling  # noqa: E402

# provider -> (base_url, env var for key, models to probe)
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com", "LLM_API_KEY", ["deepseek-v4-flash"]),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY", ["glm-4.7", "glm-4.5"]),
    "kimi": ("https://api.moonshot.cn/v1", "KIMI_API_KEY", ["kimi-k3"]),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "QWEN_API_KEY", ["qwen3-plus"]),
    "doubao": ("https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY", ["doubao-1.5-pro-256k"]),
    "hunyuan": ("https://api.hunyuan.cloud.tencent.com/v1", "HUNYUAN_API_KEY", ["hunyuan-turbos"]),
    "baidu": ("https://qianfan.baidubce.com/v2", "QIANFAN_API_KEY", ["ernie-4.5-turbo"]),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", ["gemini-2.5-flash"]),
    "openai": ("https://api.openai.com/v1", "OPENAI_KEY", ["gpt-4o-mini"]),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_KEY", ["claude-sonnet-4-20250514"]),
    "ollama": ("http://localhost:11434/v1", "", ["qwen3:14b"]),
}


async def probe_model(provider: str, base_url: str, api_key: str, model: str) -> tuple:
    """Returns (ping_ok, tools_ok, detail)."""
    cfg = {"model": model, "base_url": base_url}
    if api_key:
        cfg["api_key"] = api_key
    try:
        reply = await asyncio.wait_for(
            chat_complete("你是连接测试助手。只回复一个词：pong", "ping",
                          llm_config=cfg, temperature=0),
            timeout=30.0,
        )
        if not (reply or "").strip():
            return False, False, "ping 返回空"
    except asyncio.TimeoutError:
        return False, False, "ping 超时（30s）"
    except Exception as e:
        return False, False, f"ping 失败: {str(e)[:100]}"

    try:
        tools_ok, tools_detail = await asyncio.wait_for(
            probe_tool_calling(cfg), timeout=45.0
        )
    except asyncio.TimeoutError:
        return True, False, "工具探测超时（45s）"
    except Exception as e:
        return True, False, f"工具探测异常: {str(e)[:100]}"
    return True, tools_ok, tools_detail


async def main() -> int:
    only = sys.argv[1:]
    rows = []
    for name, (base_url, env_var, models) in PROVIDERS.items():
        if only and name not in only:
            continue
        api_key = os.getenv(env_var, "") if env_var else ""
        if env_var and not api_key:
            rows.append((name, "-", "跳过", f".env 未配置 {env_var}"))
            continue
        for model in models:
            ping_ok, tools_ok, detail = await probe_model(name, base_url, api_key, model)
            status = "✅ 全通" if (ping_ok and tools_ok) else ("⚠️ 仅对话" if ping_ok else "❌ 不可用")
            rows.append((name, model, status, detail))

    width = max(len(r[1]) for r in rows) if rows else 10
    print(f"\n{'provider':<12} {'model':<{width}}  {'结果':<8} 说明")
    print("-" * (36 + width + 20))
    for name, model, status, detail in rows:
        print(f"{name:<12} {model:<{width}}  {status:<8} {detail}")

    bad = [r for r in rows if "❌" in r[2] or "⚠️" in r[2]]
    print()
    if bad:
        print("结论：以上标 ⚠️/❌ 的模型不能直接用于投研 Agent（运行时会触发默认模型回退）。")
        return 1
    print("结论：所有已探测 provider 均支持工具调用。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
