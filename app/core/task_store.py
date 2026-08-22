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
                # save_task 写入 llm_config_json —— 该列的自愈迁移就放在这里，
                # 保证不经 persist.Repository 直接实例化的 TaskStore 也能写。
                existing = {
                    row[1] for row in conn.execute("PRAGMA table_info(tasks)")
                }
                if "llm_config_json" not in existing:
                    conn.execute(
                        "ALTER TABLE tasks ADD COLUMN llm_config_json TEXT DEFAULT ''"
                    )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"TaskStore initialized at {self.db_path}")

    def save_task(self, status: ResearchStatus, arguments: str = "",
                  llm_config: Optional[dict] = None):
        """Save or update a task.

        llm_config（可选，须已脱敏、绝不含 api_key）只在首次 INSERT 时写入
        llm_config_json 列；后续状态流转的 UPDATE 不动该列，resume 借此沿用
        原任务的 per-agent 模型配置。
        """
        with self._lock:
            conn = self._get_conn()
            try:
                now = datetime.now().isoformat()
                agents_json = json.dumps(
                    [a.model_dump() for a in status.agents],
                    ensure_ascii=False,
                )
                llm_config_json = json.dumps(llm_config, ensure_ascii=False) if llm_config else ""
                conn.execute("""
                    INSERT INTO tasks (task_id, skill_name, status, arguments, agents_json, report, error, created_at, updated_at, llm_config_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    llm_config_json,
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
                    "SELECT task_id, skill_name, arguments, status, llm_config_json FROM tasks WHERE task_id = ?",
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
        """Delete tasks older than max_age_days,连同它们的 task_artifacts。

        返回删除的任务数。cancelled/running 任务不在清理范围（与原口径一致：
        cancelled 计入成本统计，过早删除会截断统计窗口）。
        """
        with self._lock:
            conn = self._get_conn()
            try:
                cutoff = datetime.now().timestamp() - max_age_days * 86400
                cutoff_str = datetime.fromtimestamp(cutoff).isoformat()
                doomed = [
                    r[0] for r in conn.execute(
                        "SELECT task_id FROM tasks WHERE updated_at < ? "
                        "AND status IN ('completed', 'failed', 'interrupted')",
                        (cutoff_str,),
                    ).fetchall()
                ]
                if not doomed:
                    return 0
                placeholders = ",".join("?" * len(doomed))
                conn.execute(
                    f"DELETE FROM task_artifacts WHERE task_id IN ({placeholders})", doomed,
                )
                cursor = conn.execute(
                    f"DELETE FROM tasks WHERE task_id IN ({placeholders})", doomed,
                )
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old tasks (>{max_age_days} days, artifacts included)")
                return deleted
            finally:
                conn.close()

    @staticmethod
    def _row_model_entries(row) -> list:
        """把一行任务拆成 per-model 计费条目 [(model, tokens, cost_yuan)]。

        usage_json（per-model 明细，新版 billing 写入）优先；空/坏明细回落到
        整行的 model 列（旧数据，空模型名归 'unknown'）。
        """
        raw = row["usage_json"] if "usage_json" in row.keys() else None
        if raw:
            try:
                detail = json.loads(raw)
            except Exception:
                detail = None
            if isinstance(detail, list) and detail:
                out = []
                for d in detail:
                    if not isinstance(d, dict):
                        continue
                    out.append((
                        (d.get("model") or "unknown"),
                        (d.get("prompt_tokens", 0) or 0) + (d.get("completion_tokens", 0) or 0),
                        d.get("cost_yuan", 0) or 0,
                    ))
                if out:
                    return out
        return [(
            row["model"] or "unknown",
            (row["tokens_prompt"] or 0) + (row["tokens_completion"] or 0),
            row["cost_yuan"] or 0,
        )]

    def get_cost_stats(self, days: int = 30, model: str = None) -> dict:
        """Aggregate LLM billing stats over the last  calendar days
        (including today, local time).

        Only tasks that actually ran to a terminal state are counted:
        status IN ('completed', 'failed', 'cancelled'). 'running' rows
        have no final billing yet, and 'interrupted' rows never finished
        a run — including their partial/zero billing would skew the
        totals and averages, so both are excluded.

        by_model 使用 per-model 明细（usage_json 列）：一次任务用了几个
        模型，费用就按明细归集到各模型；没有明细的旧行回落到 model 列。
        （可选）过滤：只统计包含该模型用量的任务，且金额/token
        只计该模型的份额（runs/耗时为任务级）。返回 totals / by_day
        （缺日补零）/ by_skill / by_model（均按费用降序）。 被
        钳制到 [1, 365]。
        """
        days = max(1, min(int(days), 365))
        today = datetime.now().date()
        start = today - timedelta(days=days - 1)
        cutoff = datetime(start.year, start.month, start.day).isoformat()
        where = ("WHERE status IN ('completed', 'failed', 'cancelled') "
                 "AND created_at >= ?")
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(f"""
                    SELECT substr(created_at, 1, 10) AS day, skill_name, model, usage_json,
                           tokens_prompt, tokens_completion, cost_yuan, duration_s
                    FROM tasks {where}
                """, (cutoff,)).fetchall()
            finally:
                conn.close()

        # (day, skill, entries, duration)；entries 见 _row_model_entries
        parsed = [
            (r["day"], r["skill_name"], self._row_model_entries(r), r["duration_s"] or 0)
            for r in rows
        ]
        if model:
            parsed = [p for p in parsed if any(e[0] == model for e in p[2])]

        def _portion(entries):
            """model 过滤时只计该模型份额；未过滤时计整任务。"""
            if model:
                sel = [e for e in entries if e[0] == model]
            else:
                sel = entries
            return sum(e[1] for e in sel), sum(e[2] for e in sel)

        runs = len(parsed)
        tot_tp = tot_tc = 0
        tot_cost = 0.0
        tot_dur = 0.0
        day_map: dict[str, dict] = {}
        skill_map: dict[str, dict] = {}
        model_map: dict[str, dict] = {}
        for day, skill, entries, dur in parsed:
            tok, cost = _portion(entries)
            tot_cost += cost
            tot_dur += dur
            if model:
                # 过滤态下 prompt/completion 无法从份额可靠拆分，合并计入
                # tokens_prompt（tokens_completion 置 0，避免虚假拆分）
                tot_tp += tok
            d = day_map.setdefault(day, {"runs": 0, "cost_yuan": 0.0, "tokens": 0})
            d["runs"] += 1
            d["cost_yuan"] += cost
            d["tokens"] += tok
            sk = skill_map.setdefault(skill, {"runs": 0, "cost_yuan": 0.0, "tokens": 0})
            sk["runs"] += 1
            sk["cost_yuan"] += cost
            sk["tokens"] += tok
            for m_name, m_tok, m_cost in entries:
                if model and m_name != model:
                    continue
                mm = model_map.setdefault(
                    m_name, {"runs": 0, "cost_yuan": 0.0, "tokens": 0, "durations": []})
                mm["runs"] += 1
                mm["cost_yuan"] += m_cost
                mm["tokens"] += m_tok
                mm["durations"].append(dur)

        # totals 的 prompt/completion 分列只在未过滤时有意义（任务级原值）
        if not model:
            tot_tp = sum((r["tokens_prompt"] or 0) for r in rows)
            tot_tc = sum((r["tokens_completion"] or 0) for r in rows)
        else:
            tot_tc = 0  # 过滤态合并计入 tokens_prompt，避免虚假拆分

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
            {"skill_name": k, "runs": v["runs"],
             "cost_yuan": round(v["cost_yuan"], 4), "tokens": v["tokens"]}
            for k, v in skill_map.items()
        ]
        by_skill.sort(key=lambda x: (-x["cost_yuan"], -x["runs"]))
        by_model = [
            {"model": k, "runs": v["runs"], "cost_yuan": round(v["cost_yuan"], 4),
             "tokens": v["tokens"],
             "avg_duration_s": round(sum(v["durations"]) / len(v["durations"]), 1)}
            for k, v in model_map.items()
        ]
        by_model.sort(key=lambda x: (-x["cost_yuan"], -x["runs"]))
        return {
            "days": days,
            "totals": {
                "runs": runs,
                "tokens_prompt": tot_tp,
                "tokens_completion": tot_tc,
                "cost_yuan": round(tot_cost, 4),
                "avg_duration_s": round(tot_dur / runs, 1) if runs else 0,
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
