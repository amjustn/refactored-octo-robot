"""Harness task specification — immutable definitions.

TaskSpec is the single input contract between the HTTP/WS translation layer
(main.py) and the harness execution layer. Strategy and agent roster are
derived from skill metadata (is_multi_agent / series_mode), so adding a
skill never requires harness code changes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass(frozen=True)
class LLMConfig:
    """Normalized per-request LLM override.

    None fields mean "use the server default" (resolved downstream by
    core.llm.resolve_llm_config).

    synthesis_model：多Agent任务中 Team Lead 综合环节可选的独立模型
    （典型场景：研究 Agent 用 flash 省钱、综合环节用 pro 提质）。
    为空时全团队共用 model，行为与旧版完全一致。

    per_agent_enabled / agent_models：按Agent分配独立LLM配置（可选功能）。
    仅 per_agent_enabled=True 时生效。agent_models 是 agent_name→config
    的映射，每个 config 可包含 {model, base_url?, api_key?}。未出现在
    映射中的 Agent 回退到全局配置。

    agent_models 同时支持旧版 string 值（仅 model 名）和新版 dict 值，
    向前兼容。
    """
    provider: str = "deepseek"
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    synthesis_model: Optional[str] = None
    per_agent_enabled: bool = False
    agent_models: dict[str, dict] = field(default_factory=dict)

    @staticmethod
    def _normalize_agent_value(v) -> Optional[dict]:
        """Normalize a single agent_models value to {model, base_url?, api_key?}."""
        if isinstance(v, str):
            v = v.strip()
            return {"model": v} if v else None
        if isinstance(v, dict):
            out = {}
            m = (v.get("model") or "").strip()[:300]
            if m:
                out["model"] = m
            else:
                return None  # no model = skip
            for key in ("base_url", "api_key"):
                val = (v.get(key) or "").strip()[:500]
                if val:
                    out[key] = val
            return out
        return None

    @classmethod
    def from_override(cls, override: Optional[dict]) -> "LLMConfig":
        cfg = override or {}
        raw = cfg.get("agent_models") or {}
        agent_models = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                name = str(k).strip()[:80]
                if not name:
                    continue
                norm = cls._normalize_agent_value(v)
                if norm:
                    agent_models[name] = norm
        return cls(
            model=(cfg.get("model") or "").strip() or None,
            base_url=(cfg.get("base_url") or "").strip() or None,
            api_key=(cfg.get("api_key") or "").strip() or None,
            synthesis_model=(cfg.get("synthesis_model") or "").strip() or None,
            per_agent_enabled=bool(cfg.get("per_agent_enabled", False)),
            agent_models=agent_models,
        )

    def to_llm_config_dict(self) -> Optional[dict]:
        """Convert back to the dict shape core.llm.* expects.

        输出不含 synthesis_model / per_agent_enabled / agent_models
        —— 现有调用方（研究 Agent 等）形状不变。
        """
        out = {}
        if self.api_key:
            out["api_key"] = self.api_key
        if self.base_url:
            out["base_url"] = self.base_url
        if self.model:
            out["model"] = self.model
        return out or None

    def to_agent_config_dict(self, agent_name: str) -> Optional[dict]:
        """Per-agent LLM override dict (for multi-agent mode).

        When per_agent_enabled is True and agent_name is in agent_models,
        merges the agent's config with global config (agent overrides
        take priority). Falls back to to_llm_config_dict() otherwise.
        """
        base = self.to_llm_config_dict()
        if not self.per_agent_enabled:
            return base
        agent_cfg = self.agent_models.get(agent_name)
        if not agent_cfg:
            return base
        # Merge: agent config overrides global for model/base_url/api_key
        out = dict(base or {})
        out.update(agent_cfg)
        return out

    def to_storage_dict(self) -> dict:
        """脱敏序列化：供 tasks 表落库、resume 沿用原任务配置。

        绝不包含 api_key（顶层与 agent_models 内都没有）；空配置返回 {}。
        """
        out: dict = {}
        if self.model:
            out["model"] = self.model
        if self.base_url:
            out["base_url"] = self.base_url
        if self.synthesis_model:
            out["synthesis_model"] = self.synthesis_model
        if self.per_agent_enabled:
            out["per_agent_enabled"] = True
        agents = {}
        for name, cfg in self.agent_models.items():
            sub = {}
            if cfg.get("model"):
                sub["model"] = cfg["model"]
            if cfg.get("base_url"):
                sub["base_url"] = cfg["base_url"]
            # api_key 显式不序列化
            if sub:
                agents[name] = sub
        if agents:
            out["agent_models"] = agents
        return out

    def has_any_agent_override(self) -> bool:
        """True when per-agent model is enabled and at least one agent has a
        config that differs from the global model."""
        if not self.per_agent_enabled or not self.agent_models:
            return False
        for cfg in self.agent_models.values():
            if cfg.get("model") != self.model:
                return True
        return False

    def to_synthesis_config_dict(self) -> Optional[dict]:
        """Team Lead 综合环节的 LLM override dict：model 替换为 synthesis_model。

        synthesis_model 为空、或与 base model 相同时直接返回 base dict
        （零行为变化）；仅设置 synthesis_model 而未带 key/url 时沿用
        服务器默认连接（与 base dict 的规则一致）。
        """
        base = self.to_llm_config_dict()
        if not self.synthesis_model or self.synthesis_model == self.model:
            return base
        out = dict(base or {})
        out["model"] = self.synthesis_model
        return out


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    skill_name: str
    arguments: str
    attachments: str = ""               # uploaded reference text; never merged into arguments
    llm: LLMConfig = field(default_factory=LLMConfig)
    strategy: Literal["single", "multi", "series"] = "single"
    agent_names: tuple = ("default",)
    stream: bool = True
    caller: str = "ws"  # ws / http / eval
    # HITL：multi 策略辩论完成后暂停等待人工确认（默认关闭；
    # 前端 WS 启动消息 debate_confirm=true 时开启）。
    require_debate_confirm: bool = False

    @classmethod
    def build(
        cls,
        skill: dict,
        arguments: str,
        llm_override: Optional[dict] = None,
        caller: str = "ws",
        stream: bool = True,
        task_id: Optional[str] = None,
        attachments: str = "",
        debate_confirm: bool = False,
    ) -> "TaskSpec":
        """Derive strategy + agent roster from skill metadata."""
        is_multi = skill.get("is_multi_agent", False)
        is_series = skill.get("series_mode", False)

        if is_series:
            strategy = "series"
            agent_names = tuple(
                f"article-{i + 1}" for i in range(len(skill.get("series_topics", [])))
            )
        elif is_multi:
            strategy = "multi"
            agent_names = tuple(skill.get("agents", []))
        else:
            strategy = "single"
            agent_names = ("default",)

        return cls(
            task_id=task_id or uuid.uuid4().hex[:12],
            skill_name=skill["name"],
            arguments=arguments,
            attachments=attachments or "",
            llm=LLMConfig.from_override(llm_override),
            strategy=strategy,
            agent_names=agent_names,
            stream=stream,
            caller=caller,
            require_debate_confirm=bool(debate_confirm),
        )
