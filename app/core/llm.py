"""LLM API wrapper for AI Berkshire Web

Provides streaming and non-streaming chat completions with automatic retry
for transient errors (429 rate limit, 503 service unavailable), and
async function-calling support for financial + market data tools.
"""
import asyncio
import json
import logging
import re
from contextvars import ContextVar
from typing import AsyncGenerator, Awaitable, Callable, Optional

from openai import AsyncOpenAI

from .config import AGENT_TIMEOUT, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger("ai_berkshire.llm")

_client: Optional[AsyncOpenAI] = None
_clients: dict[tuple, AsyncOpenAI] = {}
_MAX_CLIENTS = 32  # FIFO bound: per-user base_url/api_key combos are unbounded


def get_client(base_url: str = None, api_key: str = None) -> AsyncOpenAI:
    """Get (or create) an OpenAI client for the given config.

    Clients are cached per (base_url, api_key) so per-user model configs
    each get their own connection pool without recreating clients. The
    cache is FIFO-bounded at _MAX_CLIENTS; evicted clients are closed
    best-effort (sync close only, never awaited in this path).
    """
    base_url = base_url or LLM_BASE_URL
    api_key = api_key or LLM_API_KEY
    key = (base_url, api_key)
    if key not in _clients:
        if len(_clients) >= _MAX_CLIENTS:
            oldest_key = next(iter(_clients))
            evicted = _clients.pop(oldest_key)
            _close_client_safely(evicted)
        _clients[key] = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=AGENT_TIMEOUT,
        )
    return _clients[key]


def _close_client_safely(client: AsyncOpenAI) -> None:
    """Best-effort close of an evicted client.

    AsyncOpenAI.close() 是协程：能拿到运行中的 loop 就调度关闭；
    拿不到（同步上下文）就跳过 —— 进程会随 GC 回收连接，注释说明即可，
    绝不能为了关闭而新建 loop（会泄漏线程）。
    """
    try:
        close = getattr(client, "close", None)
        if close is None:
            return
        if asyncio.iscoroutinefunction(close):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 无运行中的事件循环：跳过协程关闭（连接由 GC/进程退出回收）
                return
            loop.create_task(close())
        else:
            close()
    except Exception:
        pass  # best-effort; dropping the reference is enough


def resolve_llm_config(llm_config: dict = None) -> tuple:
    """Normalize a per-request LLM override into (base_url, api_key, model)."""
    cfg = llm_config or {}
    return (
        (cfg.get("base_url") or "").strip() or None,
        (cfg.get("api_key") or "").strip() or None,
        (cfg.get("model") or "").strip() or None,
    )

# ==================== Token Usage Tracking ====================
# Per-research-task token accounting. main.py sets the context at the start
# of a research run; every LLM call in that asyncio context accumulates usage.
_usage_task_var: ContextVar[Optional[str]] = ContextVar("llm_usage_task", default=None)
_usage_totals: dict[str, dict] = {}


def _new_usage_bucket() -> dict:
    """Fresh per-task usage bucket with a per-model breakdown sub-table."""
    return {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "by_model": {},  # model -> {prompt_tokens, completion_tokens, total_tokens}
    }


def set_usage_context(task_id: str) -> None:
    """Bind token accounting to the current asyncio context."""
    _usage_task_var.set(task_id)
    _usage_totals.setdefault(task_id, _new_usage_bucket())


def get_usage(task_id: str) -> dict:
    """Return accumulated token usage for a task (含 by_model per-model 明细)."""
    return dict(_usage_totals.get(task_id, _new_usage_bucket()))


def clear_usage(task_id: str) -> None:
    """Drop accumulated usage for a finished task."""
    _usage_totals.pop(task_id, None)


def _record_usage(usage, model: Optional[str] = None) -> None:
    """Accumulate an OpenAI usage object into the current task context.

    model 传入本次调用的实际模型名：总量与 by_model 明细同步累加，
    供 persist 按 per-model 价目分别计价。
    """
    task_id = _usage_task_var.get()
    if not task_id or not usage:
        return
    totals = _usage_totals.setdefault(task_id, _new_usage_bucket())
    totals.setdefault("by_model", {})  # 防御旧 bucket 形状
    p = getattr(usage, "prompt_tokens", 0) or 0
    c = getattr(usage, "completion_tokens", 0) or 0
    t = getattr(usage, "total_tokens", 0) or 0
    totals["prompt_tokens"] += p
    totals["completion_tokens"] += c
    totals["total_tokens"] += t
    slot = totals["by_model"].setdefault(
        (model or "unknown")[:300],
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    )
    slot["prompt_tokens"] += p
    slot["completion_tokens"] += c
    slot["total_tokens"] += t

# Retry config
_MAX_RETRIES = 3
_RETRY_DELAYS = [2, 5, 10]  # seconds
_MAX_TOOL_ROUNDS = 6  # Max back-and-forth rounds for function calling

# ==================== Tool-fallback (custom model can't function-call) ====================
# When a user-supplied model/endpoint never emits structured tool_calls and
# answers with a "抱歉，我无法获取实时数据" style refusal, retry the same
# prompt once with the server default model so agents can still fetch data.
_tool_fallback_var: ContextVar[Optional[dict]] = ContextVar("llm_tool_fallback", default=None)

_REFUSAL_RE = re.compile(r"(抱歉|对不起)[^。\n]{0,12}我无法|我无法[^。\n]{0,10}(实时|市场).{0,4}数据")


def _looks_like_no_data_refusal(text: str) -> bool:
    """Heuristic: first-round refusal claiming no access to real-time data."""
    return bool(_REFUSAL_RE.search((text or "")[:200]))


def pop_tool_fallback() -> Optional[dict]:
    """Return (and clear) the fallback event recorded in this context, if any."""
    event = _tool_fallback_var.get()
    _tool_fallback_var.set(None)
    return event


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is worth retrying (rate limit, server error)."""
    exc_str = str(exc).lower()
    retryable_codes = ["429", "503", "502", "500", "timeout", "connection"]
    return any(code in exc_str for code in retryable_codes)


async def _retry_async(coro_func, *args, **kwargs):
    """Execute a coroutine with exponential backoff retry."""
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            return await coro_func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt == _MAX_RETRIES - 1:
                raise
            delay = _RETRY_DELAYS[attempt]
            logger.warning(f"LLM call failed (attempt {attempt+1}/{_MAX_RETRIES}), retrying in {delay}s: {e}")
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore


async def _create_completion(client, **kwargs):
    """Create a chat completion, dropping params the provider rejects.

    Some models (e.g. Kimi K3) only accept their default temperature and
    return 400 'invalid temperature' for any explicit value; many third-party
    relays (one-api/new-api 中转站) reject `stream_options`. Retry without
    the offending parameter so every OpenAI-compatible endpoint works.
    """
    for _ in range(3):
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as e:
            msg = str(e).lower()
            if "temperature" in kwargs and "temperature" in msg:
                logger.info(f"Model {kwargs.get('model')} rejects explicit temperature, retrying without it")
                kwargs.pop("temperature", None)
                continue
            if "stream_options" in kwargs and ("stream_options" in msg or "include_usage" in msg):
                logger.info("Endpoint rejects stream_options, retrying without it (token stats may be incomplete)")
                kwargs.pop("stream_options", None)
                continue
            raise
    return await client.chat.completions.create(**kwargs)


async def chat_stream(
    system_prompt: str,
    user_message: str,
    model: str = None,
    temperature: float = 0.7,
    llm_config: dict = None,
) -> AsyncGenerator[str, None]:
    """Stream a chat completion from the LLM with retry on connection errors."""
    cfg_base, cfg_key, cfg_model = resolve_llm_config(llm_config)
    client = get_client(cfg_base, cfg_key)
    model = cfg_model or model or LLM_MODEL

    async def _create_stream():
        return await _create_completion(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )

    stream = await _retry_async(_create_stream)
    async for chunk in stream:
        if chunk.usage:
            _record_usage(chunk.usage, model)
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def chat_complete(
    system_prompt: str,
    user_message: str,
    model: str = None,
    temperature: float = 0.7,
    llm_config: dict = None,
    history: list = None,
) -> str:
    """Complete a chat (non-streaming) with automatic retry.

    Optional ``history`` is a list of prior {"role", "content"} turns
    inserted between the system prompt and the final user message.
    """
    cfg_base, cfg_key, cfg_model = resolve_llm_config(llm_config)
    client = get_client(cfg_base, cfg_key)
    model = cfg_model or model or LLM_MODEL

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    async def _create():
        return await _create_completion(
            client,
            model=model,
            messages=messages,
            temperature=temperature,
        )

    response = await _retry_async(_create)
    if response.usage:
        _record_usage(response.usage, model)
    return response.choices[0].message.content or ""


async def chat_complete_with_tools(
    system_prompt: str,
    user_message: str,
    tools: list = None,
    tool_executor: Callable[[str, dict], Awaitable[str]] = None,
    model: str = None,
    temperature: float = 0.7,
    llm_config: dict = None,
) -> str:
    """Complete a chat with async function-calling support.

    The LLM can call tools (e.g., financial calculators, market data fetchers)
    during analysis. This function handles the tool-call loop:
    send -> receive tool calls -> execute (async) -> send results -> repeat.

    Args:
        system_prompt: System prompt for the LLM
        user_message: User message
        tools: List of tool schemas in OpenAI function-calling format
        tool_executor: Async callable(tool_name, arguments_dict) -> str
        model: LLM model name
        temperature: Sampling temperature

    Returns:
        Final text response from the LLM
    """
    if not tools or not tool_executor:
        return await chat_complete(system_prompt, user_message, model, temperature, llm_config)

    cfg_base, cfg_key, cfg_model = resolve_llm_config(llm_config)
    client = get_client(cfg_base, cfg_key)
    model = cfg_model or model or LLM_MODEL
    override_active = bool(cfg_base or cfg_key or cfg_model)
    _tool_fallback_var.set(None)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    for round_num in range(_MAX_TOOL_ROUNDS):
        async def _create():
            return await _create_completion(
                client,
                model=model,
                messages=messages,
                temperature=temperature,
                tools=tools,
            )

        try:
            response = await _retry_async(_create)
        except Exception as e:
            logger.warning(
                f"Tool-enabled chat failed (round {round_num}): {e}, "
                "falling back to a no-tools completion over existing messages"
            )
            # Preserve prior tool results: retry without tools over the SAME
            # message list instead of discarding them in a fresh plain chat.
            async def _create_no_tools():
                return await _create_completion(
                    client,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                )

            try:
                response = await _retry_async(_create_no_tools)
                if response.usage:
                    _record_usage(response.usage, model)
                return response.choices[0].message.content or ""
            except Exception as e2:
                logger.warning(f"No-tools fallback failed: {e2}, falling back to plain chat")
                return await chat_complete(system_prompt, user_message, model, temperature, llm_config)

        if response.usage:
            _record_usage(response.usage, model)

        choice = response.choices[0]
        message = choice.message

        # If no tool calls, we're done
        if not message.tool_calls:
            content = message.content or ""
            # A tool-enabled agent that refuses in round 0 ("我无法获取实时数据")
            # almost always means the user-configured model/endpoint cannot do
            # function calling. Retry once with the server default model so the
            # run still fetches real data instead of writing an empty report.
            if round_num == 0 and override_active and _looks_like_no_data_refusal(content):
                logger.warning(
                    f"Custom model '{model}' refused without any tool call; "
                    f"retrying with server default model '{LLM_MODEL}'"
                )
                result = await chat_complete_with_tools(
                    system_prompt, user_message, tools, tool_executor,
                    model=None, temperature=temperature, llm_config=None,
                )
                # Set AFTER the recursive call — its own entry resets the var.
                _tool_fallback_var.set({"from_model": model, "to_model": LLM_MODEL})
                return result
            return content

        # Add assistant message with tool calls
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ],
        })

        # Execute each tool call (async) and add results
        for tc in message.tool_calls:
            tool_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}

            logger.info(f"Agent calling tool: {tool_name}({args})")
            try:
                result = await tool_executor(tool_name, args)
            except Exception as e:
                result = json.dumps({"error": f"Tool execution failed: {e}"}, ensure_ascii=False)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # Max rounds reached — get final response without tools
    logger.warning(f"Max tool rounds ({_MAX_TOOL_ROUNDS}) reached, requesting final summary")
    # Nudge hard: some models (e.g. kimi-k3) otherwise answer the no-tools
    # completion with another intent line, producing an empty report
    # section despite a successful data-gathering loop.
    messages.append({
        "role": "user",
        "content": "工具调用轮次已用完。请立即基于以上已经获取到的数据，输出完整的分析正文，禁止再请求调用任何工具。",
    })

    async def _create_final():
        return await _create_completion(
            client,
            model=model,
            messages=messages,
            temperature=temperature,
        )

    try:
        response = await _retry_async(_create_final)
        if response.usage:
            _record_usage(response.usage, model)
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"Final summary failed: {e}")
        return "（分析完成，但工具调用轮次已达上限，无法生成最终摘要）"


# ==================== Capability probe (for /api/llm/test) ====================
_PROBE_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": "查询股票最新价格",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "股票代码"}},
                "required": ["symbol"],
            },
        },
    }
]


async def probe_tool_calling(llm_config: dict = None) -> tuple:
    """Probe whether the configured model/endpoint performs function calling.

    Returns (supported: bool, detail: str). Research agents depend on
    structured tool_calls to fetch real market data; a model or relay that
    drops the ``tools`` parameter produces hollow "无法获取实时数据" reports.
    """
    cfg_base, cfg_key, cfg_model = resolve_llm_config(llm_config)
    client = get_client(cfg_base, cfg_key)
    model = cfg_model or LLM_MODEL
    try:
        response = await _create_completion(
            client,
            model=model,
            messages=[
                {"role": "system", "content": "你是数据查询助手。需要数据时必须调用提供的工具，禁止直接回答。"},
                {"role": "user", "content": "请查询贵州茅台(600519)的最新股价"},
            ],
            tools=_PROBE_TOOL_SCHEMA,
            temperature=0,
        )
    except Exception as e:
        return False, f"工具调用请求被端点拒绝: {str(e)[:120]}"
    message = response.choices[0].message
    if message.tool_calls:
        return True, "工具调用正常"
    content = message.content or ""
    if "DSML" in content or "tool_call" in content:
        return False, "模型以文本形式输出工具调用，非标准 function calling"
    return False, "模型未发起工具调用（tools 参数可能被中转端点丢弃）"
