"""Harness repository — wraps TaskStore (API unchanged) and owns billing.

- Report saving (markdown + .meta.json sidecar) lives here.
- tasks table gains billing columns via an idempotent startup migration
  (PRAGMA table_info check, then ALTER TABLE only for missing columns).
  Old rows default to 0 / '[]'.
- Cost is computed from a per-model price list (constants below, adjust
  to match the actual provider bill).
- Sync methods do blocking sqlite/file I/O; async callers (harness run
  loop, agent runner) must use the ``a*`` wrappers, which offload to a
  thread via ``asyncio.to_thread`` so the event loop is never stalled.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.config import REPORTS_DIR
from ..core.task_store import TaskStore
from ..models.schemas import ResearchStatus

logger = logging.getLogger("ai_berkshire.harness.persist")

# 元 / 百万 tokens。2026-08 按各厂公开价目估算，可按实际账单调整。
# 查找走前缀匹配（最长前缀优先）：新模型只要前缀命中即自动套用，
# 未识别的模型记 0 并在计价结果里标 priced=False（见 compute_cost_detail）。
MODEL_PRICING = {
    # DeepSeek (api.deepseek.com)
    "deepseek-v4-flash": {"prompt": 2.0, "completion": 8.0},
    "deepseek-v4-pro": {"prompt": 4.0, "completion": 16.0},
    "deepseek": {"prompt": 2.0, "completion": 8.0},
    # Kimi / Moonshot (api.moonshot.cn)
    "kimi-k2.7-code-highspeed": {"prompt": 4.0, "completion": 16.0},
    "kimi-k2.7-code": {"prompt": 2.0, "completion": 8.0},
    "kimi-k2-thinking": {"prompt": 2.0, "completion": 8.0},
    "kimi-k2.6": {"prompt": 2.0, "completion": 8.0},
    "kimi-k2.5": {"prompt": 2.0, "completion": 8.0},
    "kimi-k2": {"prompt": 2.0, "completion": 8.0},
    "kimi-k3": {"prompt": 4.0, "completion": 16.0},
    "kimi": {"prompt": 2.0, "completion": 8.0},
    # Zhipu / GLM (open.bigmodel.cn)
    "glm-4.5-air": {"prompt": 1.0, "completion": 4.0},
    "glm-4.5": {"prompt": 2.0, "completion": 8.0},
    "glm-4.6": {"prompt": 2.0, "completion": 8.0},
    "glm-4.7": {"prompt": 2.0, "completion": 8.0},
    "glm-4v": {"prompt": 2.0, "completion": 8.0},
    "glm-5-turbo": {"prompt": 4.0, "completion": 16.0},
    "glm-5.1": {"prompt": 4.0, "completion": 16.0},
    "glm-5.2": {"prompt": 4.0, "completion": 16.0},
    "glm-5": {"prompt": 4.0, "completion": 16.0},
    "glm": {"prompt": 2.0, "completion": 8.0},
    # OpenAI (api.openai.com)
    "gpt-5.6": {"prompt": 10.0, "completion": 40.0},
    "gpt-5.5": {"prompt": 8.0, "completion": 32.0},
    "gpt-5.4": {"prompt": 6.0, "completion": 24.0},
    "gpt-5.1": {"prompt": 5.0, "completion": 20.0},
    "gpt-5": {"prompt": 5.0, "completion": 20.0},
    "gpt-4o": {"prompt": 5.0, "completion": 15.0},
    "gpt-4": {"prompt": 5.0, "completion": 15.0},
    "o4-mini": {"prompt": 4.0, "completion": 16.0},
    "o4": {"prompt": 4.0, "completion": 16.0},
    # Anthropic (api.anthropic.com)
    "claude-opus-5": {"prompt": 30.0, "completion": 120.0},
    "claude-opus-4": {"prompt": 15.0, "completion": 75.0},
    "claude-sonnet-5": {"prompt": 6.0, "completion": 24.0},
    "claude-sonnet-4": {"prompt": 3.0, "completion": 15.0},
    "claude": {"prompt": 6.0, "completion": 24.0},
    # Google (generativelanguage.googleapis.com)
    "gemini-3.6-flash": {"prompt": 1.0, "completion": 4.0},
    "gemini-3.5-flash": {"prompt": 1.0, "completion": 4.0},
    "gemini-2.5-pro": {"prompt": 4.0, "completion": 16.0},
    "gemini-2.5-flash": {"prompt": 1.0, "completion": 4.0},
    "gemini-2.0-flash": {"prompt": 1.0, "completion": 4.0},
    "gemini": {"prompt": 1.0, "completion": 4.0},
    # 阿里通义 (dashscope.aliyuncs.com)
    "qwen3-max": {"prompt": 4.0, "completion": 16.0},
    "qwen3-plus": {"prompt": 2.0, "completion": 8.0},
    "qwen3-turbo": {"prompt": 1.0, "completion": 4.0},
    "qwen-vl-max": {"prompt": 2.0, "completion": 8.0},
    "qwen-vl-plus": {"prompt": 1.0, "completion": 4.0},
    "qwen-vl": {"prompt": 1.0, "completion": 4.0},
    "qwen": {"prompt": 2.0, "completion": 8.0},
    # 百度文心 (qianfan.baidubce.com)
    "ernie-speed": {"prompt": 0.5, "completion": 2.0},
    "ernie-lite": {"prompt": 0.5, "completion": 2.0},
    "ernie": {"prompt": 2.0, "completion": 8.0},
    # 腾讯混元 (api.hunyuan.cloud.tencent.com)
    "hunyuan-lite": {"prompt": 0.5, "completion": 2.0},
    "hunyuan-a13b": {"prompt": 1.0, "completion": 4.0},
    "hunyuan": {"prompt": 2.0, "completion": 8.0},
    # 字节豆包 (ark.cn-beijing.volces.com)
    "doubao-1.5-lite": {"prompt": 0.5, "completion": 2.0},
    "doubao": {"prompt": 2.0, "completion": 8.0},
    # 兜底：未识别模型的粗略估算（仅兼容旧 compute_cost_yuan 契约）
    "default": {"prompt": 2.0, "completion": 8.0},
}

# 前缀匹配表：按前缀长度降序，保证 "kimi-k2.7-code-highspeed" 先于 "kimi-k2.7-code" 命中
_PRICING_PREFIXES = sorted(
    (k for k in MODEL_PRICING if k != "default"), key=len, reverse=True
)


def _price_for_model(model: Optional[str]) -> Optional[dict]:
    """前缀匹配价目（最长前缀优先）。

    Ollama 本地模型（"name:tag" 形式，如 qwen3:14b / deepseek-r1:8b）视为免费，
    返回 0 价目（priced=True）；未识别的模型返回 None（调用方记 0 + priced=False）。
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    if ":" in name:
        # 本地 Ollama 模型（name:tag）：不计费
        return {"prompt": 0.0, "completion": 0.0}
    for prefix in _PRICING_PREFIXES:
        if name.startswith(prefix):
            return MODEL_PRICING[prefix]
    return None


def compute_cost_detail(usage: dict, model: Optional[str] = None) -> dict:
    """按模型计价：{"cost_yuan", "priced"}。未识别模型记 0 且 priced=False。"""
    price = _price_for_model(model)
    if price is None:
        return {"cost_yuan": 0.0, "priced": False}
    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    cost = prompt * price["prompt"] / 1e6 + completion * price["completion"] / 1e6
    return {"cost_yuan": round(cost, 4), "priced": True}


def compute_cost_yuan(usage: dict, model: Optional[str] = None) -> float:
    """Compute cost in CNY from a token-usage dict and the price list.

    兼容旧契约：未识别模型回落 default 价目做粗略估算。
    新代码请用 compute_cost_detail（未识别记 0 + priced=False）。
    """
    detail = compute_cost_detail(usage, model)
    if detail["priced"]:
        return detail["cost_yuan"]
    price = MODEL_PRICING["default"]
    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    cost = prompt * price["prompt"] / 1e6 + completion * price["completion"] / 1e6
    return round(cost, 4)


def compute_usage_breakdown(usage: dict, default_model: Optional[str] = None) -> list:
    """把累计 usage 按 per-model 明细分别计价。

    usage 携带 by_model 子表（core.llm._record_usage 按模型归集）时逐模型
    计价求和；没有明细的旧调用方以 default_model 汇总为单行。
    返回 [{model, prompt_tokens, completion_tokens, cost_yuan, priced}]，费用降序。
    """
    rows = []
    by_model = usage.get("by_model") or {}
    if by_model:
        items = by_model.items()
    else:
        items = [(default_model or "unknown", usage)]
    for mdl, u in items:
        detail = compute_cost_detail(u, mdl)
        rows.append({
            "model": (mdl or "unknown")[:300],
            "prompt_tokens": u.get("prompt_tokens", 0) or 0,
            "completion_tokens": u.get("completion_tokens", 0) or 0,
            "cost_yuan": detail["cost_yuan"],
            "priced": detail["priced"],
        })
    rows.sort(key=lambda r: r["cost_yuan"], reverse=True)
    return rows


def sanitize_llm_config_for_storage(cfg: Optional[dict]) -> dict:
    """脱敏 per-request LLM 配置用于落库：剥掉一切 api_key（顶层与
    agent_models 内的独立 key 都绝不落库），保留 model/base_url/
    synthesis_model/per_agent_enabled/agent_models，供 resume 沿用。"""
    if not isinstance(cfg, dict):
        return {}
    out = {}
    for key in ("model", "base_url", "synthesis_model"):
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()[:300]
    if cfg.get("per_agent_enabled"):
        out["per_agent_enabled"] = True
    raw_agents = cfg.get("agent_models")
    if isinstance(raw_agents, dict) and raw_agents:
        agents = {}
        for name, val in raw_agents.items():
            if isinstance(val, str):
                if val.strip():
                    agents[str(name).strip()[:80]] = val.strip()[:300]
            elif isinstance(val, dict):
                sub = {}
                m = (val.get("model") or "").strip()[:300]
                if m:
                    sub["model"] = m
                bu = (val.get("base_url") or "").strip()[:500]
                if bu:
                    sub["base_url"] = bu
                # api_key 显式剔除
                if sub:
                    agents[str(name).strip()[:80]] = sub
        if agents:
            out["agent_models"] = agents
    return out


def merge_usage_breakdowns(old: list, new: list) -> list:
    """累加两份 per-model 明细（resume 续跑计费合并用），费用降序。"""
    merged: dict[str, dict] = {}
    for row in (old or []) + (new or []):
        key = row.get("model") or "unknown"
        slot = merged.setdefault(key, {
            "model": key, "prompt_tokens": 0, "completion_tokens": 0,
            "cost_yuan": 0.0, "priced": bool(row.get("priced")),
        })
        slot["prompt_tokens"] += row.get("prompt_tokens", 0) or 0
        slot["completion_tokens"] += row.get("completion_tokens", 0) or 0
        slot["cost_yuan"] = round(slot["cost_yuan"] + (row.get("cost_yuan", 0) or 0), 4)
        slot["priced"] = slot["priced"] and bool(row.get("priced", True))
    out = list(merged.values())
    out.sort(key=lambda r: r["cost_yuan"], reverse=True)
    return out


# Billing columns added on top of the legacy tasks schema.
_NEW_COLUMNS = {
    "tokens_prompt": "tokens_prompt INTEGER DEFAULT 0",
    "tokens_completion": "tokens_completion INTEGER DEFAULT 0",
    "cost_yuan": "cost_yuan REAL DEFAULT 0",
    "duration_s": "duration_s REAL",
    "guard_warnings": "guard_warnings TEXT DEFAULT '[]'",
    "model": "model TEXT DEFAULT ''",
    # per-model 用量/计价明细（JSON list，见 compute_usage_breakdown）
    "usage_json": "usage_json TEXT DEFAULT ''",
    # 脱敏后的 per-agent LLM 配置（绝不落 api_key），resume 默认沿用
    "llm_config_json": "llm_config_json TEXT DEFAULT ''",
}


def _one_line_str(v) -> str:
    """把任意值压成单行字符串 —— meta 字段不允许换行，否则列表渲染会错位。"""
    return " ".join(str(v or "").split())


def _dominant_model(tokens: Optional[dict]) -> str:
    """从 tokens.by_model 推断本次报告的主模型（meta 的"模型版本"字段）。

    单模型 → 模型名；多模型（per-agent 混用）→ `mixed(a,b)`；无信息 → ''。
    """
    by_model = (tokens or {}).get("by_model") or {}
    if not isinstance(by_model, dict) or not by_model:
        return ""
    names = sorted(str(k) for k in by_model.keys())
    if len(names) == 1:
        return names[0][:80]
    return ("mixed(" + ",".join(names) + ")")[:80]


def _sanitize_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", name)
    return name or "report"


class Repository:
    """Wraps TaskStore; adds report persistence + billing columns."""

    def __init__(self, store: Optional[TaskStore] = None):
        self.store = store or TaskStore()
        self._migrate()

    # ---------- migration ----------

    def _migrate(self):
        """Idempotently add billing columns to the tasks table."""
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        try:
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)")
            }
            added = []
            for col, ddl in _NEW_COLUMNS.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {ddl}")
                    added.append(col)
            conn.commit()
            if added:
                logger.info(f"tasks table migrated, added columns: {added}")
        except Exception as e:
            logger.error(f"tasks table migration failed: {e}")
        finally:
            conn.close()
        self._migrate_checkpoints()

    def _migrate_checkpoints(self):
        """幂等创建 task_checkpoints 表（checkpoint 级断点续跑）。

        CREATE TABLE IF NOT EXISTS 本身幂等，启动跑多次无副作用；与上方
        tasks 列迁移分开 try/except，一边失败不拖累另一边。进程重启后
        prepare_resume 依赖本表恢复阶段产出（辩论结论 / 系列单篇 /
        单Agent上下文等）。与 tasks 表共用同一 DB 文件。
        """
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    agent TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ok',
                    content TEXT DEFAULT '',
                    usage_json TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_task_checkpoints_task
                ON task_checkpoints(task_id)
            """)
            conn.commit()
        except Exception as e:
            logger.error(f"task_checkpoints table migration failed: {e}")
        finally:
            conn.close()

    # ---------- task passthrough ----------

    def save_task(self, status: ResearchStatus, arguments: str = "",
                  llm_config: Optional[dict] = None):
        self.store.save_task(status, arguments, llm_config=llm_config)

    def get_task(self, task_id: str) -> Optional[ResearchStatus]:
        return self.store.get_task(task_id)

    def get_task_brief(self, task_id: str) -> Optional[dict]:
        return self.store.get_task_brief(task_id)

    def find_terminal_task(self, skill_name: str, arguments: str) -> Optional[str]:
        return self.store.find_terminal_task(skill_name, arguments)

    def list_tasks(self, limit: int = 50, status_filter: str = None):
        return self.store.list_tasks(limit=limit, status_filter=status_filter)

    def delete_task(self, task_id: str):
        self.store.delete_task(task_id)

    def cleanup_old_tasks(self, max_age_days: int = 7):
        """清理过期任务；连带删除孤儿检查点（任务行已删的残留）。"""
        deleted = self.store.cleanup_old_tasks(max_age_days)
        conn = None
        try:
            conn = sqlite3.connect(str(self.store.db_path), timeout=10)
            conn.execute(
                "DELETE FROM task_checkpoints WHERE task_id NOT IN (SELECT task_id FROM tasks)"
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"orphan checkpoint cleanup failed: {e}")
        finally:
            if conn is not None:
                conn.close()
        return deleted

    def mark_running_as_interrupted(self):
        return self.store.mark_running_as_interrupted()

    def get_cost_stats(self, days: int = 30, model: str = None) -> dict:
        return self.store.get_cost_stats(days, model)

    # ---------- artifacts (intermediate agent results) ----------

    def save_artifact(self, task_id: str, agent_name: str, content: str):
        self.store.save_artifact(task_id, agent_name, content)

    def get_artifacts(self, task_id: str) -> list[tuple[str, str]]:
        return self.store.get_artifacts(task_id)

    # ---------- checkpoints (阶段级断点续跑) ----------

    def save_checkpoint(self, task_id: str, phase: str, agent: str = "",
                        content: str = "", usage: Optional[dict] = None,
                        status: str = "ok"):
        """追加一条阶段检查点（append-only，与 task_artifacts 同风格）。

        phase：research / debate / synthesis / post_synthesis / single /
        series_episode_N；agent 可空（辩论结论等阶段级产出不属于单个
        Agent）。usage 为当时的任务累计 token 用量快照
        （core.llm.get_usage），仅供计费审计；content 为阶段中间产出
        文本。绝不写 api_key。失败只记日志，绝不阻断主流程。
        """
        import time as _time
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        try:
            conn.execute(
                """INSERT INTO task_checkpoints
                       (task_id, phase, agent, status, content, usage_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, (phase or "")[:80], (agent or "")[:120],
                    (status or "ok")[:20], content or "",
                    json.dumps(usage, ensure_ascii=False, default=str) if usage else "",
                    _time.time(),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to save checkpoint {task_id}/{phase}/{agent}: {e}")
        finally:
            conn.close()

    def load_checkpoints(self, task_id: str) -> list[dict]:
        """按写入顺序返回某任务的全部检查点（最旧在前，append-only 后写为准）。"""
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT id, phase, agent, status, content, usage_json, created_at
                   FROM task_checkpoints WHERE task_id = ?
                   ORDER BY created_at, id""",
                (task_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def clear_checkpoints(self, task_id: str):
        """任务成功完成后清空其检查点（失败/取消时保留，供续跑）。"""
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        try:
            conn.execute(
                "DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,)
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to clear checkpoints for {task_id}: {e}")
        finally:
            conn.close()

    def has_checkpoints(self, task_id: str) -> bool:
        """该任务是否留有检查点（/api/task/{id} 据此提示「可恢复」）。"""
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        try:
            row = conn.execute(
                "SELECT 1 FROM task_checkpoints WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
            return row is not None
        except Exception as e:
            logger.warning(f"has_checkpoints failed for {task_id}: {e}")
            return False
        finally:
            conn.close()

    def list_interrupted_with_checkpoints(self) -> list[dict]:
        """启动扫描：interrupted 且留有检查点的任务（可恢复名单）。

        只登记、绝不自动重跑（避免重启即烧 token）；任务状态保持
        interrupted，由用户显式调 /api/tasks/{id}/resume 触发续跑。
        """
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT t.task_id, t.skill_name, t.arguments, t.updated_at,
                          COUNT(c.id) AS checkpoint_count,
                          GROUP_CONCAT(DISTINCT c.phase) AS phases
                   FROM tasks t
                   JOIN task_checkpoints c ON c.task_id = t.task_id
                   WHERE t.status = 'interrupted'
                   GROUP BY t.task_id
                   ORDER BY t.updated_at DESC"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------- async wrappers (offload blocking I/O from the loop) ----------

    async def asave_task(self, status: ResearchStatus, arguments: str = "",
                         llm_config: Optional[dict] = None):
        await asyncio.to_thread(self.save_task, status, arguments, llm_config)

    async def aget_task(self, task_id: str) -> Optional[ResearchStatus]:
        return await asyncio.to_thread(self.get_task, task_id)

    async def asave_billing(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self.save_billing, *args, **kwargs)

    async def asave_report(self, *args, **kwargs) -> Path:
        return await asyncio.to_thread(self.save_report, *args, **kwargs)

    async def asave_artifact(self, task_id: str, agent_name: str, content: str):
        await asyncio.to_thread(self.save_artifact, task_id, agent_name, content)

    async def aget_artifacts(self, task_id: str) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self.get_artifacts, task_id)

    async def asave_checkpoint(self, *args, **kwargs):
        await asyncio.to_thread(self.save_checkpoint, *args, **kwargs)

    async def aload_checkpoints(self, task_id: str) -> list[dict]:
        return await asyncio.to_thread(self.load_checkpoints, task_id)

    async def aclear_checkpoints(self, task_id: str):
        await asyncio.to_thread(self.clear_checkpoints, task_id)

    async def alist_interrupted_with_checkpoints(self) -> list[dict]:
        return await asyncio.to_thread(self.list_interrupted_with_checkpoints)

    async def aget_cost_stats(self, days: int = 30, model: str = None) -> dict:
        return await asyncio.to_thread(self.get_cost_stats, days, model)

    # ---------- billing ----------

    def save_billing(
        self,
        task_id: str,
        usage: dict,
        duration_s: float,
        guard_warnings: Optional[list] = None,
        model: Optional[str] = None,
        accumulate: bool = False,
    ) -> dict:
        """Persist token usage + cost for a finished task. Returns the record.

        Cost is computed per-model from the usage breakdown
        (usage["by_model"], see compute_usage_breakdown) and then summed;
        the per-model detail is stored in the usage_json column. Models
        missing from the price list contribute 0 and mark the record
        priced=False (partially priced when mixed with known models).

        accumulate=True (resume flow) ADDS this run's tokens/cost/duration to
        the existing row instead of overwriting, so a resumed task's total
        spend stays visible in the cost dashboard; per-model breakdowns are
        merged per model.
        """
        breakdown = compute_usage_breakdown(usage, default_model=model)
        record = {
            "tokens_prompt": usage.get("prompt_tokens", 0) or 0,
            "tokens_completion": usage.get("completion_tokens", 0) or 0,
            "cost_yuan": round(sum(r["cost_yuan"] for r in breakdown), 4),
            "duration_s": round(duration_s, 1),
            "guard_warnings": json.dumps(guard_warnings or [], ensure_ascii=False),
            "model": (model or "")[:300],
            "by_model": breakdown,
            "priced": all(r["priced"] for r in breakdown) if breakdown else True,
        }
        usage_json = json.dumps(breakdown, ensure_ascii=False)
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if accumulate:
                row = conn.execute(
                    "SELECT usage_json FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row and row["usage_json"]:
                    try:
                        merged = merge_usage_breakdowns(json.loads(row["usage_json"]), breakdown)
                    except Exception:
                        merged = breakdown
                else:
                    merged = breakdown
                conn.execute(
                    """UPDATE tasks SET
                           tokens_prompt = tokens_prompt + ?,
                           tokens_completion = tokens_completion + ?,
                           cost_yuan = ROUND(cost_yuan + ?, 4),
                           duration_s = COALESCE(duration_s, 0) + ?,
                           guard_warnings = ?, model = ?, usage_json = ?
                       WHERE task_id = ?""",
                    (
                        record["tokens_prompt"], record["tokens_completion"],
                        record["cost_yuan"], record["duration_s"],
                        record["guard_warnings"], record["model"],
                        json.dumps(merged, ensure_ascii=False), task_id,
                    ),
                )
                record["by_model"] = merged
            else:
                conn.execute(
                    """UPDATE tasks SET
                           tokens_prompt = ?, tokens_completion = ?,
                           cost_yuan = ?, duration_s = ?, guard_warnings = ?, model = ?,
                           usage_json = ?
                       WHERE task_id = ?""",
                    (
                        record["tokens_prompt"], record["tokens_completion"],
                        record["cost_yuan"], record["duration_s"],
                        record["guard_warnings"], record["model"], usage_json, task_id,
                    ),
                )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to save billing for {task_id}: {e}")
        finally:
            conn.close()
        return record

    def get_billing(self, task_id: str) -> dict:
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT tokens_prompt, tokens_completion, cost_yuan,
                          duration_s, guard_warnings, usage_json
                   FROM tasks WHERE task_id = ?""",
                (task_id,),
            ).fetchone()
            if not row:
                return {}
            out = dict(row)
            try:
                out["guard_warnings"] = json.loads(out.get("guard_warnings") or "[]")
            except Exception:
                out["guard_warnings"] = []
            try:
                out["by_model"] = json.loads(out.pop("usage_json") or "[]")
            except Exception:
                out["by_model"] = []
            return out
        finally:
            conn.close()

    def get_task_extras(self, task_id: str) -> dict:
        """arguments + billing duration/cost for the /api/task/{id} payload.

        附带 has_checkpoints / checkpoint_phases：interrupted 任务是否
        留有可恢复检查点（前端据此提示「可恢复」）；检查点查询失败时
        安全回落 False / []，不影响主字段返回。
        """
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT arguments, duration_s, cost_yuan
                   FROM tasks WHERE task_id = ?""",
                (task_id,),
            ).fetchone()
            out = dict(row) if row else {}
            try:
                ck_rows = conn.execute(
                    """SELECT phase, MAX(created_at) AS ts
                       FROM task_checkpoints WHERE task_id = ?
                       GROUP BY phase ORDER BY ts""",
                    (task_id,),
                ).fetchall()
                out["has_checkpoints"] = bool(ck_rows)
                out["checkpoint_phases"] = [r["phase"] for r in ck_rows]
            except Exception:
                out["has_checkpoints"] = False
                out["checkpoint_phases"] = []
            return out
        finally:
            conn.close()

    # ---------- reports ----------

    def save_report(
        self,
        skill_name: str,
        arguments: str,
        report: str,
        duration_seconds: float = 0,
        tokens: Optional[dict] = None,
        partial: bool = False,
        summary: Optional[str] = None,
        task_id: Optional[str] = None,
        overwrite_name: Optional[str] = None,
        model: str = "",
        guard_warnings: Optional[list] = None,
        data_status: Optional[dict] = None,
    ) -> Path:
        """Save report markdown + a metadata sidecar (.meta.json).

        partial=True marks the report as an incomplete-run artifact dump
        (cancelled/failed task); listings surface it via meta["partial"].
        summary is an optional one-line conclusion stored as meta["summary"]
        and shown on the reports list page.
        task_id is stored in meta so a report can be traced back to its task
        (resume flow). overwrite_name reuses an existing report filename —
        a successful resume replaces the partial report in place instead of
        adding a duplicate row to the history list.

        批D(2026-09-11): model / guard_warnings / data_status 三个审计字段
        随报告落盘，供历史列表显示「模型版本 / 降级 / 数据状态」。
        此前这些信息只存在于 tasks 表（guard_warnings/model 列），报告列表
        完全看不到 —— 报告一旦离开运行态就无法自证是怎么跑出来的。
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_skill = _sanitize_filename(skill_name)
        if overwrite_name:
            report_path = REPORTS_DIR / _sanitize_filename(overwrite_name)
        else:
            report_path = REPORTS_DIR / f"{safe_skill}_{timestamp}.md"
        report_path.write_text(report, encoding="utf-8")
        meta = {
            "skill_name": skill_name,
            "arguments": arguments,
            "duration_seconds": round(duration_seconds, 1),
            "tokens": tokens or {},
            "created_at": datetime.now().isoformat(),
        }
        if task_id:
            meta["task_id"] = task_id
        if partial:
            meta["partial"] = True
        if summary:
            meta["summary"] = summary
        # ── 批D 审计字段 ────────────────────────────────────────
        eff_model = _one_line_str(model)
        if not eff_model:
            eff_model = _dominant_model(tokens)
        if eff_model:
            meta["model"] = eff_model
        clean_warnings = [
            {"gate": _one_line_str(w.get("gate"))[:40], "detail": _one_line_str(w.get("detail"))[:300]}
            for w in (guard_warnings or []) if isinstance(w, dict)
        ][:10]
        if clean_warnings:
            meta["guard_warnings"] = clean_warnings
        if data_status or clean_warnings:
            status = dict(data_status or {})
            status.setdefault("state", "degraded" if clean_warnings else "ok")
            if clean_warnings and not status.get("gates"):
                status["gates"] = sorted({w["gate"] for w in clean_warnings if w["gate"]})
            meta["data_status"] = status
        try:
            report_path.with_suffix(".meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to write report meta: {e}")
        return report_path

    def find_report_name_for_task(
        self, task_id: str, skill_name: str = "", arguments: str = ""
    ) -> Optional[str]:
        """Locate the report filename belonging to a task.

        Primary: meta["task_id"] exact match. Legacy fallback: newest
        *partial* report with the same skill+arguments (written before
        meta carried task_id). Returns None when nothing matches — the
        caller then writes a fresh report file.
        """
        if not REPORTS_DIR.exists():
            return None
        legacy_candidate = None
        metas = sorted(
            REPORTS_DIR.glob("*.meta.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for meta_path in metas:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            report_name = meta_path.name[: -len(".meta.json")] + ".md"
            if meta.get("task_id") == task_id:
                return report_name
            if (
                legacy_candidate is None
                and meta.get("partial")
                and skill_name
                and meta.get("skill_name") == skill_name
                and meta.get("arguments") == arguments
            ):
                legacy_candidate = report_name
        return legacy_candidate


_repo: Optional[Repository] = None


def get_repository() -> Repository:
    global _repo
    if _repo is None:
        _repo = Repository()
    return _repo
