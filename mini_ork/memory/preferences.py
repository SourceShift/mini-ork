"""Operator-set preferences and constraints.

Backed by ``user_preference_memory`` (db/migrations/0009_memory_namespaces.sql:109-121);
scoped by ``scope ∈ {global, task_class, workflow}`` with ``scope_target`` carrying the
class or workflow name (empty for global). Also surfaces read-only entries from the
legacy ``$MINI_ORK_HOME/config/{user_preferences,constraints}.json`` files via
``list_prefs`` / ``prefs_for`` so operators can see what the planner still ingests
through ``context_assembler.context_assemble``.

DB resolution mirrors ``mini_ork.memory.store._db_path()``: explicit ``db`` arg wins,
else ``$MINI_ORK_DB``, else ``$MINI_ORK_HOME/state.db``.

Public API (kickoff contract, ``kickoffs/auto/learn-prefs.md:34-56``):

    set_pref(key, value, *, scope="global", target="", db=None) -> dict
    remove_pref(key, *, scope="global", target="", db=None) -> bool
    list_prefs(*, db=None) -> list[dict]
    prefs_for(task_class, workflow="", *, db=None) -> list[dict]
    render_block(prefs) -> str
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from ..context import context_env


_USER_ID = "default"
_VALID_SCOPES = ("global", "task_class", "workflow")
_MAX_ENTRIES = 12
_MAX_VALUE_CHARS = 600


# ─── DB resolution (mirrors mini_ork.memory.store._db_path) ──────────────


def _resolve_db(db: str | None) -> str:
    if db:
        return db
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def _validate_scope(scope: str, target: str) -> None:
    """Enforce the table's CHECK + a stricter global/target rule.

    The DDL CHECK permits ``scope='global', scope_target='whatever'`` (no
    constraint), so the API layer enforces the cleaner rule that ``global``
    prefs carry an empty target. ``scope='role'`` is rejected here even
    though SQLite would accept it.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"invalid scope {scope!r}; allowed: {', '.join(_VALID_SCOPES)}"
        )
    if scope == "global" and target:
        raise ValueError("scope=global requires target=''")


# ─── set_pref / remove_pref ──────────────────────────────────────────────


def set_pref(key: str, value, *, scope: str = "global", target: str = "",
            db: str | None = None) -> dict:
    """Upsert a preference. Returns the stored row as a dict with ``source``.

    Raises ``ValueError`` for an invalid ``scope`` or a ``global`` pref carrying a
    non-empty ``target``. ``value`` is plain text stored as a JSON string so the
    same column holds strings, numbers, and small objects.
    """
    _validate_scope(scope, target)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    elif not value:
        value = ""
    db_path = _resolve_db(db)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO user_preference_memory "
            "(user_id, preference_key, preference_value, scope, scope_target, set_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, preference_key, scope, scope_target) DO UPDATE SET "
            "  preference_value=excluded.preference_value, "
            "  set_at=excluded.set_at",
            (_USER_ID, key, value, scope, target, _now_iso()),
        )
        con.commit()
    finally:
        con.close()
    return {
        "key": key, "value": value, "scope": scope, "target": target,
        "set_at": _now_iso(), "source": "db",
    }


def remove_pref(key: str, *, scope: str = "global", target: str = "",
                db: str | None = None) -> bool:
    """Delete a preference. Returns True when a row was removed."""
    _validate_scope(scope, target)
    db_path = _resolve_db(db)
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "DELETE FROM user_preference_memory "
            "WHERE user_id=? AND preference_key=? AND scope=? AND scope_target=?",
            (_USER_ID, key, scope, target),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


# ─── list_prefs (DB + legacy file read-only entries) ──────────────────────


def list_prefs(*, db: str | None = None) -> list[dict]:
    """DB rows plus read-only entries from legacy JSON files (when present).

    Legacy files (read-only, never written by this module):
      - ``$MINI_ORK_HOME/config/user_preferences.json``: top-level dict,
        each key→value becomes one entry.
      - ``$MINI_ORK_HOME/config/constraints.json``: ``constraints[]`` becomes
        ``{key: 'constraint-<i>', value: <text>, scope: 'global'}``.
    """
    out: list[dict] = []
    db_path = _resolve_db(db)
    if os.path.exists(db_path):
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute(
                "SELECT preference_key, preference_value, scope, scope_target, set_at "
                "FROM user_preference_memory WHERE user_id=? ORDER BY set_at ASC",
                (_USER_ID,),
            ).fetchall()
        finally:
            con.close()
        for key, val, scope, target, set_at in rows:
            out.append({
                "key": key, "value": val, "scope": scope, "target": target,
                "set_at": set_at, "source": "db",
            })

    cfg_dir = os.path.join(context_env("MINI_ORK_HOME", ".mini-ork"), "config")
    upath = os.path.join(cfg_dir, "user_preferences.json")
    if os.path.exists(upath):
        try:
            with open(upath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    out.append({
                        "key": k,
                        "value": v if isinstance(v, str) else json.dumps(v),
                        "scope": "global",
                        "target": "",
                        "set_at": "",
                        "source": f"file:{upath}",
                    })
        except (OSError, json.JSONDecodeError):
            pass

    cpath = os.path.join(cfg_dir, "constraints.json")
    if os.path.exists(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                data = json.load(f)
            for i, text in enumerate(data.get("constraints", []) or []):
                out.append({
                    "key": f"constraint-{i}",
                    "value": str(text),
                    "scope": "global",
                    "target": "",
                    "set_at": "",
                    "source": f"file:{cpath}",
                })
        except (OSError, json.JSONDecodeError):
            pass
    return out


# ─── prefs_for (operator rules reach the prompt node) ──────


def prefs_for(task_class: str, workflow: str = "",
              *, db: str | None = None) -> list[dict]:
    """Return prefs relevant to a node dispatch: globals first, then scoped.

    Ordering is stable: globals (set_at ASC), task_class-scoped (set_at ASC),
    workflow-scoped (set_at ASC). Caps: 12 entries total, 600 chars per value.
    Caps are applied per-row so the slice never truncates mid-row.
    """
    tc = task_class or ""
    rows = list_prefs(db=db)
    selected: list[dict] = []
    by_scope: dict[str, list[dict]] = {"global": [], "task_class": [], "workflow": []}
    for r in rows:
        s = r["scope"]
        if s == "global":
            by_scope["global"].append(r)
        elif s == "task_class" and r["target"] == tc:
            by_scope["task_class"].append(r)
        elif s == "workflow" and r["target"] == workflow:
            by_scope["workflow"].append(r)
    for scope_bucket in ("global", "task_class", "workflow"):
        for r in by_scope[scope_bucket]:
            if len(selected) >= _MAX_ENTRIES:
                break
            v = r["value"]
            if isinstance(v, str) and len(v) > _MAX_VALUE_CHARS:
                v = v[:_MAX_VALUE_CHARS]
            r2 = dict(r)
            r2["value"] = v
            selected.append(r2)
    return selected


# ─── render_block (prompt block for the LLM node) ────────────────────────


def render_block(prefs: list[dict]) -> str:
    """Format prefs as a fenced prompt block; empty string when no prefs.

    Shape (kickoff §"render_block"):
        --- Operator preferences and constraints (set by the user; follow them) ---
        - <value>   [scope: task_class=<x>]
        --- /operator preferences ---
    """
    if not prefs:
        return ""
    lines = [
        "--- Operator preferences and constraints (set by the user; follow them) ---",
    ]
    for p in prefs:
        value = str(p.get("value", "")).strip()
        if not value:
            continue
        if p.get("scope") == "global":
            lines.append(f"- {value}")
        else:
            target = p.get("target") or ""
            lines.append(f"- {value}   [scope: {p['scope']}={target}]")
    if len(lines) == 1:
        return ""
    lines.append("--- /operator preferences ---")
    return "\n".join(lines) + "\n"


# ─── internal helpers ────────────────────────────────────────────────────


def _now_iso() -> str:
    """ISO-8601 UTC, millisecond precision (matches migration default)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"