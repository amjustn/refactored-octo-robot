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
    """
    provider: str = "deepseek"
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None

    @classmethod
    def from_override(cls, override: Optional[dict]) -> "LLMConfig":
        cfg = override or {}
        return cls(
            model=(cfg.get("model") or "").strip() or None,
            base_url=(cfg.get("base_url") or "").strip() or None,
            api_key=(cfg.get("api_key") or "").strip() or None,
        )

    def to_llm_config_dict(self) -> Optional[dict]:
        """Convert back to the dict shape core.llm.* expects."""
        out = {}
        if self.api_key:
            out["api_key"] = self.api_key
        if self.base_url:
            out["base_url"] = self.base_url
        if self.model:
            out["model"] = self.model
        return out or None


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    skill_name: str
    arguments: str
    llm: LLMConfig = field(default_factory=LLMConfig)
    strategy: Literal["single", "multi", "series"] = "single"
    agent_names: tuple = ("default",)
    stream: bool = True
    caller: str = "ws"  # ws / http / eval

    @classmethod
    def build(
        cls,
        skill: dict,
        arguments: str,
        llm_override: Optional[dict] = None,
        caller: str = "ws",
        stream: bool = True,
        task_id: Optional[str] = None,
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
            task_id=task_id or str(uuid.uuid4())[:8],
            skill_name=skill["name"],
            arguments=arguments,
            llm=LLMConfig.from_override(llm_override),
            strategy=strategy,
            agent_names=agent_names,
            stream=stream,
            caller=caller,
        )
