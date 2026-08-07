"""SQLite-based persistent task store for AI Berkshire Web.

Survives server restarts — tasks are saved to SQLite and can be resumed
or queried after restart. Running tasks are marked as 'interrupted' on startup.
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..core.config import BASE_DIR
from ..models.schemas import AgentProgress, ResearchStatus

logger = logging.getLogger("ai_berkshire.task_store")

DB_PATH = BASE_DIR / "data" / "tasks.db"


class TaskStore:
    """Thread-safe SQLite task store with automatic schema creation."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """Create tables if not exist."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        skill_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        arguments TEXT DEFAULT '',
                        agents_json TEXT DEFAULT '[]',
                        report TEXT DEFAULT '',
                        error TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_updated
                    ON tasks(updated_at DESC)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS task_artifacts (
                        task_id TEXT NOT NULL,
                        agent_name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_task_artifacts_task
                    ON task_artifacts(task_id)
                """)
                conn.commit()
            finally:
                conn.close()
        logger.info(f"TaskStore initialized at {self.db_path}")

    def save_task(self, status: ResearchStatus, arguments: str = ""):
        """Save or update a task."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = datetime.now().isoformat()
                agents_json = json.dumps(
                    [a.model_dump() for a in status.agents],
                    ensure_ascii=False,
                )
                conn.execute("""
                    INSERT INTO tasks (task_id, skill_name, status, arguments, agents_json, report, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        status = excluded.status,
                        agents_json = excluded.agents_json,
                        report = excluded.report,
                        error = excluded.error,
                        updated_at = excluded.updated_at
                """, (
                    status.task_id,
                    status.skill_name,
                    status.status,
                    arguments,
                    agents_json,
                    status.report,
                    status.error,
                    now,
                    now,
                ))
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to save task {status.task_id}: {e}")
            finally:
                conn.close()

    def get_task(self, task_id: str) -> Optional[ResearchStatus]:
        """Get a task by ID."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if not row:
                    return None
                return self._row_to_status(row)
            finally:
                conn.close()

    def get_task_brief(self, task_id: str) -> Optional[dict]:
        """Lightweight row for resume decisions: skill/arguments/status."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT task_id, skill_name, arguments, status FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def find_terminal_task(self, skill_name: str, arguments: str) -> Optional[str]:
        """Latest terminal task matching skill+arguments.

        Fallback mapping for reports written before meta carried task_id.
        """
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT task_id FROM tasks
                       WHERE skill_name = ? AND arguments = ?
                         AND status IN ('completed', 'failed', 'cancelled', 'interrupted')
                       ORDER BY created_at DESC LIMIT 1""",
                    (skill_name, arguments),
                ).fetchone()
                return row["task_id"] if row else None
            finally:
                conn.close()

    def list_tasks(self, limit: int = 50, status_filter: str = None) -> list[ResearchStatus]:
        """List tasks, optionally filtered by status."""
        with self._lock:
            conn = self._get_conn()
            try:
                if status_filter:
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                        (status_filter, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [self._row_to_status(r) for r in rows]
            finally:
                conn.close()

    def save_artifact(self, task_id: str, agent_name: str, content: str):
        """Persist an intermediate agent result for a task (append-only)."""
        import time as _time
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO task_artifacts (task_id, agent_name, content, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, agent_name, content, _time.time()),
                )
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to save artifact {task_id}/{agent_name}: {e}")
            finally:
                conn.close()

    def get_artifacts(self, task_id: str) -> list[tuple[str, str]]:
        """All artifacts for a task as [(agent_name, content)], oldest first."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT agent_name, content FROM task_artifacts WHERE task_id = ? ORDER BY created_at",
                    (task_id,),
                ).fetchall()
                return [(row["agent_name"], row["content"]) for row in rows]
            finally:
                conn.close()

    def delete_task(self, task_id: str):
        """Delete a task."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
                conn.commit()
            finally:
                conn.close()

    def cleanup_old_tasks(self, max_age_days: int = 7):
        """Delete tasks older than max_age_days."""
        with self._lock:
            conn = self._get_conn()
            try:
                cutoff = datetime.now().timestamp() - max_age_days * 86400
                cutoff_str = datetime.fromtimestamp(cutoff).isoformat()
                cursor = conn.execute(
                    "DELETE FROM tasks WHERE updated_at < ? AND status IN ('completed', 'failed', 'interrupted')",
                    (cutoff_str,),
                )
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old tasks (>{max_age_days} days)")
                return deleted
            finally:
                conn.close()

    def get_cost_stats(self, days: int = 30, model: str = None) -> dict:
        """Aggregate LLM billing stats over the last `days` calendar days
        (including today, local time).

        Only tasks that actually ran to a terminal state are counted:
        status IN ('completed', 'failed', 'cancelled'). 'running' rows
        have no final billing yet, and 'interrupted' rows never finished
        a run — including their partial/zero billing would skew the
        totals and averages, so both are excluded.

        `model`（可选）按模型过滤所有聚合；模型名为空的旧记录归入
        'unknown' 分组。返回 totals / by_day（缺日补零）/ by_skill /
        by_model（均按费用降序）。`days` 被钳制到 [1, 365]。
        """
        days = max(1, min(int(days), 365))
        today = datetime.now().date()
        start = today - timedelta(days=days - 1)
        cutoff = datetime(start.year, start.month, start.day).isoformat()
        # 空的 model 列（旧数据）统一归入 'unknown' 分组，前端以此值回传过滤
        model_expr = "COALESCE(NULLIF(model, ''), 'unknown')"
        where = ("WHERE status IN ('completed', 'failed', 'cancelled') "
                 "AND created_at >= ?")
        params: tuple = (cutoff,)
        if model:
            where += f" AND {model_expr} = ?"
            params = (cutoff, model)
        with self._lock:
            conn = self._get_conn()
            try:
                tot = conn.execute(f"""
                    SELECT COUNT(*) AS runs,
                           COALESCE(SUM(tokens_prompt), 0) AS tokens_prompt,
                           COALESCE(SUM(tokens_completion), 0) AS tokens_completion,
                           COALESCE(SUM(cost_yuan), 0) AS cost_yuan,
                           COALESCE(AVG(COALESCE(duration_s, 0)), 0) AS avg_duration_s
                    FROM tasks {where}
                """, params).fetchone()
                day_rows = conn.execute(f"""
                    SELECT substr(created_at, 1, 10) AS day,
                           COUNT(*) AS runs,
                           COALESCE(SUM(cost_yuan), 0) AS cost_yuan,
                           COALESCE(SUM(tokens_prompt + tokens_completion), 0) AS tokens
                    FROM tasks {where}
                    GROUP BY day
                """, params).fetchall()
                skill_rows = conn.execute(f"""
                    SELECT skill_name,
                           COUNT(*) AS runs,
                           COALESCE(SUM(cost_yuan), 0) AS cost_yuan,
                           COALESCE(SUM(tokens_prompt + tokens_completion), 0) AS tokens
                    FROM tasks {where}
                    GROUP BY skill_name
                    ORDER BY cost_yuan DESC, runs DESC
                """, params).fetchall()
                model_rows = conn.execute(f"""
                    SELECT {model_expr} AS model,
                           COUNT(*) AS runs,
                           COALESCE(SUM(cost_yuan), 0) AS cost_yuan,
                           COALESCE(SUM(tokens_prompt + tokens_completion), 0) AS tokens,
                           COALESCE(AVG(COALESCE(duration_s, 0)), 0) AS avg_duration_s
                    FROM tasks {where}
                    GROUP BY {model_expr}
                    ORDER BY cost_yuan DESC, runs DESC
                """, params).fetchall()
            finally:
                conn.close()

        day_map = {r["day"]: r for r in day_rows}
        by_day = []
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            r = day_map.get(d)
            by_day.append({
                "date": d,
                "runs": r["runs"] if r else 0,
                "cost_yuan": round(r["cost_yuan"], 4) if r else 0,
                "tokens": r["tokens"] if r else 0,
            })
        by_skill = [
            {
                "skill_name": r["skill_name"],
                "runs": r["runs"],
                "cost_yuan": round(r["cost_yuan"], 4),
                "tokens": r["tokens"],
            }
            for r in skill_rows
        ]
        by_model = [
            {
                "model": r["model"],
                "runs": r["runs"],
                "cost_yuan": round(r["cost_yuan"], 4),
                "tokens": r["tokens"],
                "avg_duration_s": round(r["avg_duration_s"], 1),
            }
            for r in model_rows
        ]
        return {
            "days": days,
            "totals": {
                "runs": tot["runs"],
                "tokens_prompt": tot["tokens_prompt"],
                "tokens_completion": tot["tokens_completion"],
                "cost_yuan": round(tot["cost_yuan"], 4),
                "avg_duration_s": round(tot["avg_duration_s"], 1),
            },
            "by_day": by_day,
            "by_skill": by_skill,
            "by_model": by_model,
        }

    def mark_running_as_interrupted(self):
        """On startup, mark any 'running' tasks as 'interrupted'."""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "UPDATE tasks SET status = 'interrupted', error = '服务器重启导致中断', updated_at = ? WHERE status = 'running'",
                    (datetime.now().isoformat(),),
                )
                count = cursor.rowcount
                conn.commit()
                if count > 0:
                    logger.warning(f"Marked {count} running tasks as 'interrupted' due to restart")
                return count
            finally:
                conn.close()

    def _row_to_status(self, row: sqlite3.Row) -> ResearchStatus:
        """Convert a DB row to ResearchStatus."""
        agents_data = json.loads(row["agents_json"]) if row["agents_json"] else []
        agents = [AgentProgress(**a) for a in agents_data]
        return ResearchStatus(
            task_id=row["task_id"],
            skill_name=row["skill_name"],
            status=row["status"],
            agents=agents,
            report=row["report"] or "",
            error=row["error"] or "",
        )
