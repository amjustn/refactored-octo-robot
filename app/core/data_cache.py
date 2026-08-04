"""SQLite-based data cache with TTL support.

Caches market data, news, and knowledge updates to avoid
re-fetching the same data within the TTL window.
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .config import BASE_DIR

logger = logging.getLogger("ai_berkshire.data_cache")

DB_PATH = BASE_DIR / "data" / "data_cache.db"


class DataCache:
    """Thread-safe SQLite cache with per-key TTL."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS cache (
                        category TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        PRIMARY KEY (category, key)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_cache_expires
                    ON cache(expires_at)
                """)
                conn.commit()
            finally:
                conn.close()
        logger.debug(f"DataCache initialized at {self.db_path}")

    def get(self, category: str, key: str, max_age_seconds: int = 3600) -> Optional[Any]:
        """Get cached value if not expired. Returns None if missing/expired."""
        with self._lock:
            conn = self._get_conn()
            try:
                cutoff = (datetime.now() - timedelta(seconds=max_age_seconds)).isoformat()
                row = conn.execute(
                    "SELECT value FROM cache WHERE category = ? AND key = ? AND created_at > ?",
                    (category, key, cutoff),
                ).fetchone()
                if row:
                    return json.loads(row["value"])
                return None
            finally:
                conn.close()

    def set(self, category: str, key: str, value: Any):
        """Store a value in cache."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = datetime.now().isoformat()
                expires = (datetime.now() + timedelta(hours=24)).isoformat()
                conn.execute("""
                    INSERT INTO cache (category, key, value, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(category, key) DO UPDATE SET
                        value = excluded.value,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                """, (category, key, json.dumps(value, ensure_ascii=False), now, expires))
                conn.commit()
            except Exception as e:
                logger.warning(f"Cache set failed: {e}")
            finally:
                conn.close()

    def get_stale(self, category: str, key: str) -> Optional[Any]:
        """Get cached value even if expired (for fallback when network fails)."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT value, created_at FROM cache WHERE category = ? AND key = ?",
                    (category, key),
                ).fetchone()
                if row:
                    return {
                        "data": json.loads(row["value"]),
                        "cached_at": row["created_at"],
                        "stale": True,
                    }
                return None
            finally:
                conn.close()

    def cleanup_expired(self):
        """Remove all expired entries."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = datetime.now().isoformat()
                cursor = conn.execute("DELETE FROM cache WHERE expires_at < ?", (now,))
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} expired cache entries")
                return deleted
            finally:
                conn.close()

    def get_stats(self) -> dict:
        """Get cache statistics including per-category counts, total, and db size."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT category, COUNT(*) as cnt FROM cache GROUP BY category"
                ).fetchall()
                stats = {row["category"]: row["cnt"] for row in rows}
                stats["_total"] = sum(row["cnt"] for row in rows)
                stats["_db_size_bytes"] = self.db_path.stat().st_size if self.db_path.exists() else 0
                return stats
            finally:
                conn.close()
