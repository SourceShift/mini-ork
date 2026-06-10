"""Read-only SQLite access for the observability UI.

state.db is WAL-mode; readonly opens see fresh writes from the live
orchestrator without holding locks.

Concurrency model — corrected from the prior single-shared-connection
attempt: sqlite3 connections are NOT thread-safe even with
`check_same_thread=False` per the CPython docs. Concurrent .execute()
calls on the same connection can interleave cursor state and corrupt
fetches. We use a *per-thread* connection pool instead:

  - FastAPI runs sync handlers in a threadpool (default 40 workers).
  - Each thread gets its own sqlite connection on first use.
  - Connections stay open for the thread's lifetime (no per-request
    PRAGMA overhead).
  - Read-only + WAL means concurrent reads scale linearly.

Why not aiosqlite: this app is read-mostly + on-disk + sub-ms queries;
the GIL + threadpool is fine and avoids an async-only dependency.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class StateDB:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        if not self.db_path.exists():
            raise FileNotFoundError(f"state.db not found at {self.db_path}")
        # threading.local stores one connection per worker thread.
        self._local = threading.local()
        # Cross-thread caches (each guarded by lock).
        self._table_cache: dict[str, bool] = {}
        self._table_cache_lock = threading.Lock()
        self._result_cache: dict[str, tuple[float, Any]] = {}
        self._result_cache_lock = threading.Lock()

    def _conn_for_thread(self) -> sqlite3.Connection:
        con = getattr(self._local, "conn", None)
        if con is not None:
            return con
        uri = f"file:{self.db_path}?mode=ro"
        con = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=5.0,
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA busy_timeout = 2000")
        con.execute("PRAGMA cache_size = -16000")  # 16 MiB
        con.execute("PRAGMA mmap_size = 134217728")  # 128 MiB
        self._local.conn = con
        return con

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        yield self._conn_for_thread()

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        c = self._conn_for_thread()
        cur = c.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def row(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rs = self.rows(sql, params)
        return rs[0] if rs else None

    def has_table(self, name: str) -> bool:
        with self._table_cache_lock:
            cached = self._table_cache.get(name)
            if cached is not None:
                return cached
        result = bool(
            self.rows(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            )
        )
        with self._table_cache_lock:
            self._table_cache[name] = result
        return result

    def cached(self, key: str, ttl_s: float, producer):
        """Return cached value if fresh; otherwise compute + store.

        Cache is process-wide (not per-thread). Producer is called under
        no lock — if two threads miss simultaneously they each compute,
        last-writer wins. That's fine for read-mostly aggregates.
        """
        now = time.monotonic()
        with self._result_cache_lock:
            entry = self._result_cache.get(key)
        if entry and (now - entry[0]) < ttl_s:
            return entry[1]
        value = producer()
        with self._result_cache_lock:
            self._result_cache[key] = (now, value)
        return value

    def close(self) -> None:
        """Best-effort close of the current thread's connection."""
        con = getattr(self._local, "conn", None)
        if con is not None:
            con.close()
            self._local.conn = None  # type: ignore[assignment]


def resolve_home(home: str | os.PathLike | None) -> Path:
    if home:
        return Path(home).resolve()
    env = os.environ.get("MINI_ORK_HOME")
    if env:
        return Path(env).resolve()
    return (Path.cwd() / ".mini-ork").resolve()
