import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


class StateCache:
    """SQLite-backed cache of Snowflake read results, scoped by account with
    a per-row TTL. Thread-safe: all DB access is guarded by a lock so it can be
    shared across the parallel fetch workers."""

    def __init__(
        self,
        path: str,
        account: str,
        ttl_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._account = account
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            "account TEXT, key TEXT, value TEXT, fetched_at REAL, "
            "PRIMARY KEY (account, key))"
        )
        self._conn.commit()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value, fetched_at FROM cache WHERE account = ? AND key = ?",
                (self._account, key),
            ).fetchone()
        if row is None:
            return None
        value_json, fetched_at = row
        if (self._clock() - fetched_at) > self._ttl:
            return None
        return json.loads(value_json)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (account, key, value, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (self._account, key, json.dumps(value), self._clock()),
            )
            self._conn.commit()

    def invalidate_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cache WHERE account = ?", (self._account,))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
