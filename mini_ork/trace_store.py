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


def trace_write(payload: dict | str, db: str | None = None) -> str:
    """Write/UPSERT an execution trace. Returns trace_id. Faithful to
    lib/trace_store.sh::trace_write (same columns, ON CONFLICT set, env fallbacks)."""
    p = json.loads(payload) if isinstance(payload, str) else dict(payload)
    trace_id = p.get("trace_id") or f"tr-{uuid.uuid4().hex[:16]}"
    run_id = (p.get("run_id") or os.environ.get("MINI_ORK_TASK_RUN_ID")
              or os.environ.get("MINI_ORK_RUN_ID"))
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
    con.execute(
        """INSERT INTO execution_traces (
            trace_id, run_id, task_class, prompt_version_hash, context_bundle_hash,
            tool_calls, files_read, files_written, verifier_output,
            reviewer_verdict, cost_usd, duration_ms, final_artifact_ref,
            status, workflow_version_id, agent_version_id,
            objective_domain, segment, reward_primary_metric, reward_direction,
            reward_value, reward_anchor, reward_g, reward_vector_json,
            reward_source, validity
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(trace_id) DO UPDATE SET
            status=excluded.status, run_id=COALESCE(excluded.run_id, run_id),
            verifier_output=excluded.verifier_output,
            reviewer_verdict=excluded.reviewer_verdict, cost_usd=excluded.cost_usd,
            duration_ms=excluded.duration_ms, final_artifact_ref=excluded.final_artifact_ref,
            objective_domain=excluded.objective_domain, segment=excluded.segment,
            reward_primary_metric=excluded.reward_primary_metric,
            reward_direction=excluded.reward_direction, reward_value=excluded.reward_value,
            reward_anchor=excluded.reward_anchor, reward_g=excluded.reward_g,
            reward_vector_json=excluded.reward_vector_json,
            reward_source=excluded.reward_source, validity=excluded.validity""",
        (
            trace_id, run_id, p.get("task_class", ""), prompt_version,
            p.get("context_bundle_hash", "") or "",
            json.dumps(p.get("tool_calls", [])), json.dumps(p.get("files_read", [])),
            json.dumps(p.get("files_written", [])), json.dumps(verifier_output_obj),
            p.get("reviewer_verdict"), float(p.get("cost_usd", 0.0)),
            int(p.get("duration_ms", 0)), p.get("final_artifact_ref"),
            p.get("status", "success"), workflow_version_id,
            p.get("agent_version_id", "") or "",
            p.get("objective_domain") or "code-delivery",
            p.get("segment") or p.get("task_class") or None,
            p.get("reward_primary_metric"), reward_direction,
            float(reward_value) if reward_value is not None else None,
            float(reward_anchor) if reward_anchor is not None else None,
            float(reward_g) if reward_g is not None else None,
            reward_vector_json, p.get("reward_source") or "verifier@v1",
            p.get("validity") or "valid",
        ),
    )
    con.commit()
    con.close()
    return trace_id


def trace_write_node(task_class: str, status: str = "success",
                     extra: dict | None = None) -> dict:
    """Build a node-trace payload, enriched with cost/duration from the dispatch
    sidecars in $MINI_ORK_RUN_DIR (freshness-gated). Returns the payload dict."""
    extra = dict(extra or {})
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    try:
        timeout = int(os.environ.get("MO_DISPATCH_TIMEOUT", "1500"))
    except ValueError:
        timeout = 1500
    freshness_s = 5 * timeout
    now = time.time()

    def _read_sidecar(name):
        if not run_dir:
            return None
        path = os.path.join(run_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            return None
        if now - st.st_mtime > freshness_s:
            return None
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return None

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
    reward_g on every trace of this run, instead of the binary verdict flatten.
    Reads <run_dir>/rubric.json {score}, normalizes score/8 → [0,1] against a
    fixed neutral anchor 0.5 → reward_g in [-1,+1]. Returns rows updated.
    Best-effort: a missing/garbled rubric.json is a no-op (returns 0)."""
    if not run_id:
        return 0
    rubric_path = os.path.join(run_dir, "rubric.json")
    try:
        with open(rubric_path, encoding="utf-8") as fh:
            score = float(json.load(fh).get("score"))
    except (ValueError, TypeError, KeyError, OSError):
        return 0
    val = max(0.0, min(1.0, score / 8.0))
    anchor = 0.5
    reward_g = (val - anchor) / abs(anchor)
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        "UPDATE execution_traces SET reward_value=?, reward_anchor=?, reward_g=?, "
        "reward_direction='higher_is_better', reward_primary_metric='rubric_score', "
        "reward_source='rubric@v1' WHERE run_id=?",
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
