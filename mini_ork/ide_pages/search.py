"""Full-text search across mini-ork runs — ``board runs --query``.

Two entry points:

- :func:`reindex` refreshes ``runs_fts`` rows for runs that have changed
  since the last index. Staleness is **per-run**: each ``indexed`` row
  records the ``task_runs.updated_at`` observed when the row was indexed
  (``seen_updated_at``). Each new call reads ``task_runs (id, kickoff_path,
  updated_at, created_at)`` and ``indexed (run_id, seen_updated_at)`` once
  into dicts; the work list is "never-indexed runs (newest ``created_at``
  first) + indexed runs where ``updated_at != seen_updated_at`` (newest
  ``updated_at`` first)". No per-run ``stat()``, no global watermark, no
  warm-window skip. Indexing is bounded by a 1.2 s wall-clock budget per
  call so cold catches up over many calls; when the budget stops early
  the post-work ``remaining`` count is recorded on ``errors["index"]``.
- :func:`search` returns ``(run_ids, total)`` for the ``board runs --query``
  path. Whitespace-split terms are AND-ed as ``term*`` (prefix match);
  results are ranked by ``bm25(runs_fts)`` only (no LEFT JOIN on
  ``indexed``).

The index lives at ``<home>/state/ide-search.sqlite`` — never ``state.db``
(kickoff). When sqlite was built without FTS5, both functions fall back to a
LIKE scan over ``title`` / ``recipe`` / kickoff text and record
``errors["search"]`` so the IDE can surface the degraded state.

Index schema::

    CREATE VIRTUAL TABLE IF NOT EXISTS runs_fts USING fts5(
        run_id UNINDEXED, title, recipe, body, mtime UNINDEXED
    )
    CREATE TABLE IF NOT EXISTS indexed(run_id PRIMARY KEY, seen_updated_at INTEGER);

``body`` is the kickoff text plus the run's main artifacts
(``implementer-summary.json``, ``verdict.json``, top-level
``lens-*.md`` / ``synthesis.md`` / ``cycle-report.md``), each chunk and
the joined result capped at :data:`_BODY_INDEX_CHUNK_CAP` (64 KB) so the
index stays compact.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_BODY_INDEX_CHUNK_CAP = 64_000  # 64 KB final body indexed for FTS (kickoff r3)
_DEFAULT_REINDEX_BATCH = 400  # max_runs safety upper bound (in addition to time budget)
_REINDEX_TIME_BUDGET_S = 0.8  # in-query indexing budget; the background builder does the rest
_STATE_DIR = "state"
_DB_NAME = "ide-search.sqlite"
# r4: lock + log live in the same state/ dir as the FTS cache so the
# operator's mental model stays "one state dir per home".
_LOCK_NAME = "ide-search.lock"
_LOG_NAME = "ide-search-build.log"
_BUILD_BATCH = 100  # runs per batch inside the detached background builder
_BUILD_SLEEP_S = 0.5  # inter-batch sleep (ram-sentinel CPU threshold guard)

# Stable artifact order: kickoff first, then implementer-summary.json,
# verdict.json, synthesis.md, cycle-report.md, then lens-*.md alphabetical.
_ARTIFACT_ORDER = {
    "implementer-summary.json": 0,
    "verdict.json": 1,
    "synthesis.md": 2,
    "cycle-report.md": 3,
}
_LENS_MD_ORDER = 4
_MD_NAMES = frozenset({"synthesis.md", "cycle-report.md"})


def _db_path(home: Path) -> Path:
    return Path(home) / _STATE_DIR / _DB_NAME


def _connect(home: Path) -> sqlite3.Connection:
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    # WAL: a query reads while the background builder writes.
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Idempotent schema ensure + r3/r5 column-set migration.

    Pre-r3 ``runs_fts`` schema is ``(run_id, title, recipe, body)``; r3
    added a ``mtime UNINDEXED`` column (drop + recreate, FTS5 has no
    ``ALTER TABLE``). Pre-r5 ``indexed`` schema is
    ``(run_id PRIMARY KEY, mtime REAL)``; r5 stores
    ``seen_updated_at INTEGER`` per run. When the column is missing the
    table is dropped + recreated — the index is a derived cache, so
    ``reindex`` repopulates it. The r3-r4 ``meta`` table (scan window and
    global watermark) is dropped.
    """
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='runs_fts'"
    )
    if cur.fetchone():
        cols = con.execute("PRAGMA table_info(runs_fts)").fetchall()
        # r3 schema has 5 columns (run_id, title, recipe, body, mtime);
        # pre-r3 has 4. Drop only the FTS table; indexed stays.
        if len(cols) < 5:
            con.execute("DROP TABLE runs_fts")
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='indexed'"
    )
    if cur.fetchone():
        cols = con.execute("PRAGMA table_info(indexed)").fetchall()
        col_names = {row[1] for row in cols}
        # r5 schema has columns ``run_id`` + ``seen_updated_at``; pre-r5
        # had ``run_id`` + ``mtime``. Drop + recreate when the column
        # is missing.
        if "seen_updated_at" not in col_names:
            con.execute("DROP TABLE indexed")
    con.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS runs_fts USING fts5(
            run_id UNINDEXED, title, recipe, body, mtime UNINDEXED
        );
        CREATE TABLE IF NOT EXISTS indexed(run_id PRIMARY KEY, seen_updated_at INTEGER);
        DROP TABLE IF EXISTS meta;
        """
    )
    con.commit()


# Module-level FTS5 probe cache. Availability is a property of the
# sqlite build, not the home, so one cache value covers every home in
# the process and we never re-probe per ``board runs`` call.
_FTS5_PROBE: bool | None = None


def _fts5_available(errors: dict[str, str]) -> bool:
    """Probe FTS5 — return False and surface a degraded-state note on failure.

    Probes an in-memory ``sqlite3.connect(":memory:")`` so the probe never
    takes a write-lock on the shared ``ide-search.sqlite`` or creates a
    probe table on disk. The result is cached per process because FTS5
    availability is a property of the sqlite build, not the home.
    """
    global _FTS5_PROBE
    if _FTS5_PROBE is not None:
        if not _FTS5_PROBE:
            errors["search"] = "FTS5 unavailable, fell back to LIKE scan"
        return _FTS5_PROBE
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(probe)")
            con.execute("DROP TABLE _fts5_probe")
            con.commit()
            _FTS5_PROBE = True
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        _FTS5_PROBE = False
        errors["search"] = f"FTS5 unavailable, fell back to LIKE scan: {exc}"
    return _FTS5_PROBE


# ── reindex ──────────────────────────────────────────────────────────────────


def _task_runs_rows(home: Path) -> list[tuple[str, str | None, int, int]]:
    """``(id, kickoff_path, updated_at_int, created_at_int)`` for every
    row in ``task_runs``.

    Used by :func:`reindex` for the SQL candidate pass — one query on
    ``state.db`` instead of a per-run ``stat()`` cascade. Returns ``[]``
    when the DB / table is missing. ``updated_at`` / ``created_at`` are
    read as the raw ints stored in ``task_runs`` so the per-run
    staleness comparison (``updated_at != seen_updated_at``) and the
    never-indexed ordering (``ORDER BY created_at DESC``) are both
    type-stable.
    """
    state_db = Path(home) / "state.db"
    if not state_db.is_file():
        return []
    try:
        con = sqlite3.connect(str(state_db), timeout=5)
        try:
            rows = con.execute(
                "SELECT id, kickoff_path, updated_at, created_at FROM task_runs"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return []
    out: list[tuple[str, str | None, int, int]] = []
    for rid, kpath, ua, ca in rows:
        if not (isinstance(rid, str) and rid):
            continue
        try:
            ua_int = int(ua) if ua is not None else 0
        except (TypeError, ValueError):
            ua_int = 0
        try:
            ca_int = int(ca) if ca is not None else 0
        except (TypeError, ValueError):
            ca_int = 0
        out.append((
            rid,
            kpath if isinstance(kpath, str) and kpath else None,
            ua_int,
            ca_int,
        ))
    return out


def _is_indexed_artifact(name: str) -> bool:
    """``True`` iff ``name`` is one of the per-run artifacts the index
    reads (kickoff lives outside the run dir)."""
    if name in _ARTIFACT_ORDER or name in _MD_NAMES:
        return True
    if name.startswith("lens-") and name.endswith(".md"):
        return True
    return False


def _artifact_sort_key(name: str) -> tuple[int, str]:
    """Stable order: ``_ARTIFACT_ORDER`` first, then ``lens-*.md`` alphabetical."""
    if name in _ARTIFACT_ORDER:
        return (_ARTIFACT_ORDER[name], name)
    return (_LENS_MD_ORDER, name)


def _body_for(home: Path, run_id: str, kickoff_path: str | None) -> tuple[str, float]:
    """The body to index for ``run_id`` plus its max source mtime.

    Returns ``(text, max_mtime)``; ``text`` is the kickoff + main artifacts
    joined by ``\\n\\n``. Each chunk and the joined result are capped at
    :data:`_BODY_INDEX_CHUNK_CAP` (64 KB) so the FTS index stays compact.
    The ``run_id`` is appended as a final chunk so the FTS query can match
    the run id (the column is ``UNINDEXED`` so the id is only searchable
    via the body text). Artifact order: kickoff first, then the run dir's
    artifacts in :data:`_ARTIFACT_ORDER` (implementer-summary.json,
    verdict.json, synthesis.md, cycle-report.md, lens-*.md).
    """
    from mini_ork.acp.history import _is_safe_token, _read_kickoff

    chunks: list[str] = []
    max_mtime = 0.0
    run_dir = Path(home) / "runs" / run_id

    text = _read_kickoff(Path(home), run_id, kickoff_path) if _is_safe_token(run_id) else ""
    if text:
        chunks.append(text[:_BODY_INDEX_CHUNK_CAP])

    if run_dir.is_dir():
        try:
            max_mtime = max(max_mtime, run_dir.stat().st_mtime)
        except OSError:
            pass
        try:
            with os.scandir(run_dir) as it:
                entries = sorted(
                    (e for e in it if _is_indexed_artifact(e.name)),
                    key=lambda e: _artifact_sort_key(e.name),
                )
                for entry in entries:
                    try:
                        st = entry.stat()
                        max_mtime = max(max_mtime, st.st_mtime)
                    except OSError:
                        continue
                    try:
                        chunk = Path(entry.path).read_text(
                            encoding="utf-8", errors="replace"
                        )[:_BODY_INDEX_CHUNK_CAP]
                        if chunk:
                            chunks.append(chunk)
                    except OSError:
                        continue
        except OSError:
            pass

    if kickoff_path:
        try:
            kp = Path(kickoff_path)
            if kp.is_file():
                max_mtime = max(max_mtime, kp.stat().st_mtime)
        except OSError:
            pass

    # Make the run id searchable: the FTS column is UNINDEXED, so we
    # append it to body so a query for the exact id finds the run.
    chunks.append(run_id)
    return "\n\n".join(chunks)[:_BODY_INDEX_CHUNK_CAP], max_mtime


def _title_for_run(home: Path, run_id: str) -> tuple[str, str]:
    """``(title, recipe)`` — cheap, no kickoff read for already-indexed runs."""
    from mini_ork.acp.history import _title_from_kickoff, kickoff_text

    recipe = ""
    state_db = Path(home) / "state.db"
    if state_db.is_file():
        try:
            con = sqlite3.connect(str(state_db), timeout=5)
            try:
                row = con.execute(
                    "SELECT recipe FROM task_runs WHERE id = ?", (run_id,)
                ).fetchone()
            finally:
                con.close()
            if row:
                recipe = str(row[0] or "")
        except sqlite3.OperationalError:
            recipe = ""
    text = kickoff_text(Path(home), run_id, max_chars=4000)
    title = _title_from_kickoff(text) or f"{recipe or 'mini-ork'} run"
    return title, recipe


def reindex(home: Path, *, max_runs: int | None = None,
            time_budget: float | None = None,
            fts_available: bool | None = None,
            errors: dict[str, str] | None = None) -> int:
    """Refresh stale runs in the search index.

    Returns the number of runs reindexed this call. On FTS5 absence,
    returns 0 and records a note in ``errors`` so :func:`search` can
    use LIKE.

    Staleness model (kickoff r5):

    - The ``indexed`` table stores ``seen_updated_at INTEGER`` per run —
      the ``task_runs.updated_at`` observed when the run was indexed.
    - One ``SELECT … FROM task_runs`` against ``state.db`` + one
      ``SELECT run_id, seen_updated_at FROM indexed``. No per-run
      ``stat()`` — the staleness check is a dict comparison.
    - Work list = ``never_indexed`` (newest ``created_at`` first) +
      indexed runs where ``task_runs.updated_at != seen_updated_at``
      (newest ``updated_at`` first). Never-indexed runs go first so the
      cold budget is spent on the coldest work.
    - The whole-call time budget starts at the top of ``reindex`` and
      is checked before each candidate is indexed. The first candidate
      is always processed (cold-start guarantee).
    - After the work, ``remaining`` is computed cheaply from the
      ``indexed`` + ``task_rows`` already in memory; ``indexing`` flag
      is set on ``errors["index"]`` when ``remaining > 0``.

    When ``remaining > 0`` after the in-call work, a detached builder
    is launched via :func:`_maybe_spawn_builder` so a fresh home
    doesn't need ~45 user queries to catch up. The builder's lock
    claim is atomic (``O_CREAT | O_EXCL``) and survives ram-sentinel
    kills (lock holding a dead pid is removed + reclaimed on the next
    call).
    """
    errors = errors if errors is not None else {}
    if fts_available is None and not _fts5_available(errors):
        return 0
    if max_runs is None:
        max_runs = _DEFAULT_REINDEX_BATCH
    if time_budget is None:
        time_budget = _REINDEX_TIME_BUDGET_S
    budget = max(0, int(max_runs))
    time_budget = max(0.0, float(time_budget))

    # One budget for the whole call; at least one run is indexed per call.
    started = time.monotonic()

    task_rows = _task_runs_rows(home)
    if not task_rows:
        return 0

    # While the background builder runs, a query only reports progress: two
    # writers would just wait on each other's locks.
    building = _builder_running(home)
    con = _connect(home)
    try:
        _ensure_schema(con)
        work = _work_list(con, task_rows)
        done = 0
        for run_id, kpath, updated_at in ([] if building else work):
            if done >= budget:
                break
            if done > 0 and (time.monotonic() - started) >= time_budget:
                break
            _index_one(con, home, run_id, kpath, updated_at)
            done += 1
        if done:
            con.commit()
    finally:
        con.close()

    # Progress is computed, not remembered: what is still out of date after
    # this call's work. The builder takes the rest in the background.
    total = len(task_rows)
    remaining = len(work) - done
    if remaining > 0:
        errors["index"] = f"indexing: {total - remaining}/{total} runs"
        if not building:
            _maybe_spawn_builder(home)
    return done


def _work_list(con: sqlite3.Connection,
               task_rows: list[tuple[str, str | None, int, int]]) -> list[tuple[str, str | None, int]]:
    """Runs to (re)index, as ``(run_id, kickoff_path, updated_at)``.

    Never-indexed runs first (newest ``created_at`` first), then indexed runs
    whose ``task_runs.updated_at`` moved since they were indexed (newest
    first). One SELECT on the index; no per-run ``stat``.
    """
    seen: dict[str, int] = {}
    try:
        for row in con.execute("SELECT run_id, seen_updated_at FROM indexed").fetchall():
            try:
                seen[row["run_id"]] = int(row["seen_updated_at"] or 0)
            except (TypeError, ValueError):
                continue
    except sqlite3.OperationalError:
        pass
    never: list[tuple[int, str, str | None, int]] = []
    stale: list[tuple[int, str, str | None, int]] = []
    for rid, kpath, updated_at, created_at in task_rows:
        if rid not in seen:
            never.append((created_at, rid, kpath, updated_at))
        elif seen[rid] != updated_at:
            stale.append((updated_at, rid, kpath, updated_at))
    never.sort(key=lambda x: x[0], reverse=True)
    stale.sort(key=lambda x: x[0], reverse=True)
    return [(rid, kpath, ua) for _, rid, kpath, ua in (*never, *stale)]


def _index_one(con: sqlite3.Connection, home: Path, run_id: str,
               kickoff_path: str | None, updated_at: int) -> None:
    """(Re)write one run's FTS row and record the ``updated_at`` it reflects."""
    body, observed_mtime = _body_for(home, run_id, kickoff_path)
    title, recipe = _title_for_run(home, run_id)
    con.execute("DELETE FROM runs_fts WHERE run_id = ?", (run_id,))
    con.execute(
        "INSERT INTO runs_fts(run_id, title, recipe, body, mtime) VALUES (?, ?, ?, ?, ?)",
        (run_id, title, recipe, body, float(observed_mtime)),
    )
    con.execute(
        "INSERT OR REPLACE INTO indexed(run_id, seen_updated_at) VALUES (?, ?)",
        (run_id, int(updated_at)),
    )


# ── search ───────────────────────────────────────────────────────────────────


_FTS_PUNCT = re.compile(r'["()]')
_FS = re.compile(r"\s+")


def _escape_term(raw: str) -> str:
    """FTS5-safe prefix term.

    Quotes are doubled (FTS5's only escape). Parens are FTS5 group syntax,
    which can't be escaped, so they're stripped. Everything else is kept
    so ``"node-id"`` and ``"don't"`` work as users expect.
    """
    cleaned = _FTS_PUNCT.sub("", raw)
    if not cleaned:
        return ""
    return '"' + cleaned.replace('"', '""') + '"*'


def _like_term(raw: str) -> str:
    """LIKE-safe substring — strips ``"`` / ``(`` / ``)`` so user input
    doesn't break the LIKE pattern. (LIKE wildcards ``%`` and ``_`` pass
    through, so a query of ``_`` would match every row; the LIKE path
    is the degraded fallback, not the steady state.)
    """
    return _FTS_PUNCT.sub("", raw)


def search(home: Path, query: str, limit: int, offset: int,
           *, errors: dict[str, str] | None = None) -> tuple[list[str], int]:
    """Return ``(run_ids, total)`` matching ``query``.

    Whitespace-split AND; bm25 then mtime DESC; LIMIT/OFFSET applied. When
    FTS5 is unavailable the LIKE fallback honours ``offset``/``limit`` and
    records a note in ``errors``.
    """
    errors = errors if errors is not None else {}
    terms = [t for t in _FS.split(query.strip()) if t]
    if not terms:
        return [], 0

    use_fts = _fts5_available(errors)
    if use_fts:
        return _search_fts(home, terms, limit, offset)
    errors.setdefault("search", "FTS5 unavailable, fell back to LIKE scan")
    return _search_like(home, terms, limit, offset, errors)


def _search_fts(home: Path, terms: list[str], limit: int, offset: int) -> tuple[list[str], int]:
    fts_expr = " AND ".join(matches for matches in (_escape_term(t) for t in terms) if matches)
    if not fts_expr:
        return [], 0
    con = _connect(home)
    try:
        _ensure_schema(con)
        total = int(con.execute(
            "SELECT COUNT(*) FROM runs_fts WHERE runs_fts MATCH ?", (fts_expr,)
        ).fetchone()[0])
        # bm25(runs_fts) only — no LEFT JOIN on ``indexed`` (kickoff r3 fix).
        rows = con.execute(
            """
            SELECT run_id FROM runs_fts
            WHERE runs_fts MATCH ?
            ORDER BY bm25(runs_fts)
            LIMIT ? OFFSET ?
            """,
            (fts_expr, int(limit), int(offset)),
        ).fetchall()
        return [r[0] for r in rows], total
    finally:
        con.close()


def _search_like(home: Path, terms: list[str], limit: int, offset: int,
                 errors: dict[str, str]) -> tuple[list[str], int]:
    """LIKE fallback — scans every run's kickoff text + title + recipe.

    The list is small enough (a few hundred at most) and the regex match
    is straightforward; this is the degraded mode, not the steady-state path.
    """
    from mini_ork.acp.history import _read_kickoff, _is_safe_token, _title_from_kickoff

    state_db = Path(home) / "state.db"
    if not state_db.is_file():
        errors.setdefault("search", "no state.db — LIKE scan skipped")
        return [], 0
    try:
        con = sqlite3.connect(str(state_db), timeout=5)
        try:
            rows = con.execute(
                "SELECT id, recipe, kickoff_path FROM task_runs ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        errors["search"] = f"LIKE scan failed: {exc}"
        return [], 0

    needles = [_like_term(t).lower() for t in terms]
    needles = [t for t in needles if t]
    if not needles:
        return [], 0
    matches: list[tuple[float, str]] = []
    for rid, recipe, kpath in rows:
        if not isinstance(rid, str) or not _is_safe_token(rid):
            continue
        text = _read_kickoff(Path(home), rid, kpath if isinstance(kpath, str) else None)
        title = _title_from_kickoff(text)
        haystack = f"{title}\n{recipe or ''}\n{text}".lower()
        if all(n in haystack for n in needles):
            try:
                ts = (Path(home) / "runs" / rid).stat().st_mtime
            except OSError:
                ts = 0.0
            matches.append((ts, rid))
    matches.sort(key=lambda x: x[0], reverse=True)
    total = len(matches)
    page = matches[int(offset): int(offset) + int(limit)]
    return [rid for _, rid in page], total


__all__ = ["reindex", "search"]


# ── background builder ────────────────────────────────────────────────────────
#
# One builder at a time, enforced with ``flock`` on ``state/ide-search.lock``:
# the kernel drops the lock when the builder exits or is killed, so there is no
# pid bookkeeping and no stale lock to clean up.


def _lock_path(home: Path) -> Path:
    return Path(home) / _STATE_DIR / _LOCK_NAME


def _log_path(home: Path) -> Path:
    return Path(home) / _STATE_DIR / _LOG_NAME


def _try_lock(home: Path) -> Any:
    """The locked file object, or ``None`` when another builder holds it."""
    import fcntl

    path = _lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _builder_running(home: Path) -> bool:
    handle = _try_lock(home)
    if handle is None:
        return True
    handle.close()  # closing releases the probe's lock
    return False


def _spawn_indexer(args: list[str], *, log_handle) -> subprocess.Popen[Any]:
    """Spawn the detached builder (own session, so it outlives this CLI).

    Module-level so tests can replace it.
    """
    return subprocess.Popen(
        args,
        start_new_session=True,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )


def _maybe_spawn_builder(home: Path) -> None:
    """Start the background builder unless one is already running.

    Two callers racing past the probe both spawn; the second builder finds
    the lock taken and exits at once, so at most one ever indexes.
    """
    if _builder_running(home):
        return
    log_path = _log_path(home)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "ab", buffering=0)
    try:
        _spawn_indexer(
            [sys.executable, "-m", "mini_ork.ide_pages.search", "--home", str(home), "--build"],
            log_handle=log_handle,
        )
    finally:
        log_handle.close()


def _run_build(home: Path) -> int:
    """Index every out-of-date run in batches, then exit.

    Holds the ``flock`` for its whole life (another builder → returns 0 at
    once). ``os.nice(10)`` plus a sleep between batches keeps it under
    ram-sentinel's CPU guard; each batch commits, so a kill leaves a usable
    partial index. Uses the same work list as :func:`reindex`, so stale runs
    are refreshed too. Returns the number of runs indexed.
    """
    lock = _try_lock(home)
    if lock is None:
        return 0
    try:
        try:
            os.nice(10)
        except OSError:
            pass
        started = time.monotonic()
        done = 0
        while True:
            task_rows = _task_runs_rows(home)
            con = _connect(home)
            try:
                _ensure_schema(con)
                batch = _work_list(con, task_rows)[:_BUILD_BATCH] if task_rows else []
                for run_id, kpath, updated_at in batch:
                    _index_one(con, home, run_id, kpath, updated_at)
                if batch:
                    con.commit()
            finally:
                con.close()
            done += len(batch)
            if len(batch) < _BUILD_BATCH:
                break
            time.sleep(_BUILD_SLEEP_S)
        try:
            with open(_log_path(home), "a", encoding="utf-8") as fh:
                fh.write(f"build done: indexed={done} elapsed={time.monotonic() - started:.1f}s\n")
        except OSError:
            pass
        return done
    finally:
        lock.close()


if __name__ == "__main__":  # pragma: no cover — exercised via tests w/ tmp home
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m mini_ork.ide_pages.search",
        description="Build the IDE search index in the background.",
    )
    parser.add_argument("--home", required=True, help="MINI_ORK_HOME")
    parser.add_argument(
        "--build",
        action="store_true",
        help="Run the detached background indexer loop and exit.",
    )
    args = parser.parse_args()
    home_path = Path(args.home).expanduser().absolute()
    if args.build:
        sys.exit(0 if _run_build(home_path) >= 0 else 1)
    parser.error("no action requested (use --build)")