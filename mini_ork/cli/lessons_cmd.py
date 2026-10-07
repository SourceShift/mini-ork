"""``mini-ork lessons`` — list, forget and restore learned rules.

A "learned rule" is an ``emergent_patterns`` row that cleared the judge gate
(``status='approved'``) and carries an authored ``lesson_text``; only those are
read back into agent prompts (``context_assembler``). This verb is the
operator's control surface over them:

    lessons list [--json]
    lessons forget <pattern_id>
    lessons restore <pattern_id>

``forget`` sets ``status='rejected'`` (``resolved_at`` = now) so the lesson
stops being injected. ``restore`` returns a ``rejected`` row to ``approved``,
and only when it still carries a non-blank lesson — restoring a lessonless row
would inject a frequency count, exactly the confabulation the judge gate exists
to stop (``reflection_verify_patterns``).

Exit codes: 0 on success, 2 on a usage error or an unknown id (message on
stderr). The module mirrors ``prefs_cmd``: a bad id is a real mistake the
operator should see as a non-gate, so it is not swallowed into a 0.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Any

_ACTIONS = ("list", "forget", "restore")
_LIST_STATUSES = ("approved", "proposed")


def _resolve_db(db: str | None = None) -> str:
    """Resolve the state DB: explicit arg, then ``MINI_ORK_DB``, then
    ``$MINI_ORK_HOME/state.db``, then ``.mini-ork/state.db`` (mirrors
    ``mini_ork.memory.preferences._resolve_db``)."""
    if db:
        return db
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def _usage() -> str:
    return (
        "Usage: mini-ork lessons <action> [args]\n"
        "\n"
        "List, forget and restore learned rules (verified emergent patterns).\n"
        "\n"
        "Actions:\n"
        "  lessons list               Approved + proposed learned rules\n"
        "  lessons forget <id>        Stop giving this lesson to agents\n"
        "  lessons restore <id>       Return a forgotten lesson to the agents\n"
        "\n"
        "Options:\n"
        "  --json                     Emit JSON instead of text (list)\n"
        "  --home <dir>               Override $MINI_ORK_HOME for DB resolution\n"
        "  --help                     This message\n"
    )


def _parse(argv: list[str]) -> dict:
    """Return a dict of parsed options; ``error`` key on usage error."""
    opts = {
        "action": None,
        "positional": [],
        "home": "",
        "json": False,
        "help": False,
        "error": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            opts["help"] = True
            return opts
        if a == "--json":
            opts["json"] = True
            i += 1
            continue
        if a == "--home":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --home"
                return opts
            opts["home"] = argv[i + 1]
            i += 2
            continue
        if a in _ACTIONS:
            opts["action"] = a
            i += 1
            continue
        opts["positional"].append(a)
        i += 1
    return opts


def _connect(db: str) -> sqlite3.Connection | None:
    """Open the DB read/write; ``None`` when the file does not exist.

    A missing file is not conjured (mirrors ``ledger._open``): the caller
    reports an empty list/addressing error rather than bootstrapping a DB.
    """
    if not os.path.exists(db):
        return None
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(str(r[1]) == column for r in con.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def _seen_in_runs(con: sqlite3.Connection, members_json: Any) -> int:
    """Distinct runs behind a pattern's member traces (0 when unresolvable)."""
    try:
        members = json.loads(members_json) if members_json else []
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(members, list):
        return 0
    ids: list[str] = []
    for m in members:
        if isinstance(m, dict):
            if (m.get("item_table") or "execution_traces") != "execution_traces":
                continue
            mid = m.get("item_id")
        else:
            mid = m
        if mid:
            ids.append(str(mid))
    distinct = sorted(set(ids))
    if not distinct:
        return 0
    placeholders = ",".join("?" for _ in distinct)
    try:
        row = con.execute(
            f"SELECT COUNT(DISTINCT NULLIF(TRIM(COALESCE(run_id,'')),'')) "  # noqa: S608 — placeholders
            f"FROM execution_traces WHERE trace_id IN ({placeholders})",
            distinct,
        ).fetchone()
    except sqlite3.OperationalError:
        return len(distinct)
    return int(row[0] or 0) if row else 0


def _rows_for_list(con: sqlite3.Connection) -> list[dict]:
    has_lesson = _has_column(con, "emergent_patterns", "lesson_text")
    cols = ("pattern_id, cluster_label, member_item_ids_json, strength_score, status"
            + (", lesson_text" if has_lesson else ""))
    try:
        raw = con.execute(
            f"SELECT {cols} FROM emergent_patterns "  # noqa: S608 — fixed cols
            "WHERE status IN (?,?)",
            _LIST_STATUSES,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict] = []
    for r in raw:
        lesson = (str(r[5] or "") if has_lesson else "").strip()
        out.append({
            "pattern_id": str(r[0]),
            "cluster_label": str(r[1] or ""),
            "lesson": lesson,
            "strength": float(r[3] or 0),
            "status": str(r[4] or "proposed"),
            "seen_in_runs": _seen_in_runs(con, r[2]),
        })
    # Verified (approved) first, strongest first; proposed rows after.
    out.sort(key=lambda d: (d["status"] != "approved", -d["strength"], d["pattern_id"]))
    return out


def _emit_table(rows: list[dict], out) -> None:
    out.write(f"{'status':<10} {'seen':<5} {'lesson'}\n")
    for r in rows:
        lesson = r["lesson"] or f"{r['cluster_label']} (no lesson)"
        out.write(f"{r['status']:<10} {r['seen_in_runs']:<5} {lesson[:100]}\n")


def _do_list(con: sqlite3.Connection | None, as_json: bool, out) -> int:
    rows = _rows_for_list(con) if con is not None else []
    if as_json:
        out.write(json.dumps(rows, ensure_ascii=False) + "\n")
    else:
        _emit_table(rows, out)
    return 0


def _row_status(con: sqlite3.Connection, pid: str) -> tuple[str, str] | None:
    """``(status, lesson_text)`` for ``pid``; ``None`` when the row is absent."""
    has_lesson = _has_column(con, "emergent_patterns", "lesson_text")
    cols = "status" + (", lesson_text" if has_lesson else "")
    try:
        row = con.execute(
            f"SELECT {cols} FROM emergent_patterns WHERE pattern_id = ?", (pid,)  # noqa: S608 — fixed cols
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return str(row[0] or ""), (str(row[1] or "") if has_lesson else "")


def _do_forget(con: sqlite3.Connection, pid: str, err) -> int:
    if _row_status(con, pid) is None:
        err.write(f"no such lesson: {pid}\n")
        return 2
    con.execute(
        "UPDATE emergent_patterns SET status='rejected', resolved_at=? "
        "WHERE pattern_id=? AND status IN ('approved','proposed')",
        (int(time.time()), pid),
    )
    con.commit()
    return 0


def _do_restore(con: sqlite3.Connection, pid: str, err) -> int:
    status = _row_status(con, pid)
    if status is None:
        err.write(f"no such lesson: {pid}\n")
        return 2
    _status, lesson = status
    if not lesson.strip():
        err.write(
            f"cannot restore {pid}: it has no authored lesson — restoring it would "
            "inject a frequency count, not guidance\n"
        )
        return 2
    if _status != "rejected":
        err.write(f"cannot restore {pid}: it is not forgotten (status={_status!r})\n")
        return 2
    con.execute(
        "UPDATE emergent_patterns SET status='approved', resolved_at=? "
        "WHERE pattern_id=? AND status='rejected'",
        (int(time.time()), pid),
    )
    con.commit()
    return 0


def main(argv=None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    opts = _parse(sys.argv[1:] if argv is None else argv)

    if opts["help"]:
        out.write(_usage())
        return 0

    if opts["error"]:
        err.write(f"{opts['error']}\n")
        out.write(_usage())
        return 2

    # ``--home`` overrides $MINI_ORK_HOME for DB resolution (the IDE appends
    # ``--home <home>`` to every cli action).
    if opts["home"]:
        os.environ["MINI_ORK_HOME"] = opts["home"]

    action = opts["action"]
    if action is None:
        if opts["positional"]:
            err.write(f"unknown sub-action: {opts['positional'][0]}\n")
        else:
            err.write("missing sub-action\n")
        out.write(_usage())
        return 2

    if action == "list":
        con = _connect(_resolve_db())
        try:
            return _do_list(con, opts["json"], out)
        finally:
            if con is not None:
                con.close()

    pos = opts["positional"]
    if len(pos) != 1:
        err.write(f"usage: lessons {action} <pattern_id>\n")
        out.write(_usage())
        return 2

    con = _connect(_resolve_db())
    if con is None:
        err.write(f"no such lesson: {pos[0]} (no state DB at {_resolve_db()})\n")
        return 2
    try:
        if action == "forget":
            return _do_forget(con, pos[0], err)
        return _do_restore(con, pos[0], err)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
