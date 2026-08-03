"""AgentRunner — single / multi / series strategies unified.

Absorbs agents/orchestrator.py (run_single_agent / run_multi_agent /
run_series) and the single-agent streaming branch formerly in main.py.

- Progress flows through the EventBus (``progress`` events, legacy name).
- multi / series now support chunk-level streaming: every agent's token
  stream is emitted as ``chunk`` events carrying an ``agent`` field
  (additive; the old frontend ignores the field). Tool-enabled agents use
  the (non-streaming) function-calling loop, so their result is emitted as
  a single whole-content chunk when done.
- Module-level run_single_agent / run_multi_agent / run_series wrappers
  preserve the old orchestrator public API for existing tests/scripts.

chat_stream / chat_complete / chat_complete_with_tools are imported at
module level so tests can patch ``app.harness.runner.chat_*`` exactly as
they patched ``app.agents.orchestrator.chat_*``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..core.llm import chat_stream, chat_complete, chat_complete_with_tools
from .context import ContextBuilder
from ..core.config import MAX_PARALLEL_AGENTS, MAX_REPORT_LENGTH, AGENT_TIMEOUT
from ..models.schemas import AgentProgress
from ..skills import get_skill, build_user_prompt
from .events import EventBus, EventType
from .prompts import (
    AGENT_ROLE_PROMPTS, TEAM_LEAD_PROMPT, SERIES_SYSTEM_PROMPT,
    get_default_system_prompt, get_role_label,
)
from .spec import TaskSpec
from .tools import ALL_TOOL_SCHEMAS, TOOL_ENABLED_AGENTS, get_gateway

logger = logging.getLogger("ai_berkshire.harness.runner")

# Series reports can be much longer (8 articles × 3000-5000 words each)
MAX_SERIES_REPORT_LENGTH = 200000  # 200k chars for series mode


class AgentRunner:
    """Unified agent execution. All observable progress goes to the bus."""

    def __init__(self, bus: Optional[EventBus] = None, task_id: Optional[str] = None):
        self.bus = bus
        self.task_id = task_id or "adhoc"

    # ---------- event helpers ----------

    def _emit(self, type_: EventType, **payload):
        if self.bus is not None:
            self.bus.emit(self.task_id, type_, **payload)

    def _progress(self, agent: str, status: str, message: str = "", progress: float = 0.0):
        self._emit(EventType.PROGRESS, agent=agent, status=status,
                   message=message, progress=progress)

    def _chunk(self, content: str, agent: Optional[str] = None):
        if agent:
            self._emit(EventType.CHUNK, agent=agent, content=content)
        else:
            self._emit(EventType.CHUNK, content=content)

    # ---------- dispatch ----------

    async def run(self, spec: TaskSpec) -> str:
        """Execute a TaskSpec; returns the final report text."""
        llm_config = spec.llm.to_llm_config_dict()
        if spec.strategy == "series":
            return await self.run_series(spec.skill_name, spec.arguments, llm_config=llm_config)
        if spec.strategy == "multi":
            return await self.run_multi(
                spec.skill_name, spec.arguments,
                llm_config=llm_config,
                synthesis_llm_config=spec.llm.to_synthesis_config_dict(),
                spec=spec,  # pass full spec for per-agent model lookup
            )
        return await self.run_single(
            spec.skill_name, spec.arguments,
            llm_config=llm_config, stream=spec.stream,
        )

    # ---------- tool execution ----------

    def _tool_executor(self, agent: Optional[str] = None):
        """Function-calling tool executor routed through the ToolGateway
        (circuit breaking, caching, timeout/retry, TOOL_CALL events)."""
        gateway = get_gateway()
        bus, task_id = self.bus, self.task_id

        async def _exec(name: str, args: dict) -> str:
            return await gateway.execute(name, args, task_id=task_id, agent=agent, bus=bus)

        return _exec

    # ---------- single ----------

    async def run_single(
        self,
        skill_name: str,
        arguments: str,
        agent_role: str = None,
        context: str = "",
        llm_config: dict = None,
        stream: bool = False,
    ) -> str:
        """Run a single agent analysis.

        If context is empty it is built automatically (all callers get
        proper context). Tool-enabled roles use function calling; with
        stream=True non-tool roles stream tokens as chunk events.
        """
        if agent_role and agent_role in AGENT_ROLE_PROMPTS:
            system_prompt = AGENT_ROLE_PROMPTS[agent_role]
            user_msg = f"请分析以下目标：{arguments}"
        else:
            system_prompt = get_default_system_prompt(skill_name)
            user_msg = build_user_prompt(skill_name, arguments)

        if not context:
            bundle = await ContextBuilder(bus=self.bus, task_id=self.task_id).build(
                arguments, skill_name=skill_name,
            )
            context = bundle.context

        if context:
            system_prompt = f"{system_prompt}\n\n{context}"

        if agent_role in TOOL_ENABLED_AGENTS:
            # Function-calling loop is non-streaming; emit whole result.
            result = await chat_complete_with_tools(
                system_prompt, user_msg,
                tools=ALL_TOOL_SCHEMAS,
                tool_executor=self._tool_executor(agent_role),
                llm_config=llm_config,
            )
            if stream:
                self._chunk(result, agent=agent_role)
            return result

        if stream:
            full = ""
            async for chunk in chat_stream(system_prompt, user_msg, llm_config=llm_config):
                full += chunk
                self._chunk(chunk, agent=agent_role)
            return full

        return await chat_complete(system_prompt, user_msg, llm_config=llm_config)

    # ---------- multi ----------

    async def run_multi(
        self,
        skill_name: str,
        arguments: str,
        llm_config: dict = None,
        synthesis_llm_config: dict = None,
        spec: "TaskSpec" = None,
    ) -> str:
        """Run multi-agent parallel analysis and synthesize.

        Supports phased execution: skills may define post_synthesis_agents
        that run AFTER the Team Lead synthesis.

        synthesis_llm_config：Team Lead 综合环节可选的独立 LLM override
        （用户可为综合环节单独指定更强模型）。为 None 或与 llm_config
        同模型时行为与旧版一致；研究 Agent 与 post_synthesis Agent
        始终使用 llm_config。

        per_agent_enabled：当 spec.llm.per_agent_enabled 为 True 时，
        每个研究 Agent 使用 spec.llm.to_agent_config_dict(agent_name)
        获取自己的模型配置（未映射的 Agent 回退到 llm_config）。
        post_synthesis Agent 也遵循相同的 per-agent 规则。
        """
        skill = get_skill(skill_name)
        agent_names = skill.get("agents", [])
        post_synthesis_agents = skill.get("post_synthesis_agents", [])

        if not agent_names:
            return await self.run_single(skill_name, arguments, llm_config=llm_config)

        self._progress("system", "running", "正在获取实时市场数据...")

        bundle = await ContextBuilder(bus=self.bus, task_id=self.task_id).build(
            arguments, skill_name=skill_name,
        )
        context = bundle.context

        self._progress("system", "completed", "市场数据获取完成", 1.0)

        research_agents = [a for a in agent_names if a not in post_synthesis_agents]

        # ── Per-agent model resolution ─────────────────────────
        # When per_agent_enabled is True, each agent gets its own model
        # from spec.llm.agent_models, falling back to llm_config otherwise.
        per_agent_enabled = spec is not None and spec.llm.per_agent_enabled
        if per_agent_enabled:
            total_overrides = len(spec.llm.agent_models)
            if total_overrides:
                logger.info(
                    f"Per-agent model enabled: {total_overrides} agent(s) have custom models "
                    f"(global model: {(llm_config or {}).get('model') or 'server default'})"
                )
            else:
                logger.info("Per-agent model enabled but no agent overrides set — all agents use global model")

        async def agent_task(name: str) -> tuple[str, str]:
            # Resolve per-agent LLM config
            agent_cfg = llm_config
            if per_agent_enabled and spec is not None:
                agent_cfg = spec.llm.to_agent_config_dict(name)
                if agent_cfg and agent_cfg != llm_config:
                    logger.info(f"Agent '{name}' using model: {agent_cfg.get('model')}")
            self._progress(name, "running", "开始分析...")
            try:
                result = await self.run_single(
                    skill_name, arguments, agent_role=name,
                    context=context, llm_config=agent_cfg, stream=True,
                )
                self._progress(name, "completed", "分析完成", 1.0)
                return name, result
            except Exception as e:
                self._progress(name, "failed", str(e))
                return name, f"[错误] {str(e)}"

        # Phase 1: research agents in batches
        results = []
        for i in range(0, len(research_agents), MAX_PARALLEL_AGENTS):
            batch = research_agents[i:i + MAX_PARALLEL_AGENTS]
            batch_tasks = [agent_task(name) for name in batch]
            try:
                batch_results = await asyncio.wait_for(
                    asyncio.gather(*batch_tasks),
                    timeout=AGENT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                batch_results = []
                for name in batch:
                    batch_results.append((name, f"[错误] Agent 超时 ({AGENT_TIMEOUT}s)"))
                    self._progress(name, "failed", "超时")
            results.extend(batch_results)

        agent_reports = ""
        success_count = 0
        failed_count = 0
        for name, result in results:
            if result.startswith("[错误]"):
                failed_count += 1
                continue
            success_count += 1
            role_label = get_role_label(name)
            # Strip tool-call noise: remove entire XML blocks + function-call artifacts
            import re as _strip_re
            _clean = _strip_re.sub(
                r'<\s*(?:｜｜DSML｜｜tool_calls|｜｜DSML｜｜invoke|｜｜DSML｜｜parameter|/｜｜DSML｜｜tool_calls|/｜｜DSML｜｜invoke|/｜｜DSML｜｜parameter)\s*[^>]*>',
                '', result, flags=_strip_re.DOTALL
            )
            # Also catch self-closing forms: <｜｜DSML｜｜invoke ... />
            _clean = _strip_re.sub(
                r'<\s*｜｜DSML｜｜(?:tool_calls|invoke|parameter)\b[^>]*?/>',
                '', _clean, flags=_strip_re.DOTALL
            )
            agent_reports += f"\n\n---\n## {role_label}\n\n{_clean}\n"

        if success_count == 0:
            return f"# AI Berkshire 投研报告\n\n**目标**：{arguments}\n\n## 错误\n\n所有 {failed_count} 个 Agent 均执行失败，无法生成报告。\n\n请检查 LLM API 配置和网络连接。"

        # Phase 2: Team Lead synthesis (streamed as agent="team-lead")
        # When per_agent_enabled, the Team Lead model comes from agent_models["team-lead"];
        # otherwise falls back to synthesis_config (or llm_config if no synthesis override).
        if per_agent_enabled and spec is not None:
            synthesis_config = spec.llm.to_agent_config_dict("team-lead")
            base_model = (llm_config or {}).get("model") or "(服务器默认)"
            tl_model = (synthesis_config or {}).get("model") or "(服务器默认)"
            if tl_model != base_model:
                logger.info(f"Team Lead (per-agent mode) 使用模型: {tl_model}（研究团队: {base_model}）")
            else:
                logger.info(f"Team Lead (per-agent mode) 使用全局模型: {tl_model}")
        else:
            synthesis_config = synthesis_llm_config if synthesis_llm_config is not None else llm_config
            base_model = (llm_config or {}).get("model") or "(服务器默认)"
            synthesis_model = (synthesis_config or {}).get("model") or "(服务器默认)"
            if synthesis_model != base_model:
                logger.info(f"Team Lead 综合使用独立模型: {synthesis_model}（研究团队模型: {base_model}）")
            else:
                logger.info(f"Team Lead 综合使用模型: {synthesis_model}")
        agent_label = f"{success_count} 位分析师"
        if failed_count > 0:
            agent_label += f"（{failed_count} 个 Agent 失败，已排除）"
        synthesis_prompt = TEAM_LEAD_PROMPT
        if context:
            synthesis_prompt += f"\n\n{context}"
        synthesis_prompt += f"\n\n以下是{agent_label}的独立研究报告：\n{agent_reports}"

        synthesis = ""
        async for chunk in chat_stream(
            synthesis_prompt,
            f"请综合以上报告，输出最终投研报告。目标：{arguments}",
            llm_config=synthesis_config,
        ):
            synthesis += chunk
            self._chunk(chunk, agent="team-lead")

        # Phase 3: post-synthesis agents (sequential, streamed)
        post_synthesis_output = ""
        if post_synthesis_agents:
            # Truncate synthesis to avoid token overflow for post-synthesis agents
            _MAX_PS_INPUT = 12000  # chars for synthesis passed to editor/reviewer
            _synth_trimmed = synthesis[-_MAX_PS_INPUT:] if len(synthesis) > _MAX_PS_INPUT else synthesis
            for agent_name in post_synthesis_agents:
                # Resolve per-agent LLM config for post-synthesis agents too
                psa_cfg = llm_config
                if per_agent_enabled and spec is not None:
                    psa_cfg = spec.llm.to_agent_config_dict(agent_name)
                self._progress(agent_name, "running", "处理中...")
                try:
                    role_prompt = AGENT_ROLE_PROMPTS.get(agent_name, "你是一位专业的财经编辑。")
                    # Only include minimal context for post-synthesis (time only, not full market data)
                    post_input = f"以下是投研团队的综合研究报告（已截取关键部分），请基于此进行你的工作：\n\n{_synth_trimmed}"
                    post_result = ""
                    async for chunk in chat_stream(role_prompt, post_input, llm_config=psa_cfg):
                        post_result += chunk
                        self._chunk(chunk, agent=agent_name)
                    post_synthesis_output += f"\n\n---\n## {agent_name}\n\n{post_result}\n"
                    self._progress(agent_name, "completed", "完成", 1.0)
                except Exception as e:
                    logger.error(f"Post-synthesis agent {agent_name} failed: {e}", exc_info=True)
                    _err_msg = str(e)[:200]
                    post_synthesis_output += f"\n\n---\n## {agent_name}\n\n[错误] {_err_msg}\n"
                    self._progress(agent_name, "failed", _err_msg)

        all_agent_count = len(agent_names)
        final_report = f"""# AI Berkshire 投研报告

**目标**：{arguments}
**框架**：{skill['display_name']}
**执行模式**：{success_count}/{all_agent_count} Agent 成功{'（' + str(failed_count) + ' 个失败）' if failed_count else ''}{'，分阶段执行' if post_synthesis_agents else ''}
**数据源**：实时市场数据 + 投资大师知识库

{synthesis}

---

## 附录：各Agent独立研究报告
{agent_reports}
"""

        if post_synthesis_output:
            final_report += f"\n---\n## 后期处理（编辑/评审）\n{post_synthesis_output}\n"

        if len(final_report) > MAX_REPORT_LENGTH:
            final_report = final_report[:MAX_REPORT_LENGTH] + "\n\n---\n*报告已截断（超过最大长度限制）*\n"

        return final_report

    # ---------- series ----------

    async def run_series(
        self,
        skill_name: str,
        arguments: str,
        llm_config: dict = None,
    ) -> str:
        """Run a series generation: multiple long-form articles sequentially."""
        skill = get_skill(skill_name)
        topics = skill.get("series_topics", [])

        if not topics:
            return await self.run_single(skill_name, arguments, llm_config=llm_config)

        bundle = await ContextBuilder(bus=self.bus, task_id=self.task_id).build(
            arguments, skill_name=skill_name,
        )
        context = bundle.context

        total = len(topics)
        articles = []

        for i, topic in enumerate(topics):
            self._progress(f"article-{i + 1}", "running", f"撰写：{topic}")

            context_str = ""
            if articles:
                prev_summaries = []
                for j, (t, a) in enumerate(articles):
                    excerpt = a[:200].replace("\n", " ") + "..."
                    prev_summaries.append(f"第{j + 1}篇《{t}》：{excerpt}")
                context_str = f"\n\n## 前文回顾\n" + "\n".join(prev_summaries)

            user_msg = f"""请撰写关于「{arguments}」的深度系列文章的第 {i + 1}/{total} 篇。

## 本篇主题
{topic}

## 写作要求
- 3000-5000字
- 数据翔实，来源明确
- 观点鲜明，不打太极
- 公众号级深度和可读性
- 与系列其他篇相互关联{context_str}

请直接输出文章正文，以 Markdown 格式排版。"""

            system_prompt = SERIES_SYSTEM_PROMPT
            if context:
                system_prompt += f"\n\n{context}"

            try:
                # Function-calling loop (non-streaming); emit whole article
                # as one chunk so subscribers see per-article output.
                article = await asyncio.wait_for(
                    chat_complete_with_tools(
                        system_prompt, user_msg,
                        tools=ALL_TOOL_SCHEMAS,
                        tool_executor=self._tool_executor(f"article-{i + 1}"),
                        llm_config=llm_config,
                    ),
                    timeout=AGENT_TIMEOUT,
                )
                articles.append((topic, article))
                self._chunk(article, agent=f"article-{i + 1}")
                self._progress(
                    f"article-{i + 1}", "completed",
                    f"第{i + 1}/{total}篇完成", (i + 1) / total,
                )
            except asyncio.TimeoutError:
                articles.append((topic, f"[错误] 第{i + 1}篇生成超时"))
                self._progress(f"article-{i + 1}", "failed", "超时")
            except Exception as e:
                articles.append((topic, f"[错误] {str(e)}"))
                self._progress(f"article-{i + 1}", "failed", str(e))

        success_count = sum(1 for _, a in articles if not a.startswith("[错误]"))
        failed_count = total - success_count

        report_parts = [
            f"# 深度系列长文：{arguments}\n",
            f"**框架**：{skill['display_name']}",
            f"**执行模式**：系列生成（{success_count}/{total} 篇成功{f'，{failed_count} 篇失败' if failed_count else ''}）",
            f"**数据源**：实时市场数据 + 投资大师知识库\n",
        ]

        for i, (topic, article) in enumerate(articles):
            report_parts.append(f"\n---\n\n## 第{i + 1}篇：{topic}\n")
            report_parts.append(article)
            report_parts.append("")

        if success_count > 0:
            report_parts.append("\n---\n\n## 系列总结\n")
            report_parts.append(f"以上 {success_count} 篇文章构成了关于「{arguments}」的完整深度分析系列。"
                                "每篇文章独立成文，同时相互关联，形成对该公司的全方位深度解读。")

        final_report = "\n".join(report_parts)

        if len(final_report) > MAX_SERIES_REPORT_LENGTH:
            final_report = final_report[:MAX_SERIES_REPORT_LENGTH] + "\n\n---\n*报告已截断（超过最大长度限制）*\n"

        return final_report


# ==================== Legacy public API (was agents/orchestrator.py) ====================

async def run_single_agent(
    skill_name: str,
    arguments: str,
    agent_role: str = None,
    context: str = "",
    llm_config: dict = None,
) -> str:
    """Non-streaming single agent (legacy signature preserved)."""
    runner = AgentRunner()
    return await runner.run_single(
        skill_name, arguments, agent_role=agent_role,
        context=context, llm_config=llm_config, stream=False,
    )


async def run_multi_agent(
    skill_name: str,
    arguments: str,
    progress_callback=None,
    llm_config: dict = None,
) -> str:
    """Legacy multi-agent entry; progress_callback bridged onto an EventBus."""
    bus = EventBus()
    runner = AgentRunner(bus=bus, task_id="legacy")
    pump = None
    if progress_callback is not None:
        queue = bus.subscribe("legacy")

        async def _pump():
            while True:
                ev = await queue.get()
                if ev.type == EventType.PROGRESS:
                    await progress_callback(AgentProgress(
                        agent_name=ev.payload.get("agent", ""),
                        status=ev.payload.get("status", ""),
                        message=ev.payload.get("message", ""),
                        progress=ev.payload.get("progress", 0.0),
                    ))

        pump = asyncio.create_task(_pump())
    try:
        return await runner.run_multi(skill_name, arguments, llm_config=llm_config)
    finally:
        if pump is not None:
            pump.cancel()


async def run_series(
    skill_name: str,
    arguments: str,
    progress_callback=None,
    llm_config: dict = None,
) -> str:
    """Legacy series entry; progress_callback bridged onto an EventBus."""
    bus = EventBus()
    runner = AgentRunner(bus=bus, task_id="legacy")
    pump = None
    if progress_callback is not None:
        queue = bus.subscribe("legacy")

        async def _pump():
            while True:
                ev = await queue.get()
                if ev.type == EventType.PROGRESS:
                    await progress_callback(AgentProgress(
                        agent_name=ev.payload.get("agent", ""),
                        status=ev.payload.get("status", ""),
                        message=ev.payload.get("message", ""),
                        progress=ev.payload.get("progress", 0.0),
                    ))

        pump = asyncio.create_task(_pump())
    try:
        return await runner.run_series(skill_name, arguments, llm_config=llm_config)
    finally:
        if pump is not None:
            pump.cancel()
