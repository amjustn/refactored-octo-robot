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
import json
import logging
import time
from pathlib import Path
from typing import Optional

from ..core.config import AGENT_TIMEOUT, MAX_PARALLEL_AGENTS
from ..core.llm import chat_complete, chat_complete_with_tools, chat_stream, pop_tool_fallback
from ..models.schemas import AgentProgress
from ..skills import build_user_prompt, get_skill
from .context import ContextBuilder

# P3: decision log
from .decision_log import asave_decision as _asave_decision
from .events import EventBus, EventType
from .guards import MAX_SERIES_REPORT_LENGTH  # noqa: F401  # re-export (sole definition lives in guards)
from .persist import get_repository
from .prompts import (
    AGENT_ROLE_PROMPTS,
    SERIES_SYSTEM_PROMPT,
    TERMINOLOGY_RULE,
    get_default_system_prompt,
    get_role_label,
    get_synthesis_prompt,
)
from .spec import TaskSpec
from .tools import ALL_TOOL_SCHEMAS, TOOL_ENABLED_AGENTS, get_gateway
from ..tools.valuation_guard import annotate_report, check_report

logger = logging.getLogger("ai_berkshire.harness.runner")

# ── P1: soft loop limits ────────────────────────────────────────
MAX_SAME_TOOL_CALLS = 3  # per agent per tool name within one task

# ── P2: audit trail / scratchpad ─────────────────────────────────
SCRATCHPAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "scratchpad"

import re as _re

# 第2层防御：DSML 工具调用标记剥离，覆盖三类形态（幂等）：
#  1. 整块 <tool_calls>...</tool_calls>（含其间内容），开/闭标签均可带
#     全角 ｜｜DSML｜｜ 前缀，大小写不敏感；
#  2. 全角前缀标签（｜｜DSML｜｜invoke 等，含自闭合，历史行为保留）；
#  3. 孤立标签：无前缀或混用形态的 <invoke ...> / </invoke> /
#     <parameter ...> / </parameter> / <tool_calls> / </tool_calls> /
#     </｜｜DSML｜｜invoke> 等。这些标记不会出现在正常中文研报正文里。
_DSML_WHOLE_BLOCK_RE = _re.compile(
    r'<\s*(?:｜｜DSML｜｜)?\s*tool_calls\b.*?</\s*(?:｜｜DSML｜｜)?\s*tool_calls\s*>',
    _re.DOTALL | _re.IGNORECASE,
)
_DSML_BLOCK_RE = _re.compile(
    r'<\s*(?:｜｜DSML｜｜tool_calls|｜｜DSML｜｜invoke|｜｜DSML｜｜parameter|/｜｜DSML｜｜tool_calls|/｜｜DSML｜｜invoke|/｜｜DSML｜｜parameter)\s*[^>]*>',
    _re.DOTALL,
)
_DSML_SELF_RE = _re.compile(
    r'<\s*｜｜DSML｜｜(?:tool_calls|invoke|parameter)\b[^>]*?/>',
    _re.DOTALL,
)
_DSML_ORPHAN_RE = _re.compile(
    r'<\s*/?\s*(?:｜｜DSML｜｜)?\s*(?:tool_calls|invoke|parameter)\b[^>]*>',
    _re.IGNORECASE,
)


def _strip_dsml(text: str) -> str:
    """Remove DSML tool-call artifacts from an agent result (idempotent)."""
    text = _DSML_WHOLE_BLOCK_RE.sub("", text)
    text = _DSML_BLOCK_RE.sub("", text)
    text = _DSML_SELF_RE.sub("", text)
    return _DSML_ORPHAN_RE.sub("", text)


def _with_attachments(prompt: str, attachments: str) -> str:
    """Append the uploaded-reference section to an inline-built prompt.

    Mirrors the block build_user_prompt adds; attachments never enter
    context building, only agent-facing prompts.
    """
    if not attachments:
        return prompt
    return (
        f"{prompt}\n\n## 用户上传的参考资料\n{attachments}\n\n"
        "请将上述资料作为分析的重要依据。"
    )


class AllAgentsFailedError(RuntimeError):
    """Raised when every research agent in a multi-agent run failed."""


class AgentRunner:
    """Unified agent execution. All observable progress goes to the bus."""

    def __init__(self, bus: Optional[EventBus] = None, task_id: Optional[str] = None):
        self.bus = bus
        self.task_id = task_id or "adhoc"
        # P1: per-agent per-tool call counts — reset each run()
        self._tool_call_counts: dict[str, dict[str, int]] = {}
        # P2: scratchpad path — written once on first tool call
        self._scratchpad_path: Optional[Path] = None
        # Custom-model tool fallbacks — merged into guard_warnings by harness run()
        self.extra_warnings: list[dict] = []
        # E2: set by run_multi when every research agent failed — harness
        # turns the run into a hard failure instead of a "completed" report.
        self.fatal_error: Optional[str] = None
        # E3: goal-parser hints captured during context build; run() passes
        # the company name to save_decision as a trusted hint.
        self._last_goal_hints: Optional[dict] = None

    # ---------- event helpers ----------

    def _emit(self, type_: EventType, **payload):
        if self.bus is not None:
            self.bus.emit(self.task_id, type_, **payload)

    def _note_tool_fallback(self, agent_label: str):
        """Record + broadcast when a custom model couldn't function-call and
        the harness retried the agent with the server default model."""
        event = pop_tool_fallback()
        if not event:
            return
        detail = (
            f"Agent「{agent_label}」配置的模型 {event['from_model']} 未发起工具调用"
            f"（疑似不支持 function calling），已自动回退默认模型 {event['to_model']} 完成取数"
        )[:300]
        self.extra_warnings.append({"gate": "llm_fallback", "detail": detail})
        self._emit(EventType.GUARD_WARNING, gate="llm_fallback", detail=detail)

    def _progress(self, agent: str, status: str, message: str = "", progress: float = 0.0):
        self._emit(EventType.PROGRESS, agent=agent, status=status,
                   message=message, progress=progress)

    def _chunk(self, content: str, agent: Optional[str] = None):
        if agent:
            self._emit(EventType.CHUNK, agent=agent, content=content)
        else:
            self._emit(EventType.CHUNK, content=content)

    async def _save_artifact(self, agent: str, content: str):
        """Persist an intermediate agent result (non-fatal on failure).

        Saved for both successful and ``[错误]`` results so cancelled /
        failed runs can still surface completed work as a partial report.
        """
        try:
            await get_repository().asave_artifact(self.task_id, agent, content)
        except Exception as e:
            logger.warning(f"artifact save failed ({self.task_id}/{agent}): {e}")

    # ---------- dispatch ----------

    async def run(self, spec: TaskSpec, prefill_results: Optional[dict] = None) -> str:
        """Execute a TaskSpec; returns the final report text.

        prefill_results (resume flow): agent_name -> prior good artifact
        content; those research agents are skipped and their previous
        output is reused verbatim in the synthesis input.
        """
        # P1: reset tool call counters at the start of each run
        self._tool_call_counts.clear()
        self._scratchpad_path = None
        llm_config = spec.llm.to_llm_config_dict()

        report = ""
        if spec.strategy == "series":
            report = await self.run_series(
                spec.skill_name, spec.arguments,
                llm_config=llm_config, attachments=spec.attachments,
            )
        elif spec.strategy == "multi":
            report = await self.run_multi(
                spec.skill_name, spec.arguments,
                llm_config=llm_config,
                synthesis_llm_config=spec.llm.to_synthesis_config_dict(),
                spec=spec,
                attachments=spec.attachments,
                prefill_results=prefill_results,
            )
        else:
            report = await self.run_single(
                spec.skill_name, spec.arguments,
                llm_config=llm_config, stream=spec.stream,
                attachments=spec.attachments,
            )

        # ── 估值一致性校验(硬防线) ─────────────────────────────
        # 拦截 AI 口算导致的估值数字自相矛盾(如"9-10倍PE×EPS 8.2元=60-68元")。
        # 硬失败: 打回重写一次, 仍失败则报告顶部插入警告条; 软警告: 轻量提示条。
        # 非估值内容原样放行, 零开销。
        if report and not report.startswith("[错误]"):
            report = await self._apply_valuation_guard(report, llm_config)

        # P3: save decision to cross-session log (non-blocking, best-effort).
        # Skipped for error reports and for E2 fatal runs — a run where every
        # agent failed carries no trustworthy investment thesis.
        if report and not report.startswith("[错误]") and not self.fatal_error:
            await _asave_decision(
                spec.arguments, report,
                skill_name=spec.skill_name,
                task_id=self.task_id,
                company_hint=(self._last_goal_hints or {}).get("company") or "",
            )

        return report

    # ---------- 估值一致性校验 ----------

    async def _apply_valuation_guard(self, report: str, llm_config: Optional[dict] = None) -> str:
        """估值一致性校验入口: 硬失败→打回重写一次→仍失败→顶部标注警告。

        返回修正/标注后的报告文本; 非估值内容原样返回。
        """
        vg = check_report(report)
        if not vg["triggered"]:
            return report
        if vg["hard_fail"]:
            issues = "\n".join(f"- {c['detail']}" for c in vg["checks"] if c["severity"] == "hard")
            fixed = await self._rewrite_valuation_fixes(report, issues, llm_config)
            if fixed and fixed.strip():
                vg2 = check_report(fixed)
                if vg2["triggered"] and not vg2["hard_fail"]:
                    # 修正成功; 残留软警告则附轻量提示条
                    return annotate_report(fixed, vg2) if vg2["warnings"] else fixed
                return annotate_report(fixed, vg2)
            return annotate_report(report, vg)
        # 仅软警告
        return annotate_report(report, vg)

    async def _rewrite_valuation_fixes(self, report: str, issues: str, llm_config: Optional[dict] = None) -> str:
        """把错误清单交给模型做一次定点修正(仅数字); 失败返回空串。"""
        system = (
            "你是财经数据校对员。下面是一份研究报告, 其中包含几处估值数字自相矛盾。\n"
            "请只修正错误清单中列出的数字, 使计算自洽\n"
            "(例如: '9-10倍PE × EPS 8.2元' 的合理价值区间应为74-82元, 而非60-68元)。\n"
            "保持报告的其余内容、结构、格式、语气完全不变, 不要添加或删除段落。\n"
            "直接输出修正后的完整报告, 不要任何解释或前后缀。"
        )
        user = f"错误清单:\n{issues}\n\n报告原文:\n{report}"
        try:
            return await chat_complete(system, user, llm_config=llm_config or {}) or ""
        except Exception as e:
            logger.warning(f"valuation rewrite failed (task={self.task_id}): {e}")
            return ""

    # ---------- tool execution ----------

    async def _log_tool_call(self, agent: str, tool_name: str, args: dict, result: str):
        """P2: append one JSONL line to the scratchpad file (non-fatal)."""
        try:
            if self._scratchpad_path is None:
                SCRATCHPAD_DIR.mkdir(parents=True, exist_ok=True)
                self._scratchpad_path = SCRATCHPAD_DIR / f"{self.task_id}.jsonl"
            entry = json.dumps({
                "ts": time.time(),
                "agent": agent or "unknown",
                "tool": tool_name,
                "args": args,
                "result": result[:2000],  # truncate long results
                "task_id": self.task_id,
            }, ensure_ascii=False)
            with open(self._scratchpad_path, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass  # never let scratchpad failure block research

    def _tool_executor(self, agent: Optional[str] = None):
        """P1: function-calling tool executor with soft loop limits.
        Each agent can call the same tool at most MAX_SAME_TOOL_CALLS times.
        Exceeding returns a limit message instead of executing.
        P2: every call is logged to the scratchpad JSONL.

        Routed through the ToolGateway (circuit breaking, caching,
        timeout/retry, TOOL_CALL events)."""
        gateway = get_gateway()
        bus, task_id = self.bus, self.task_id
        runner = self

        # Ensure agent-level tracking dict exists
        agent_key = agent or "__single__"
        if agent_key not in runner._tool_call_counts:
            runner._tool_call_counts[agent_key] = {}

        async def _exec(name: str, args: dict) -> str:
            counts = runner._tool_call_counts[agent_key]
            current = counts.get(name, 0)

            # P1: soft loop limit check
            if current >= MAX_SAME_TOOL_CALLS:
                limit_msg = json.dumps({
                    "error": "TOOL_LOOP_LIMIT",
                    "message": f"已调用 {name} {current} 次，达到上限 {MAX_SAME_TOOL_CALLS} 次。"
                               f"请基于已有数据直接输出结论，不要再调用此工具。",
                }, ensure_ascii=False)
                # Log the limit hit to scratchpad
                await runner._log_tool_call(agent_key, f"{name}__LIMIT_HIT", {}, limit_msg)
                return limit_msg

            # Execute the actual tool
            result = await gateway.execute(name, args, task_id=task_id, agent=agent, bus=bus)
            counts[name] = current + 1

            # P2: log to scratchpad
            await runner._log_tool_call(agent_key, name, args, result)

            return result

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
        attachments: str = "",
    ) -> str:
        """Run a single agent analysis.

        If context is empty it is built automatically (all callers get
        proper context). Tool-enabled roles use function calling; with
        stream=True non-tool roles stream tokens as chunk events.
        """
        if agent_role and agent_role in AGENT_ROLE_PROMPTS:
            system_prompt = AGENT_ROLE_PROMPTS[agent_role]
            user_msg = _with_attachments(f"请分析以下目标：{arguments}", attachments)
        else:
            system_prompt = get_default_system_prompt(skill_name)
            user_msg = build_user_prompt(skill_name, arguments, attachments=attachments)

        if not context:
            bundle = await ContextBuilder(bus=self.bus, task_id=self.task_id).build(
                arguments, skill_name=skill_name, llm_config=llm_config,
            )
            context = bundle.context
            self._last_goal_hints = getattr(bundle, "goal_hints", None)

        if context:
            system_prompt = f"{system_prompt}\n\n{context}"

        # 术语注释规则: 所有报告统一挂载(首次出现的金融专业术语加≤30字注释)
        system_prompt = f"{system_prompt}\n\n{TERMINOLOGY_RULE}"

        # 技能级工具开关（第1层修复）：单Agent技能（agent_role 为 None）
        # 若在 registry 中声明 tools_enabled（提示词明确要求调用工具，如
        # daily-briefing），同样走 function-calling 循环；多Agent成员角色
        # 仍由 TOOL_ENABLED_AGENTS 覆盖，行为不变。
        skill_tools_enabled = (
            agent_role is None
            and bool((get_skill(skill_name) or {}).get("tools_enabled"))
        )
        if agent_role in TOOL_ENABLED_AGENTS or skill_tools_enabled:
            tool_label = agent_role or skill_name or "default"
            # Function-calling loop is non-streaming; emit whole result.
            result = await chat_complete_with_tools(
                system_prompt, user_msg,
                tools=ALL_TOOL_SCHEMAS,
                tool_executor=self._tool_executor(tool_label),
                llm_config=llm_config,
            )
            self._note_tool_fallback(tool_label)
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
        attachments: str = "",
        prefill_results: Optional[dict] = None,
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

        prefill_results：断点续跑。agent_name → 上一次运行保存的成功
        artifact；名单内在列的 Agent 直接复用旧成果（不重跑、不重存
        artifact），其余 Agent 照常执行。Team Lead 综合与
        post_synthesis Agent 始终重跑。
        """
        skill = get_skill(skill_name)
        agent_names = skill.get("agents", [])
        post_synthesis_agents = skill.get("post_synthesis_agents", [])

        if not agent_names:
            return await self.run_single(skill_name, arguments, llm_config=llm_config, attachments=attachments)

        self._progress("system", "running", "正在获取实时市场数据...")

        bundle = await ContextBuilder(bus=self.bus, task_id=self.task_id).build(
            arguments, skill_name=skill_name, llm_config=llm_config,
        )
        context = bundle.context
        self._last_goal_hints = getattr(bundle, "goal_hints", None)

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
            # 断点续跑：上一次已有成功成果的 Agent 直接复用，不再执行
            if prefill_results and name in prefill_results:
                self._progress(name, "completed", "复用上次成果（断点续跑）", 1.0)
                return name, prefill_results[name]
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
                    attachments=attachments,
                )
                self._progress(name, "completed", "分析完成", 1.0)
                return name, result
            except Exception as e:
                self._progress(name, "failed", str(e))
                return name, f"[错误] {str(e)}"

        # Phase 1: all research agents scheduled at once behind a semaphore;
        # each agent gets its own timeout so one slow/failed agent never
        # discards its siblings' results. Result order follows the roster
        # regardless of completion order (appendix ordering contract).
        sem = asyncio.Semaphore(MAX_PARALLEL_AGENTS)

        async def _run_agent(name: str) -> tuple[str, str]:
            # 续跑复用的 Agent：直接返回旧成果，不重存 artifact
            if prefill_results and name in prefill_results:
                return await agent_task(name)
            async with sem:
                try:
                    name, result = await asyncio.wait_for(agent_task(name), timeout=AGENT_TIMEOUT)
                except asyncio.TimeoutError:
                    self._progress(name, "failed", "超时")
                    result = f"[错误] Agent 超时 ({AGENT_TIMEOUT}s)"
            # Strip DSML noise BEFORE persisting so partial reports are clean.
            clean = result if result.startswith("[错误]") else _strip_dsml(result)
            await self._save_artifact(name, clean)
            return name, clean

        gathered = await asyncio.gather(
            *(_run_agent(name) for name in research_agents),
            return_exceptions=True,
        )
        results = []
        for name, res in zip(research_agents, gathered):
            if isinstance(res, BaseException):
                # Unreachable in practice (agent_task catches Exception and
                # timeouts are handled above) — keep siblings safe anyway.
                results.append((name, f"[错误] {res}"))
            else:
                results.append(res)

        agent_reports = ""
        success_count = 0
        failed_count = 0
        failed_details: list[tuple[str, str]] = []
        for name, result in results:
            if result.startswith("[错误]"):
                failed_count += 1
                failed_details.append((name, result))
                continue
            success_count += 1
            role_label = get_role_label(name)
            # Strip tool-call noise via the shared helper (same as artifact path)
            agent_reports += f"\n\n---\n## {role_label}\n\n{_strip_dsml(result)}\n"

        if success_count == 0:
            # E2: every research agent failed — flag a hard failure so harness
            # marks the task failed (the generic exception path still persists
            # a partial report built from the error artifacts).
            self.fatal_error = f"所有 {failed_count} 个 Agent 均执行失败"
            return f"# AI Berkshire 投研报告\n\n**目标**：{arguments}\n\n## 错误\n\n所有 {failed_count} 个 Agent 均执行失败，无法生成报告。\n\n请检查 LLM API 配置和网络连接。"

        # ── P5: Phase 1.5 — Agent peer debate ──────────────────
        # Each successful agent reviews another's work and provides:
        # agree/disagree + specific challenge points + confidence vote.
        # Only triggered when ≥2 agents succeed.
        debate_output = ""
        if success_count >= 2:
            self._progress("system", "running", "Agent 交叉辩论中...")
            success_results = [(n, r) for n, r in results if not r.startswith("[错误]")]

            async def _debate_one(idx: int) -> tuple[str, str]:
                """Agent idx reviews agent (idx+1)%n's work."""
                reviewer_name, _ = success_results[idx]
                target_idx = (idx + 1) % len(success_results)
                target_name, target_report = success_results[target_idx]
                target_role = get_role_label(target_name)
                reviewer_role = get_role_label(reviewer_name)

                # Truncate target report to avoid token issues
                _max_debate_input = 3000
                _short = target_report[:_max_debate_input] if len(target_report) > _max_debate_input else target_report

                debate_prompt = (
                    f"你是{reviewer_role}。以下是{target_role}的分析报告（已截取关键部分）。\n\n"
                    f"## {target_role}的报告\n{_short}\n\n"
                    "请从你的专业角度审视这份报告，完成以下三项：\n"
                    "1. **同意/反对**：你同意还是反对其核心结论？（一句话）\n"
                    "2. **质疑点**：列出1-2个最关键的质疑或补充（具体、可操作）\n"
                    "3. **投票**：BUY / HOLD / SELL（基于这份报告的逻辑，不是你的个人观点）\n\n"
                    "请用中文回复，保持简洁。"
                )

                # Use reviewer's own LLM config for debate
                dcfg = llm_config
                if per_agent_enabled and spec is not None:
                    dcfg = spec.llm.to_agent_config_dict(reviewer_name)

                try:
                    review_text = await asyncio.wait_for(
                        chat_complete(debate_prompt, "请给出你的交叉评审意见。", llm_config=dcfg),
                        timeout=60,  # shorter timeout for debate
                    )
                    return f"{reviewer_name}→{target_name}", review_text[:800]
                except Exception as e:
                    logger.warning(f"Debate {reviewer_name}→{target_name} failed: {e}")
                    return f"{reviewer_name}→{target_name}", f"[评审失败: {e}]"

            debates = await asyncio.gather(
                *(_debate_one(i) for i in range(len(success_results))),
                return_exceptions=True,
            )

            for item in debates:
                if isinstance(item, Exception):
                    continue
                pair, text = item
                if text.startswith("[评审失败"):
                    # Never feed a failed review into the synthesis prompt as
                    # if it were a real peer review — the Team Lead would read
                    # it as an actual challenge. Failures stay in logs/progress
                    # only (Cumora lesson: failures must be explicit, never
                    # disguised as content).
                    logger.warning(f"Debate {pair} failed, excluded from synthesis input")
                    continue
                debate_output += f"\n\n### 交叉评审：{pair}\n{text}\n"

            self._progress("system", "completed", f"{len(success_results)} 位 Agent 交叉辩论完成", 1.0)

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
        synthesis_prompt = get_synthesis_prompt(skill_name)
        if context:
            synthesis_prompt += f"\n\n{context}"
        synthesis_prompt += f"\n\n{TERMINOLOGY_RULE}"
        synthesis_prompt += f"\n\n以下是{agent_label}的独立研究报告：\n{agent_reports}"
        # P5: inject debate results
        if debate_output:
            synthesis_prompt += f"\n\n## Agent 交叉辩论记录\n以下是各分析师交叉评审对方报告的结果，请在综合时重点考虑质疑和反对意见：{debate_output}"

        async def _synthesize(cfg="__default__") -> str:
            text = ""
            async for chunk in chat_stream(
                synthesis_prompt,
                _with_attachments(f"请综合以上报告，输出最终投研报告。目标：{arguments}", attachments),
                llm_config=synthesis_config if cfg == "__default__" else cfg,
            ):
                text += chunk
                self._chunk(chunk, agent="team-lead")
            return text

        try:
            synthesis = await asyncio.wait_for(_synthesize(), timeout=AGENT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"Team Lead synthesis timed out after {AGENT_TIMEOUT}s")
            synthesis = f"[错误] 综合超时（超过 {AGENT_TIMEOUT}s），请直接参考附录中的各 Agent 独立研究报告。"
            self._progress("team-lead", "failed", "综合超时")
        except Exception as e:
            # E1: non-timeout synthesis failure (typically a broken custom
            # synthesis model).  Retry once with the server default model;
            # if that also fails, degrade to an appendix-only report with an
            # explicit warning instead of losing the whole run.
            logger.error(f"Team Lead synthesis failed: {e}", exc_info=True)
            synthesis = None
            if synthesis_config is not None:
                try:
                    logger.warning("Retrying synthesis with the server default model")
                    synthesis = await asyncio.wait_for(_synthesize(None), timeout=AGENT_TIMEOUT)
                    self.extra_warnings.append({
                        "gate": "synthesis_model_fallback",
                        "detail": f"综合环节配置的模型调用失败（{str(e)[:100]}），已回退服务器默认模型完成综合"[:300],
                    })
                except Exception as e2:
                    logger.error(f"Synthesis retry with default model failed: {e2}", exc_info=True)
                    synthesis = None
            if synthesis is None:
                synthesis = (f"[错误] 综合环节失败（{str(e)[:150]}），本报告为无综合降级版本，"
                             "请直接参考附录中的各 Agent 独立研究报告。")
                self.extra_warnings.append({
                    "gate": "synthesis_degraded",
                    "detail": f"综合环节失败，报告降级为各 Agent 原始报告附录：{str(e)[:200]}"[:300],
                })
                self._progress("team-lead", "failed", "综合失败，已降级")

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
                post_result = ""
                try:
                    role_prompt = AGENT_ROLE_PROMPTS.get(agent_name, "你是一位专业的财经编辑。")
                    # Only include minimal context for post-synthesis (time only, not full market data)
                    post_input = f"以下是投研团队的综合研究报告（已截取关键部分），请基于此进行你的工作：\n\n{_synth_trimmed}"

                    async def _run_post() -> str:
                        text = ""
                        async for chunk in chat_stream(role_prompt, post_input, llm_config=psa_cfg):
                            text += chunk
                            self._chunk(chunk, agent=agent_name)
                        return text

                    post_result = await asyncio.wait_for(_run_post(), timeout=AGENT_TIMEOUT)
                    post_synthesis_output += f"\n\n---\n## {agent_name}\n\n{post_result}\n"
                    self._progress(agent_name, "completed", "完成", 1.0)
                except asyncio.TimeoutError:
                    # Post-synthesis failure must never kill the completed synthesis
                    logger.error(f"Post-synthesis agent {agent_name} timed out after {AGENT_TIMEOUT}s")
                    post_result = f"[错误] 超时（超过 {AGENT_TIMEOUT}s）"
                    post_synthesis_output += f"\n\n---\n## {agent_name}\n\n{post_result}\n"
                    self._progress(agent_name, "failed", "超时")
                except Exception as e:
                    logger.error(f"Post-synthesis agent {agent_name} failed: {e}", exc_info=True)
                    _err_msg = str(e)[:200]
                    post_result = f"[错误] {_err_msg}"
                    post_synthesis_output += f"\n\n---\n## {agent_name}\n\n{post_result}\n"
                    self._progress(agent_name, "failed", _err_msg)
                await self._save_artifact(agent_name, post_result)

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

        # Failure attribution: which agents failed and why must be visible in
        # the deliverable itself, not just the progress UI — the report may be
        # exported/sent to WeChat where the per-agent progress is long gone
        # (Cumora lesson: failures surface in the artifact, never silent).
        if failed_details:
            final_report += "\n---\n\n## ⚠ 部分 Agent 执行失败\n\n"
            for name, err in failed_details:
                role_label = get_role_label(name)
                _reason = err[:200]
                final_report += f"- **{role_label}**（{name}）：{_reason}\n"

        # Length truncation is owned solely by OutputGuard (harness run flow).
        return final_report

    # ---------- series ----------

    async def run_series(
        self,
        skill_name: str,
        arguments: str,
        llm_config: dict = None,
        attachments: str = "",
    ) -> str:
        """Run a series generation: multiple long-form articles sequentially."""
        skill = get_skill(skill_name)
        topics = skill.get("series_topics", [])

        if not topics:
            return await self.run_single(skill_name, arguments, llm_config=llm_config, attachments=attachments)

        bundle = await ContextBuilder(bus=self.bus, task_id=self.task_id).build(
            arguments, skill_name=skill_name, llm_config=llm_config,
        )
        context = bundle.context
        self._last_goal_hints = getattr(bundle, "goal_hints", None)

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
                context_str = "\n\n## 前文回顾\n" + "\n".join(prev_summaries)

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

            user_msg = _with_attachments(user_msg, attachments)

            system_prompt = SERIES_SYSTEM_PROMPT
            if context:
                system_prompt += f"\n\n{context}"
            system_prompt += f"\n\n{TERMINOLOGY_RULE}"

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
                self._note_tool_fallback(f"article-{i + 1}")
                articles.append((topic, article))
                self._chunk(article, agent=f"article-{i + 1}")
                self._progress(
                    f"article-{i + 1}", "completed",
                    f"第{i + 1}/{total}篇完成", (i + 1) / total,
                )
            except asyncio.TimeoutError:
                article = f"[错误] 第{i + 1}篇生成超时"
                articles.append((topic, article))
                self._progress(f"article-{i + 1}", "failed", "超时")
            except Exception as e:
                article = f"[错误] {str(e)}"
                articles.append((topic, article))
                self._progress(f"article-{i + 1}", "failed", str(e))
            await self._save_artifact(topic, article)

        success_count = sum(1 for _, a in articles if not a.startswith("[错误]"))
        failed_count = total - success_count

        report_parts = [
            f"# 深度系列长文：{arguments}\n",
            f"**框架**：{skill['display_name']}",
            f"**执行模式**：系列生成（{success_count}/{total} 篇成功{f'，{failed_count} 篇失败' if failed_count else ''}）",
            "**数据源**：实时市场数据 + 投资大师知识库\n",
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

        # Length truncation is owned solely by OutputGuard (harness run flow).
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
