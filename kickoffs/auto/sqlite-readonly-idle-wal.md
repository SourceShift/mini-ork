# Read-only state.db opens fail on an idle WAL database

## Goal

`state.db` runs in WAL mode. A `mode=ro` connection cannot create the `-shm`
file, so when no process has the database open and the `-wal`/`-shm` sidecar
files are absent (SQLite's default on Linux deletes them when the last
connection closes; on macOS other tools can leave the same state), every query
on a read-only connection fails with `sqlite3.OperationalError: unable to open
database file`. Reproduced on 2026-10-03 against `mini_ork.web.db.StateDB` — the
reader behind `mini-ork serve`, the ACP agent's run history (Zed Thread History)
and the `mini-ork mcp-context` tools: an idle db with no sidecars makes all of
them raise. The live `.mini-ork/state.db` of the main checkout was in exactly
that state the same morning.

Add one read-only opener that works in that state and use it at every
`mode=ro` site.

## Files in scope

- `mini_ork/sqlite_read.py` — NEW.
- `mini_ork/web/db.py` — `StateDB._conn_for_thread`.
- `mini_ork/otel_export.py` — the two `mode=ro` opens (lines ~84 and ~214).
- `mini_ork/cli/update.py` — the `mode=ro` open (~126) and its docstrings.
- `mini_ork/orchestration/concord_admission.py` — the `mode=ro` open (~243).
- `mini_ork/mcp_context/server.py` — module docstring only (it says `mode=ro`).
- `scripts/run_heldout.py` — `_run_cost_usd`'s open (~111).
- `tests/unit/test_sqlite_read.py` — NEW.

No other file changes.

## Mechanism (exact spec)

```python
def connect_readonly(path: str | os.PathLike, *, timeout: float = 5.0) -> sqlite3.Connection
```

1. `uri = Path(path).resolve().as_uri() + "?mode=ro"` (proper percent-encoding:
   a path containing `?`, `#` or `%` must still open — the current f-strings break).
2. `con = sqlite3.connect(uri, uri=True, timeout=timeout)`; probe with
   `con.execute("PRAGMA schema_version").fetchone()`.
3. If the connect or the probe raises `sqlite3.OperationalError`: close it, then
   `con = sqlite3.connect(os.fspath(path), timeout=timeout)` and
   `con.execute("PRAGMA query_only = ON")` — a normal connection can create the
   sidecar files; `query_only` keeps it read-only. If that also raises, re-raise
   the original error.
4. A path that does not exist → `FileNotFoundError` (never create an empty db: the
   plain-connect fallback would otherwise create one).

Callers replace their `sqlite3.connect(f"file:{...}?mode=ro", uri=True)` with
`connect_readonly(...)`, keeping everything else (row factories, pragmas, error
handling, return values) as it is. `StateDB` keeps `isolation_level=None`
(set `con.isolation_level = None` after opening) and its existing pragmas
(`query_only` is harmless to repeat). `scripts/run_heldout.py` imports it the way
that script already imports from `mini_ork` (check its sys.path setup).

## Tests (`tests/unit/test_sqlite_read.py`)

Build a WAL db in `tmp_path`, write a row, `PRAGMA wal_checkpoint(TRUNCATE)`,
close, then delete `state.db-shm` / `state.db-wal` if present (that is the idle
state). Then:
- `connect_readonly` reads the row;
- `StateDB(db).rows(...)` reads the row (the regression this fixes);
- writes through a `connect_readonly` connection fail (`sqlite3.OperationalError`)
  in both the `mode=ro` path and the fallback path;
- a db under a directory whose name contains `#` and `?` opens;
- a missing path raises `FileNotFoundError` and creates nothing;
- a non-WAL (rollback-journal) db still opens on the `mode=ro` path.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_sqlite_read.py tests/unit/test_web_repositories_py.py tests/unit/test_run_detail_repo_py.py tests/unit/test_mcp_context_server.py tests/unit/test_acp_history.py tests/test_learning_routes.py` passes.
- `grep -rn 'mode=ro"' mini_ork scripts --include=*.py` finds only `mini_ork/sqlite_read.py`.
- `ruff check` clean; diff touches only the files in scope.
