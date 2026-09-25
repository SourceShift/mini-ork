"""TraceStore CRUD on execution_traces — Python port of lib/trace_store.sh (Tier A).

The bash version already delegated every function to an embedded python heredoc,
so this is a faithful extraction into an importable module. reward_g is
direction-normalized: dir*(value-anchor)/abs(anchor); anchor==0 → None (a
deliberate "unanchored baseline, no learning signal"). This is the write path
the GRPO loop reads via lane_router, and where win #1's reward stamp lands.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

from mini_ork.context import context_env


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if not env:
        raise RuntimeError("MINI_ORK_DB unset")
    return env


def compute_reward_g(value, anchor, direction: str) -> float | None:
    """Direction-normalized, scale-free gain. anchor==0 → None (no signal)."""
    if value is None or anchor is None:
        return None
    try:
        v = float(value)
        a = float(anchor)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    sign = 1.0 if direction == "higher_is_better" else -1.0
    return sign * (v - a) / abs(a)


def _normalise_verifier_output(v) -> dict:
    """Decode once; handles new dict-from-execute AND legacy double-encoded rows."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s or s in ("null", "None"):
            return {}
        try:
            decoded = json.loads(s)
        except (ValueError, TypeError):
            return {}
        if isinstance(decoded, dict):
            return decoded
        if isinstance(decoded, str):
            try:
                redecoded = json.loads(decoded)
            except (ValueError, TypeError):
                return {}
            return redecoded if isinstance(redecoded, dict) else {}
        return {}
    return {}


# Every execution_traces column this writer knows about, in INSERT order, each
# paired with how the ON CONFLICT clause treats it:
#   "skip"     — write on insert, never overwrite on conflict
#   "set"      — overwrite on conflict
#   "coalesce" — overwrite only when the incoming value is non-NULL
# A column the live table lacks is dropped from both lists rather than raising:
# the router columns (route_*, predicted_error) arrive under later migrations, and
# a trace write must never be the thing that fails when the schema is behind.
_TRACE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trace_id", "skip"),
    ("run_id", "coalesce"),
    ("task_class", "skip"),
    ("prompt_version_hash", "skip"),
    ("context_bundle_hash", "skip"),
    ("tool_calls", "skip"),
    ("files_read", "skip"),
    ("files_written", "skip"),
    ("verifier_output", "set"),
    ("reviewer_verdict", "set"),
    ("cost_usd", "set"),
    ("duration_ms", "set"),
    ("final_artifact_ref", "set"),
    ("status", "set"),
    ("workflow_version_id", "skip"),
    ("agent_version_id", "skip"),
    ("objective_domain", "set"),
    ("segment", "set"),
    ("reward_primary_metric", "set"),
    ("reward_direction", "set"),
    ("reward_value", "set"),
    ("reward_anchor", "set"),
    ("reward_g", "set"),
    ("reward_vector_json", "set"),
    ("reward_source", "set"),
    ("validity", "set"),
    ("route_source", "coalesce"),
    ("route_explore", "coalesce"),
    ("route_score", "coalesce"),
    ("route_margin", "coalesce"),
    ("predicted_error", "coalesce"),
)


def _trace_values(
    p: dict, verifier_output_obj: dict, reward_vector_json: str | None,
    reward_direction: str, reward_value, reward_anchor, reward_g,
    trace_id: str, run_id, workflow_version_id, prompt_version,
) -> dict[str, object]:
    """Map a payload onto execution_traces columns, reproducing the historical
    coercion exactly (json-encode the list/dict columns, float the numerics,
    bool→int for route_explore)."""
    return {
        "trace_id": trace_id,
        "run_id": run_id,
        "task_class": p.get("task_class", ""),
        "prompt_version_hash": prompt_version,
        "context_bundle_hash": p.get("context_bundle_hash", "") or "",
        "tool_calls": json.dumps(p.get("tool_calls", [])),
        "files_read": json.dumps(p.get("files_read", [])),
        "files_written": json.dumps(p.get("files_written", [])),
        "verifier_output": json.dumps(verifier_output_obj),
        "reviewer_verdict": p.get("reviewer_verdict"),
        "cost_usd": float(p.get("cost_usd", 0.0)),
        "duration_ms": int(p.get("duration_ms", 0)),
        "final_artifact_ref": p.get("final_artifact_ref"),
        "status": p.get("status", "success"),
        "workflow_version_id": workflow_version_id,
        "agent_version_id": p.get("agent_version_id", "") or "",
        "objective_domain": p.get("objective_domain") or "code-delivery",
        "segment": p.get("segment") or p.get("task_class") or None,
        "reward_primary_metric": p.get("reward_primary_metric"),
        "reward_direction": reward_direction,
        "reward_value": float(reward_value) if reward_value is not None else None,
        "reward_anchor": float(reward_anchor) if reward_anchor is not None else None,
        "reward_g": float(reward_g) if reward_g is not None else None,
        "reward_vector_json": reward_vector_json,
        "reward_source": p.get("reward_source") or "verifier@v1",
        "validity": p.get("validity") or "valid",
        "route_source": p.get("route_source"),
        "route_explore": (
            int(bool(p["route_explore"])) if p.get("route_explore") is not None else None
        ),
        "route_score": (
            float(p["route_score"]) if p.get("route_score") is not None else None
        ),
        "route_margin": (
            float(p["route_margin"]) if p.get("route_margin") is not None else None
        ),
        "predicted_error": (
            float(p["predicted_error"]) if p.get("predicted_error") is not None else None
        ),
    }


def _trace_upsert(con: sqlite3.Connection, values: dict[str, object]) -> None:
    """INSERT/UPDATE only the columns the live table actually has.

    Fail-open on schema drift: the write path must degrade to a narrower write,
    never raise. calibration.load_margin_rows reads a missing column as "no data
    yet"; the writer treats it the same way rather than killing the row.
    """
    present = {row[1] for row in con.execute("PRAGMA table_info(execution_traces)")}
    if not present:
        raise sqlite3.OperationalError("no such table: execution_traces")
    cols = [c for c, _ in _TRACE_COLUMNS if c in present]
    conflict = ", ".join(
        f"{c}=excluded.{c}" if mode == "set" else f"{c}=COALESCE(excluded.{c}, {c})"
        for c, mode in _TRACE_COLUMNS
        if mode in ("set", "coalesce") and c in present
    )
    sql = (
        f"INSERT INTO execution_traces ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))}) "
        + (f"ON CONFLICT(trace_id) DO UPDATE SET {conflict}" if conflict
           else "ON CONFLICT(trace_id) DO NOTHING")
    )
    con.execute(sql, [values[c] for c in cols])


def trace_write(payload: dict | str, db: str | None = None) -> str:
    """Write/UPSERT an execution trace. Returns trace_id. Faithful to
    lib/trace_store.sh::trace_write (same columns, ON CONFLICT set, env fallbacks),
    except that columns the live table lacks are dropped instead of raising — see
    ``_trace_upsert``."""
    p = json.loads(payload) if isinstance(payload, str) else dict(payload)
    trace_id = p.get("trace_id") or f"tr-{uuid.uuid4().hex[:16]}"
    # agent_version_id arrives as the lane the router was ASKED for, which may be
    # an agents.yaml alias or a two-element retry family — `glm,minimax` is a
    # legitimate value there. Only the member that actually served can be
    # credited with the outcome, and the dispatcher stamps it into
    # .last-llm-lane. Stamped as the family, `glm,minimax` held an advantage row
    # of its own and competed with its own members (`glm`, `minimax`) for the
    # same slice. The node gate in _read_fresh_sidecar keeps this from reaching
    # a node that dispatched nothing.
    served_lane = _read_fresh_sidecar(".last-llm-lane")
    if served_lane:
        p["agent_version_id"] = served_lane
    run_id = (p.get("run_id") or os.environ.get("MINI_ORK_TASK_RUN_ID")
              or context_env("MINI_ORK_RUN_ID") or None)
    workflow_version_id = (p.get("workflow_version_id")
                           or os.environ.get("MINI_ORK_WORKFLOW_VERSION_ID"))
    prompt_version = (p.get("prompt_version") or os.environ.get("MO_NODE_PROMPT_SHA") or "")

    verifier_output_obj = _normalise_verifier_output(p.get("verifier_output", {}))
    reward_value = p.get("reward_value")
    reward_anchor = p.get("reward_anchor")
    reward_direction = p.get("reward_direction") or "higher_is_better"
    reward_g_explicit = p.get("reward_g")
    reward_g = (reward_g_explicit if reward_g_explicit is not None
                else compute_reward_g(reward_value, reward_anchor, reward_direction))
    reward_vector = p.get("reward_vector")
    if isinstance(reward_vector, dict):
        reward_vector_json = json.dumps(reward_vector)
    elif isinstance(reward_vector, str):
        reward_vector_json = reward_vector
    else:
        reward_vector_json = None

    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    _trace_upsert(
        con,
        _trace_values(
            p, verifier_output_obj, reward_vector_json, reward_direction,
            reward_value, reward_anchor, reward_g, trace_id, run_id,
            workflow_version_id, prompt_version,
        ),
    )
    con.commit()
    con.close()
    return trace_id


def _read_sidecar_text(run_dir: str, name: str, freshness_s: float) -> str | None:
    path = os.path.join(run_dir, name)
    try:
        if time.time() - os.stat(path).st_mtime > freshness_s:
            return None
    except OSError:
        return None
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_fresh_sidecar(name: str) -> str | None:
    """Freshness- AND node-gated sidecar read from $MINI_ORK_RUN_DIR.

    The dispatch sidecars are overwritten, never consumed, so two gates are
    needed to keep one dispatch's numbers off another node's trace:

      * freshness (5x MO_DISPATCH_TIMEOUT) drops a lane/cost left by an earlier
        node in the same run;
      * the node stamp (.last-llm-node) drops one left by a node that DID
        dispatch while the current node did not.

    The second gate is why a deterministic node — a transform, a scaffold
    implementer that early-returns — no longer inherits the previous dispatch's
    cost: every such node carried the same ~$0.17 line, over-counting a single
    call several times over. A writer that stamps no node (an older dispatcher,
    a test fixture) is trusted on freshness alone, so the gate narrows
    attribution without being able to blank it out."""
    run_dir = context_env("MINI_ORK_RUN_DIR", "")
    if not run_dir:
        return None
    try:
        timeout = int(os.environ.get("MO_DISPATCH_TIMEOUT", "1500"))
    except ValueError:
        timeout = 1500
    freshness_s = 5 * timeout
    owner = _read_sidecar_text(run_dir, ".last-llm-node", freshness_s)
    node = os.environ.get("MO_NODE_ID", "")
    if owner and node and owner != node:
        return None
    return _read_sidecar_text(run_dir, name, freshness_s)


def enrich_stage_trace(payload: dict, *, node_type: str, verdict: str = "") -> dict:
    """Stamp node_type, lane, and the status-anchored reward on a pipeline-stage
    trace payload (classify/plan/verify/reflect). Stage writers used to emit
    bare status rows — reward-less AND lane-less — so every completed stage
    trace was invisible to both advantage writebacks (they filter on
    agent_version_id <> '' and reward_g IS NOT NULL) and landed as
    node_type='unknown' when they did appear.

      * node_type keeps lane_router's grouping from lumping every stage into
        one 'unknown' bucket.
      * lane comes from the .last-llm-lane sidecar llm_dispatch writes after a
        stage's LLM call; deterministic stages (no dispatch) simply get none.
      * reward reuses reward_from_status (MO_REWARD_STAMP-gated) so stage rows
        live on the same [-1,+1] reward_g scale as node traces.

    Mutates and returns ``payload`` for inline use."""
    p = payload
    vo = p.get("verifier_output")
    if not isinstance(vo, dict):
        vo = {}
    vo.setdefault("node_type", node_type or "unknown")
    p["verifier_output"] = vo
    if not p.get("agent_version_id"):
        lane = _read_fresh_sidecar(".last-llm-lane")
        if lane:
            p["agent_version_id"] = lane
    if (p.get("reward_value") is None
            and os.environ.get("MO_REWARD_STAMP", "1") == "1"):
        from mini_ork.learning.writeback import reward_from_status
        rv = reward_from_status(str(p.get("status") or ""),
                                verdict or str(p.get("reviewer_verdict") or ""))
        try:
            p["reward_value"] = float(rv)
            p["reward_anchor"] = float(os.environ.get("MO_REWARD_ANCHOR", "0.5"))
            p["reward_direction"] = "higher_is_better"
        except (TypeError, ValueError):
            pass
    return p


def trace_write_node(task_class: str, status: str = "success",
                     extra: dict | None = None) -> dict:
    """Build a node-trace payload, enriched with cost/duration from the dispatch
    sidecars in $MINI_ORK_RUN_DIR (freshness-gated). Returns the payload dict."""
    extra = dict(extra or {})

    def _read_sidecar(name):
        return _read_fresh_sidecar(name)

    cost = 0.0
    c = _read_sidecar(".last-llm-cost")
    if c:
        try:
            cost = float(c)
        except ValueError:
            cost = 0.0
    duration_ms = 0
    d = _read_sidecar(".last-llm-duration-ms")
    if d:
        try:
            duration_ms = int(float(d))
        except ValueError:
            duration_ms = 0

    payload = {
        "trace_id": extra.get("trace_id") or f"tr-{uuid.uuid4().hex[:16]}",
        "task_class": task_class, "status": status,
        "cost_usd": cost, "duration_ms": duration_ms,
    }
    for k, v in extra.items():
        if k not in payload or payload[k] in (None, "", 0, 0.0):
            payload[k] = v
    return payload


def grade_run_reward(run_dir: str, run_id: str, db: str | None = None) -> int:
    """Close the eval loop (win #3): stamp the rubric's GRADED 0-8 run score as
    reward_g on the traces of this run that carry no per-node reward yet.
    Reads <run_dir>/rubric.json {score}, normalizes score/8 → [0,1] against a
    fixed neutral anchor 0.5 → reward_g in [-1,+1]. Returns rows updated.
    Best-effort: a missing/garbled rubric.json is a no-op (returns 0)."""
    if not run_id:
        return 0
    rubric_path = os.path.join(run_dir, "rubric.json")
    try:
        with open(rubric_path, encoding="utf-8") as fh:
            rubric = json.load(fh)
        score = float(rubric.get("score"))
    except (ValueError, TypeError, KeyError, OSError):
        return 0
    # A rubric that failed to parse writes {"parse_error": true, "score": -1}.
    # That -1 is a grader-failure SENTINEL, not a real 0/8 — normalizing it would
    # stamp reward_g=-1 on every trace of the run, punishing the whole run for the
    # grader's own failure and poisoning the GRPO signal. No-op instead.
    if rubric.get("parse_error") or score < 0:
        return 0
    val = max(0.0, min(1.0, score / 8.0))
    anchor = 0.5
    reward_g = (val - anchor) / abs(anchor)
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    # Fill-ONLY: a per-node reward already on the row (status-anchored stamp from
    # the trace_fn, or an eval@v1 score) encodes WITHIN-run lane differentiation;
    # overwriting every trace of the run with one uniform rubric value zeroes
    # lane_adv = lane_mean - group_mean across the run's lanes and freezes the
    # router at 0.0 (the exact starvation the 2026-07 LRA rows show). The rubric
    # grades only traces the per-node stamp missed.
    con.execute(
        "UPDATE execution_traces SET reward_value=?, reward_anchor=?, reward_g=?, "
        "reward_direction='higher_is_better', reward_primary_metric='rubric_score', "
        "reward_source='rubric@v1' WHERE run_id=? AND reward_g IS NULL",
        (val, anchor, reward_g, run_id),
    )
    n = con.total_changes
    con.commit()
    con.close()
    return n


def trace_get(trace_id: str, db: str | None = None) -> dict | None:
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM execution_traces WHERE trace_id=?",
                      (trace_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def trace_query(task_class: str = "", status: str = "", since: int = 0,
                limit: int = 1000, db: str | None = None) -> list[dict]:
    import datetime
    try:
        since_iso = datetime.datetime.utcfromtimestamp(int(since)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
    except (ValueError, TypeError, OSError):
        since_iso = str(since)
    clauses, params = ["created_at >= ?"], [since_iso]
    if task_class:
        clauses.append("task_class = ?")
        params.append(task_class)
    if status:
        clauses.append("status = ?")
        params.append(status)
    sql = ("SELECT * FROM execution_traces WHERE " + " AND ".join(clauses)
           + " ORDER BY created_at DESC LIMIT ?")
    params.append(limit)
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]
