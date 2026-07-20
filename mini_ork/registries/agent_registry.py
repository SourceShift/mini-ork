"""AgentVersionRecord storage + performance stats — Python port of lib/agent_registry.sh.

Public API mirrors the bash four:
  register(role, payload) -> str          # version_id on stdout
  get(role, version_id) -> dict | None
  current(role) -> dict | None
  performance(role) -> dict

Schema DDL is owned by this module (lazy CREATE TABLE IF NOT EXISTS), matching
the bash source-of-truth in lib/agent_registry.sh. Strangler-fig: bash stays
in place; this port is additive under mini_ork/registries/ for downstream Python
callers. Float rounding uses Python's stdlib round() — same algorithm as the
bash heredoc which also shells out to `python3 -`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid


__all__ = ["register", "get", "current", "performance"]


SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS agent_registry (
        version_id           TEXT PRIMARY KEY,
        role                 TEXT NOT NULL,
        model                TEXT NOT NULL,
        provider             TEXT NOT NULL DEFAULT 'anthropic',
        tools                TEXT NOT NULL DEFAULT '[]',
        prompt_hash          TEXT,
        task_classes         TEXT NOT NULL DEFAULT '[]',
        cost_profile         TEXT DEFAULT '{}',
        context_window       INTEGER DEFAULT 200000,
        success_rate         REAL DEFAULT 0.0,
        known_failure_modes  TEXT NOT NULL DEFAULT '[]',
        status               TEXT NOT NULL DEFAULT 'active'
                                 CHECK(status IN ('active','retired','candidate')),
        registered_at        INTEGER NOT NULL
    );
"""


def _db() -> str:
    path = os.environ.get("MINI_ORK_DB")
    if not path:
        raise RuntimeError("MINI_ORK_DB unset")
    return path


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db())
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _ensure_schema() -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS — mirrors _agent_ensure_tables."""
    con = _connect()
    con.executescript(SCHEMA_SQL)
    con.commit()
    con.close()


def _coerce_json_list(value, default):
    """Replicate bash's: if string given, try json.loads; else coerce to []."""
    if value is None:
        return list(default)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else list(default)
        except Exception:
            return list(default)
    if isinstance(value, list):
        return value
    return list(default)


def _coerce_json_obj(value, default):
    """Replicate bash's: if string given, try json.loads; else coerce to {}."""
    if value is None:
        return dict(default)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else dict(default)
        except Exception:
            return dict(default)
    if isinstance(value, dict):
        return value
    return dict(default)


def register(role: str, payload) -> str:
    """agent_register: persist a new (or upsert an existing) version row for role.

    Returns the version_id string. payload may be a dict or a JSON string;
    mirrors the bash behavior (bash always JSON-decodes a string; we accept
    both). Raises ValueError on invalid JSON or missing 'model' key — same
    semantic as bash's sys.exit(1) with stderr prefix.
    """
    if not role:
        raise ValueError("role required")
    if isinstance(payload, str):
        try:
            p = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ValueError(f"agent_register: invalid JSON: {e}") from None
    elif isinstance(payload, dict):
        p = payload
    else:
        raise ValueError(
            f"agent_register: invalid JSON: expected dict or string, got {type(payload).__name__}"
        )

    if not p.get("model"):
        raise ValueError("agent_register: 'model' required in payload")

    vid = p.get("version_id") or f"av-{role[:6]}-{uuid.uuid4().hex[:10]}"
    now = int(time.time())

    tools = _coerce_json_list(p.get("tools"), [])
    task_classes = _coerce_json_list(p.get("task_classes"), [])
    known_failures = _coerce_json_list(p.get("known_failure_modes"), [])
    cost_profile = _coerce_json_obj(p.get("cost_profile"), {})

    _ensure_schema()
    con = _connect()
    try:
        con.execute(
            "UPDATE agent_registry SET status='retired' "
            "WHERE role=? AND status='active'",
            (role,),
        )
        con.execute(
            """
            INSERT INTO agent_registry (
                version_id, role, model, provider, tools, prompt_hash,
                task_classes, cost_profile, context_window, success_rate,
                known_failure_modes, status, registered_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(version_id) DO UPDATE SET
                model=excluded.model,
                provider=excluded.provider,
                tools=excluded.tools,
                prompt_hash=excluded.prompt_hash,
                task_classes=excluded.task_classes,
                cost_profile=excluded.cost_profile,
                context_window=excluded.context_window,
                known_failure_modes=excluded.known_failure_modes,
                status='active'
            """,
            (
                vid, role, p["model"],
                p.get("provider", "anthropic"),
                json.dumps(tools),
                p.get("prompt_hash"),
                json.dumps(task_classes),
                json.dumps(cost_profile),
                int(p.get("context_window", 200000)),
                float(p.get("success_rate", 0.0)),
                json.dumps(known_failures),
                p.get("status", "active"),
                now,
            ),
        )
        con.commit()
    finally:
        con.close()
    return vid


def get(role: str, version_id: str):
    """agent_get: return dict for (role, version_id) or None."""
    if not role or not version_id:
        return None
    _ensure_schema()
    con = _connect()
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM agent_registry WHERE role=? AND version_id=?",
            (role, version_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def current(role: str):
    """agent_current: return the active version dict for role or None."""
    if not role:
        return None
    _ensure_schema()
    con = _connect()
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM agent_registry WHERE role=? AND status='active' "
            "ORDER BY registered_at DESC LIMIT 1",
            (role,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def performance(role: str) -> dict:
    """agent_performance: aggregate stats joined on execution_traces.

    Mirrors bash: total_runs / success_runs / round(success/total, 4) /
    round(avg_cost or 0.0, 6) / round(avg_duration or 0.0, 1). Joins on
    execution_traces.agent_version_id; if the table is missing (try/except),
    falls back to the agent_registry.success_rate column with total=0.
    """
    if not role:
        return {"role": role, "version_count": 0, "versions": []}
    _ensure_schema()
    con = _connect()
    try:
        con.row_factory = sqlite3.Row
        versions = con.execute(
            "SELECT version_id, model, status, registered_at, success_rate "
            "FROM agent_registry WHERE role=?",
            (role,),
        ).fetchall()

        stats = []
        for v in versions:
            vid = v["version_id"]
            try:
                rows = con.execute(
                    """
                    SELECT
                        COUNT(*) as total_runs,
                        SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successes,
                        AVG(cost_usd) as avg_cost,
                        AVG(duration_ms) as avg_duration_ms
                    FROM execution_traces
                    WHERE agent_version_id=?
                    """,
                    (vid,),
                ).fetchone()
                total = rows[0] or 0
                success = rows[1] or 0
                succ_rate = (success / total) if total > 0 else float(v["success_rate"] or 0.0)
            except Exception:
                total, success, succ_rate = 0, 0, 0.0
                rows = None

            stats.append({
                "version_id": vid,
                "model": v["model"],
                "status": v["status"],
                "registered_at": v["registered_at"],
                "total_runs": total,
                "success_runs": success,
                "success_rate": round(succ_rate, 4),
                "avg_cost_usd": round(rows[2] or 0.0, 6) if rows else 0.0,
                "avg_duration_ms": round(rows[3] or 0.0, 1) if rows else 0.0,
            })

        return {
            "role": role,
            "version_count": len(stats),
            "versions": stats,
        }
    finally:
        con.close()


# CLI parity wrapper — bash shell-outs use this; parity tests can invoke it
# via subprocess if they want rc semantics. Mirrors bash's stderr prefix +
# sys.exit(1) contract for invalid JSON and missing 'model' key.
def _main(argv):
    if len(argv) < 2 or argv[1] not in {"register", "get", "current", "performance"}:
        print(f"usage: {argv[0]} <register|get|current|performance> ...", file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "register":
            vid = register(argv[2], argv[3])
            print(vid)
        elif cmd == "get":
            res = get(argv[2], argv[3])
            print(json.dumps(res) if res is not None else "null")
        elif cmd == "current":
            res = current(argv[2])
            print(json.dumps(res) if res is not None else "null")
        elif cmd == "performance":
            print(json.dumps(performance(argv[2])))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))