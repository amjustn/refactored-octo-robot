"""SQLite-based persistent task store for AI Berkshire Web.

Survives server restarts — tasks are saved to SQLite and can be resumed
or queried after restart. Running tasks are marked as 'interrupted' on startup.
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models.schemas import ResearchStatus, AgentProgress
from ..core.config import BASE_DIR

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
