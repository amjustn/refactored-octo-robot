"""Harness repository — wraps TaskStore (API unchanged) and owns billing.

- Report saving (markdown + .meta.json sidecar) lives here.
- tasks table gains billing columns via an idempotent startup migration
  (PRAGMA table_info check, then ALTER TABLE only for missing columns).
  Old rows default to 0 / '[]'.
- Cost is computed from a per-model price list (constants below, adjust
  to match the actual provider bill).
"""
from __future__ import annotations

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

# 元 / 百万 tokens。按 DeepSeek 公开价目估算，可按实际账单调整。
MODEL_PRICING = {
    "deepseek-v4-flash": {"prompt": 2.0, "completion": 8.0},
    "deepseek-v4-pro": {"prompt": 4.0, "completion": 16.0},
    "default": {"prompt": 2.0, "completion": 8.0},
}

# Billing columns added on top of the legacy tasks schema.
_NEW_COLUMNS = {
    "tokens_prompt": "tokens_prompt INTEGER DEFAULT 0",
    "tokens_completion": "tokens_completion INTEGER DEFAULT 0",
    "cost_yuan": "cost_yuan REAL DEFAULT 0",
    "duration_s": "duration_s REAL",
    "guard_warnings": "guard_warnings TEXT DEFAULT '[]'",
}


def _sanitize_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", name)
    return name or "report"


def compute_cost_yuan(usage: dict, model: Optional[str] = None) -> float:
    """Compute cost in CNY from a token-usage dict and the price list."""
    price = MODEL_PRICING.get(model or "", MODEL_PRICING["default"])
    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    cost = prompt * price["prompt"] / 1e6 + completion * price["completion"] / 1e6
    return round(cost, 4)


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

    # ---------- task passthrough ----------

    def save_task(self, status: ResearchStatus, arguments: str = ""):
        self.store.save_task(status, arguments)

    def get_task(self, task_id: str) -> Optional[ResearchStatus]:
        return self.store.get_task(task_id)

    def list_tasks(self, limit: int = 50, status_filter: str = None):
        return self.store.list_tasks(limit=limit, status_filter=status_filter)

    def delete_task(self, task_id: str):
        self.store.delete_task(task_id)

    def cleanup_old_tasks(self, max_age_days: int = 7):
        return self.store.cleanup_old_tasks(max_age_days)

    def mark_running_as_interrupted(self):
        return self.store.mark_running_as_interrupted()

    # ---------- billing ----------

    def save_billing(
        self,
        task_id: str,
        usage: dict,
        duration_s: float,
        guard_warnings: Optional[list] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Persist token usage + cost for a finished task. Returns the record."""
        record = {
            "tokens_prompt": usage.get("prompt_tokens", 0) or 0,
            "tokens_completion": usage.get("completion_tokens", 0) or 0,
            "cost_yuan": compute_cost_yuan(usage, model),
            "duration_s": round(duration_s, 1),
            "guard_warnings": json.dumps(guard_warnings or [], ensure_ascii=False),
        }
        conn = sqlite3.connect(str(self.store.db_path), timeout=10)
        try:
            conn.execute(
                """UPDATE tasks SET
                       tokens_prompt = ?, tokens_completion = ?,
                       cost_yuan = ?, duration_s = ?, guard_warnings = ?
                   WHERE task_id = ?""",
                (
                    record["tokens_prompt"], record["tokens_completion"],
                    record["cost_yuan"], record["duration_s"],
                    record["guard_warnings"], task_id,
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
                          duration_s, guard_warnings
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
    ) -> Path:
        """Save report markdown + a metadata sidecar (.meta.json)."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_skill = _sanitize_filename(skill_name)
        report_path = REPORTS_DIR / f"{safe_skill}_{timestamp}.md"
        report_path.write_text(report, encoding="utf-8")
        meta = {
            "skill_name": skill_name,
            "arguments": arguments,
            "duration_seconds": round(duration_seconds, 1),
            "tokens": tokens or {},
            "created_at": datetime.now().isoformat(),
        }
        try:
            report_path.with_suffix(".meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to write report meta: {e}")
        return report_path


_repo: Optional[Repository] = None


def get_repository() -> Repository:
    global _repo
    if _repo is None:
        _repo = Repository()
    return _repo
