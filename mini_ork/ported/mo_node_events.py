"""Node-events emit — Python port of lib/mo_node_events.sh.

Faithful port of the run_events emission helpers used by mini-ork to power
the per-node DAG status view in the observability UI. The Python port gives
callers an in-process surface (no `python3` heredoc per call) and gives the
parity test a stable target to byte-diff against the live bash.

Co-existence model (strangler-fig): bash `lib/mo_node_events.sh` is the
authoritative source. This module mirrors its surfaces exactly. Parity is
enforced by `tests/unit/test_mo_node_events_py.py` (>=6 cases that drive
the LIVE bash subprocess against a temp DB seeded by `db/init.sh` and diff
the resulting `run_events` rows against the Python port byte-for-byte;
floats 1e-6 on integer epoch columns).

Schema citations:
  - `run_events` base table  — db/migrations/0016_recursive_orchestration.sql
      (event_id, run_id, parent_run_id, event_type, payload_json, created_at)
  - `run_events.finish_reason`  — db/migrations/0021_error_taxonomy_finish_reasons.sql
  - `run_events.last_heartbeat_at`  — db/migrations/0023_node_heartbeat_fuse.sql

Pipeline map (bash function → Python):
  _mo_now_ms              → _now_ms          (gdate → time.time_ns → seconds*1000)
  mo_node_emit            → mo_node_emit     (required-arg guards + schema-aware insert)
  mo_node_start           → mo_node_start    (model_lane convenience)
  mo_node_end             → mo_node_end      (build_extra_json + delegate)
  mo_emit_node_heartbeat  → mo_emit_node_heartbeat
  mo_node_emit_end_trap   → mo_node_emit_end_trap
        (signature drift: bash uses RETURN-trap + caller-scope vars; the port
        takes them as explicit args. Bash callers cannot 1:1 swap.)

Signature deltas vs bash (documented for callers):
  - `mo_node_emit_end_trap`: bash reads `_mo_run_id`, `_mo_node_start_ms`,
    `node_id`, `node_type`, `VERDICT`, `CONTEXT_FILE`, `IMPL_LOG`, `REVIEW_FILE`,
    `MO_NODE_FINISH_REASON` from the caller's scope (RETURN-trap pattern).
    Python has no caller scope, so the port takes them as explicit args. Bash
    also auto-resolves artifact via `CONTEXT_FILE → IMPL_LOG → REVIEW_FILE`
    chain; the port takes a single resolved `context_path` and the caller
    must perform the chain.
  - `_now_ms` is the public name of bash's `_mo_now_ms` (the leading
    underscore is dropped to match Python idioms; the bash function name
    is preserved in the docstring for grep parity).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from typing import Any

__all__ = [
    "mo_node_emit",
    "mo_node_start",
    "mo_node_end",
    "mo_emit_node_heartbeat",
    "mo_node_emit_end_trap",
    "_now_ms",
]


# Mirror of bash default for the 5th positional: `'{\}}'` in single-quoted bash
# produces the literal string `{}`. JSON-empty object.
_DEFAULT_EXTRA_JSON = "{}"


# Mirrors lib/mo_node_events.sh:56
def _resolve_db() -> str | None:
    """Return the state.db path the bash script would pick, or None if missing.

    Resolution order (mirrors bash line 56):
      $MINI_ORK_DB → $MINI_ORK_HOME/state.db → $(pwd)/.mini-ork/state.db
    Returns None when the resolved path does not exist (caller must mirror
    bash's silent no-op in `mo_node_emit`).
    """
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        return os.path.join(home, "state.db")
    return os.path.join(os.getcwd(), ".mini-ork", "state.db")


# Mirrors lib/mo_node_events.sh:_mo_now_ms (gdate → python3 → date+%%s*1000)
def _now_ms() -> int:
    """Portable millisecond timestamp.

    BSD `date` (macOS default) does NOT honor `%3N` — it silently emits the
    literal "N" instead of failing, which poisons arithmetic. Prefer GNU
    `gdate` (coreutils), fall back to `time.time_ns()`, then to seconds×1000
    as a last-resort 1s-resolution fallback. Same pattern as
    lib/llm-dispatch.sh::_mo_llm_now_ms.
    """
    gdate = subprocess.run(
        ["gdate", "+%s%3N"], capture_output=True, text=True
    )
    if gdate.returncode == 0 and gdate.stdout.strip():
        try:
            return int(gdate.stdout.strip())
        except ValueError:
            pass
    try:
        return time.time_ns() // 1_000_000
    except AttributeError:  # pragma: no cover (Py<3.7)
        return int(time.time() * 1000)


def _build_event_id(event_type: str, node_id: str, pid: int | None = None) -> str:
    """Mirror bash line 59::

        evt-${event_type}-${node_id}-$(date +%s%N 2>/dev/null || date +%s)-$$

    `date +%s%N` works on GNU `date` (ns resolution); BSD `date` swallows
    `%N` silently and emits `%s` (s resolution). The port uses
    `time.time_ns()` (ns) with `time.time()` (s) fallback. `pid` defaults to
    `os.getpid()` and mirrors bash's `$$` (current shell PID).
    """
    if pid is None:
        pid = os.getpid()
    try:
        suffix = f"{time.time_ns()}"
    except AttributeError:  # pragma: no cover (Py<3.7)
        suffix = f"{int(time.time())}"
    return f"evt-{event_type}-{node_id}-{suffix}-{pid}"


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """Mirror bash line 83: PRAGMA table_info(run_events) column-name set."""
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _default_extra_json() -> str:
    """Mirror bash default `$5` of `mo_node_emit` (the literal `{}`)."""
    return _DEFAULT_EXTRA_JSON


def _build_extra_json(
    duration_ms: int | str,
    verdict: str = "",
    artifact_path: str = "",
    finish_reason: str = "",
) -> str:
    """Mirror bash lines 161-170 heredoc: always include `duration_ms` as int;
    include verdict/artifact_path/finish_reason only when truthy. Output is
    a JSON string (not a dict) so callers can pass it as `extra_json` to
    `mo_node_emit`.
    """
    out: dict[str, Any] = {"duration_ms": int(duration_ms or 0)}
    if verdict:
        out["verdict"] = verdict
    if artifact_path:
        out["artifact_path"] = artifact_path
    if finish_reason:
        out["finish_reason"] = finish_reason
    return json.dumps(out)


def mo_node_emit(
    run_id: str,
    node_id: str,
    node_type: str,
    event_type: str,
    extra_json: str = _DEFAULT_EXTRA_JSON,
) -> int:
    """Mirror lib/mo_node_events.sh::mo_node_emit (lines 45-103).

    Required-arg guards emit the same stderr phrase as bash and return 0
    (mirroring the bash `return 0` on guard miss). DB-missing is a silent
    no-op. Schema-aware insert includes `finish_reason` and
    `last_heartbeat_at` columns only when present in `run_events`. The
    `last_heartbeat_at` write is further gated to `event_type in
    ('node_start', 'node_heartbeat')` per migration 0023 semantics.
    """
    if not run_id:
        print("mo_node_emit: run_id required", file=__import__("sys").stderr)
        return 0
    if not node_id:
        print("mo_node_emit: node_id required", file=__import__("sys").stderr)
        return 0
    if not event_type:
        print("mo_node_emit: event_type required", file=__import__("sys").stderr)
        return 0

    db = _resolve_db()
    if not db or not os.path.isfile(db):
        return 0  # silent no-op if state.db missing (e.g. uninitialized test)

    # Mirror bash lines 67-78: parse extra_json, coerce non-dict, recover
    # JSONDecodeError to `_raw` envelope.
    try:
        extra = json.loads(extra_json) if extra_json else {}
        if not isinstance(extra, dict):
            extra = {"_raw": str(extra)}
    except json.JSONDecodeError:
        extra = {"_raw": extra_json}

    payload = {
        "node_id": node_id,
        "node_type": node_type,
        **extra,
    }

    event_id = _build_event_id(event_type, node_id)
    now_s = int(time.time())
    heartbeat_ms = _now_ms()

    con = sqlite3.connect(db, timeout=2.0)
    try:
        con.execute("PRAGMA busy_timeout = 2000")
        cols = _table_columns(con, "run_events")
        insert_cols = ["event_id", "run_id", "event_type", "payload_json", "created_at"]
        values: list[Any] = [event_id, run_id, event_type, json.dumps(payload), now_s]
        if "finish_reason" in cols:
            insert_cols.append("finish_reason")
            values.append(payload.get("finish_reason"))
        if "last_heartbeat_at" in cols and event_type in ("node_start", "node_heartbeat"):
            insert_cols.append("last_heartbeat_at")
            values.append(heartbeat_ms)
        placeholders = ",".join("?" for _ in insert_cols)
        con.execute(
            f"INSERT INTO run_events({', '.join(insert_cols)}) VALUES ({placeholders})",
            values,
        )
        con.commit()
    except sqlite3.Error:
        # Mirror bash `2>/dev/null || true` — observability must never break
        # execution. DB-missing/missing-table is the only no-op case we
        # return 0 for above; once we open a connection, schema errors are
        # surfaced as sqlite3.Error and swallowed to match bash's
        # best-effort observability contract.
        return 0
    finally:
        con.close()
    return 0


def mo_node_start(
    run_id: str,
    node_id: str,
    node_type: str,
    model_lane: str = "",
) -> int:
    """Mirror lib/mo_node_events.sh::mo_node_start (lines 107-114).

    Builds `extra = {"model_lane": <lane>}` when non-empty; otherwise passes
    the default `'{}'`. Delegates to `mo_node_emit` with `event_type='node_start'`.
    """
    extra = _default_extra_json()
    if model_lane:
        extra = json.dumps({"model_lane": model_lane})
    return mo_node_emit(run_id, node_id, node_type, "node_start", extra)


def mo_node_end(
    run_id: str,
    node_id: str,
    node_type: str,
    duration_ms: int | str,
    verdict: str = "",
    artifact_path: str = "",
    finish_reason: str = "",
) -> int:
    """Mirror lib/mo_node_events.sh::mo_node_end (lines 157-172).

    Builds the extra JSON via the same logic as the bash in-here python
    (`duration_ms` always; verdict/artifact_path/finish_reason only when
    truthy), then delegates to `mo_node_emit` with `event_type='node_end'`.
    """
    extra = _build_extra_json(duration_ms, verdict, artifact_path, finish_reason)
    return mo_node_emit(run_id, node_id, node_type, "node_end", extra)


def mo_emit_node_heartbeat(node_id: str, run_id: str) -> int:
    """Mirror lib/mo_node_events.sh::mo_emit_node_heartbeat (lines 118-122).

    `node_type` defaults to `$MO_NODE_TYPE` (env override) or the literal
    `'heartbeat'`. `extra_json` is always the empty-object literal `'{}'`.
    """
    node_type = os.environ.get("MO_NODE_TYPE", "heartbeat")
    return mo_node_emit(run_id, node_id, node_type, "node_heartbeat", _default_extra_json())


def mo_node_emit_end_trap(
    _run_id: str,
    node_id: str,
    node_type: str,
    start_ms: int,
    rc: int,
    *,
    context_path: str = "",
    verdict: str = "",
    finish_reason: str = "",
) -> int:
    """Mirror lib/mo_node_events.sh::mo_node_emit_end_trap (lines 131-153).

    Signature drift vs bash: the bash function reads caller-scope vars
    (`_mo_run_id`, `_mo_node_start_ms`, `node_id`, `node_type`, `VERDICT`,
    `CONTEXT_FILE`, `IMPL_LOG`, `REVIEW_FILE`, `MO_NODE_FINISH_REASON`) via
    the RETURN-trap pattern. Python has no caller scope, so callers must
    thread them explicitly. Artifact resolution is collapsed to a single
    `context_path` arg; the caller must perform the bash
    `CONTEXT_FILE → IMPL_LOG → REVIEW_FILE` chain before invoking.

    `finish_reason` defaulting mirrors bash lines 141-147: rc==0 → 'done',
    rc!=0 → 'error', else the env-override `MO_NODE_FINISH_REASON` (which
    the caller is expected to have already inlined into the `finish_reason`
    arg — the port does not re-read the env to keep the surface explicit).

    Early-return on empty `_run_id` / `node_id` / `node_type` (mirrors bash
    lines 134-136). OTel piggyback (bash line 150) is intentionally NOT
    ported — it depends on `lib/mo_otel.sh` which has no Python counterpart
    in this port.
    """
    if not _run_id or not node_id or not node_type:
        return 0

    end_ms = _now_ms()
    duration_ms = max(0, end_ms - start_ms)
    artifact = context_path

    if not finish_reason:
        finish_reason = "done" if rc == 0 else "error"

    return mo_node_end(_run_id, node_id, node_type, duration_ms, verdict, artifact, finish_reason)
