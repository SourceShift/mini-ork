"""Operator-set preferences and constraints, plus drafted learned rules.

Backed by ``user_preference_memory`` (db/migrations/0009_memory_namespaces.sql:109-121;
CHECK extended by ``db/migrations/0065_preference_path_scope.sql``; provenance
columns added by ``db/migrations/0074_preference_origin.sql``);
scoped by ``scope ∈ {global, task_class, workflow, path}`` with ``scope_target``
carrying the class, workflow, or file glob (empty for global). Also surfaces
read-only entries from the legacy
``$MINI_ORK_HOME/config/{user_preferences,constraints}.json`` files via
``list_prefs`` / ``prefs_for`` so operators can see what the planner still ingests
through ``context_assembler.context_assemble``.

Every row also carries ``origin`` (``operator`` | ``learned``), ``source_ids``
(the finding keys a learned rule was derived from), and ``active`` (LF-R4). A
rule the "Make it a rule" button drafts is ``origin='learned', active=0``:
``list_prefs`` shows it — the Rules tab offers Approve on it — while ``prefs_for``
drops it, so a draft never reaches a prompt. ``approve_pref`` promotes it. Legacy
file-sourced entries are read-only operator rules: no origin/active fields, so
they behave as ``origin='operator', active=1``.

A ``path`` rule's ``scope_target`` is a *relative file glob* (``**`` = any depth,
``*`` = one segment, a trailing ``/`` = the directory and everything under it).
It is injected only into runs whose ``run_profile.json`` ``scope_allow`` touches
a matching path — see ``scope_paths``.

DB resolution mirrors ``mini_ork.memory.store._db_path()``: explicit ``db`` arg wins,
else ``$MINI_ORK_DB``, else ``$MINI_ORK_HOME/state.db``.

Public API (kickoff contract, ``kickoffs/auto/learn-prefs.md:34-56`` +
``kickoffs/auto/eng-path-rules.md`` + ``kickoffs/auto/lf-r4-rule-action.md``):

    set_pref(key, value, *, scope="global", target="", origin="operator",
             source_ids=None, active=True, db=None) -> dict
    remove_pref(key, *, scope="global", target="", db=None) -> bool
    approve_pref(key, *, scope, target, text=None, db=None) -> bool
    list_prefs(*, db=None) -> list[dict]
    prefs_for(task_class, workflow="", *, paths=None, db=None) -> list[dict]
    render_block(prefs) -> str
    scope_paths(run_dir) -> list[str]
    ensure_schema(db=None) -> None
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from ..context import context_env
from ..kickoff_scope import paths_in_line


_USER_ID = "default"
_VALID_SCOPES = ("global", "task_class", "workflow", "path")
_VALID_ORIGINS = ("operator", "learned")
_MAX_ENTRIES = 12
_MAX_VALUE_CHARS = 600

# The two prompt block headers. The operator heading is the pre-LF-R4 block,
# byte for byte — out-of-scope consumers (``mini_ork/cli/execute.py``) and their
# tests pin it, so it must not move. The learned heading and its close are new:
# a rule the review harvester proposed is advice from findings, not an operator
# order, and it is rendered as such (em dash U+2014, verbatim from the kickoff).
OPERATOR_HEADING = (
    "--- Operator preferences and constraints (set by the user; follow them) ---"
)
OPERATOR_CLOSE = "--- /operator preferences ---"
LEARNED_HEADING = (
    "--- Learned rules (from review findings — check they still apply "
    "before following) ---"
)
LEARNED_CLOSE = "--- /learned rules ---"


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
    """Enforce the table's CHECK + a stricter per-scope target rule.

    The DDL CHECK permits ``scope='global', scope_target='whatever'`` (no
    constraint), so the API layer enforces the cleaner rule that ``global``
    prefs carry an empty target. ``scope='role'`` is rejected here even
    though SQLite would accept it. A ``path`` rule's target is a relative file
    glob (non-empty, no leading ``/``, no ``..`` segment).
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"invalid scope {scope!r}; allowed: {', '.join(_VALID_SCOPES)}"
        )
    if scope == "global" and target:
        raise ValueError("scope=global requires target=''")
    if scope == "path":
        _validate_glob(target)


def _validate_glob(glob: str) -> None:
    """A path-scope target must be a non-empty, relative glob."""
    if not isinstance(glob, str) or not glob.strip():
        raise ValueError("scope=path requires a non-empty --target glob")
    if glob != glob.strip():
        raise ValueError(f"invalid path glob {glob!r}: leading/trailing whitespace")
    if glob.startswith("/") or os.path.isabs(glob):
        raise ValueError(f"invalid path glob {glob!r}: must be relative (no leading '/')")
    if ".." in glob.split("/"):
        raise ValueError(f"invalid path glob {glob!r}: must not contain '..'")


# ─── path globs ──────────────────────────────────────────────────────────


def _compile_glob(glob: str) -> re.Pattern:
    """Translate a relative path glob into an anchored regex.

    ``**`` → any depth (``.*``); a ``**`` directly followed by ``/`` becomes
    ``(?:.*/)?`` — zero or more *whole* directories — so ``a/**/b`` matches both
    ``a/b`` and ``a/x/b`` but never ``a/xb``. The ``/`` is kept *inside* the
    optional group: emitting ``.*`` and consuming the slash would let the match
    end mid-segment (``a/**/b.py`` → ``a/.*b\\.py``, which wrongly matches
    ``a/xb.py``).
    ``*`` → a single segment (``[^/]*``); ``?`` → one non-``/`` char. A trailing
    ``/`` means "the directory and everything under it" (an implied ``**``).
    Everything else is regex-escaped.

    Deliberately NOT ``fnmatch``: ``fnmatch``'s ``*`` also crosses ``/``, so
    ``tests/unit/test_*.py`` would wrongly match ``tests/unit/sub/test_x.py``.
    The repo documents this divergence (mini_ork/gates/scope_overlap.py:268).
    """
    g = glob + "**" if glob.endswith("/") else glob
    out: list[str] = []
    i, n = 0, len(g)
    while i < n:
        c = g[i]
        if c == "*":
            if i + 1 < n and g[i + 1] == "*":
                i += 2
                if i < n and g[i] == "/":
                    # `**/` = zero or more *whole* directories. The `/` stays
                    # inside the optional group so a match can never end
                    # mid-segment: `a/.*b` would wrongly match `a/xb`.
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")  # trailing/bare `**` = any depth
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    return re.compile("^(?:" + "".join(out) + ")$")


def glob_matches(glob: str, path: str) -> bool:
    """True when relative ``path`` matches ``glob`` (semantics: ``_compile_glob``)."""
    try:
        return bool(_compile_glob(glob).match(path))
    except re.error:
        return False


def _glob_matches_any(glob: str, paths: list[str]) -> bool:
    return any(glob_matches(glob, p) for p in paths if p)


# ─── file-scope extraction (kickoff → run_profile → path rules) ───────────


def paths_in_text(text: str) -> list[str]:
    """Path-like tokens in ``text``: normalized, order-preserving, de-duplicated.

    One line at a time through ``kickoff_scope.paths_in_line`` (LF-R8), so a
    BARE bullet head counts (``- mini_ork/foo.py``) as well as a backticked
    token, and the same rule answers for ``scope_paths`` (run profiles),
    ``context_v2.parse_contract`` and the ``prefs preview`` kickoff parser.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        for p in paths_in_line(line):
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def scope_paths(run_dir: str | None) -> list[str]:
    """The file paths a run declares in ``run_profile.json`` → ``scope_allow``.

    ``scope_allow`` is a list of raw kickoff bullet lines, so one entry may carry
    several paths (``"`a.py`, `b.py`"``). ``paths_in_text`` extracts and
    normalizes them (``kickoff_scope.paths_in_line``: backticked tokens AND bare
    bullet heads); the result is what ``prefs_for(paths=…)`` matches
    ``path``-scoped rules against.

    Two guards drop noise: scanning stops at the ``Do NOT …`` sentinel that
    terminates a real "Files in scope" list, and a token ending in a glob ``**``
    is skipped. Both are needed because the profiling heuristic that builds
    ``scope_allow`` substring-matches section titles, so before LF-R8 a heading
    like ``## Tests`` — whose title contains the word "scope" — could leak a
    whole prose block into the list; without the guards such prose would make a
    path rule fire for runs that merely *mention* the glob.

    Missing file → ``[]`` (never raises): callers sit on the prompt-injection
    path and must fail soft.
    """
    if not run_dir:
        return []
    profile = os.path.join(str(run_dir), "run_profile.json")
    try:
        with open(profile, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("scope_allow") or []
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        if entry.lstrip().startswith("Do NOT"):
            break
        # A profile entry is the bullet's TEXT — ``gen_profile`` stripped the
        # marker — and ``paths_in_line``'s bare-head rule applies to a LIST
        # ITEM's first token, so restore the item before parsing (LF-R8).
        item = entry if entry[:2] in ("- ", "* ", "+ ") else "- " + entry
        for p in paths_in_text(item):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


# ─── ensure_schema (idempotent DDL; mirrors migration 0065) ───────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_preference_memory (
  user_id             TEXT    NOT NULL,
  preference_key      TEXT    NOT NULL,
  preference_value    TEXT    NOT NULL DEFAULT '{}',
  scope               TEXT    NOT NULL DEFAULT 'global'
                      CHECK (scope IN ('global','task_class','workflow','path')),
  scope_target        TEXT    NOT NULL DEFAULT '',
  set_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  origin              TEXT    NOT NULL DEFAULT 'operator',
  source_ids          TEXT,
  active              INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, preference_key, scope, scope_target)
);
"""

_CREATE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_user_pref_user_id ON user_preference_memory(user_id);
CREATE INDEX IF NOT EXISTS idx_user_pref_key     ON user_preference_memory(preference_key);
CREATE INDEX IF NOT EXISTS idx_user_pref_scope   ON user_preference_memory(scope);
"""

# The CHECK cannot be widened in place, so the table is rebuilt: create-new →
# copy rows → drop-old → rename. Mirrors db/migrations/0065_preference_path_scope.sql;
# unlike the SQL migration this path introspects ``sqlite_master`` for dependent
# views/triggers (the migration drops/recreates the one known view by hand).
#
# Deliberately the PRE-0074 shape: this rebuild only fires for a table whose
# CHECK predates migration 0065, and the origin/source_ids/active columns are
# added by ``_add_origin_columns`` *after* it (see ``_ensure_schema_con``). Copying
# six columns into a table that may not have nine keeps the statement valid on
# every shape; a DB old enough to need the rebuild cannot have the columns yet
# (0074 sorts after 0065).
_REBUILD_TABLE_SQL = """
CREATE TABLE user_preference_memory_new (
  user_id             TEXT    NOT NULL,
  preference_key      TEXT    NOT NULL,
  preference_value    TEXT    NOT NULL DEFAULT '{}',
  scope               TEXT    NOT NULL DEFAULT 'global'
                      CHECK (scope IN ('global','task_class','workflow','path')),
  scope_target        TEXT    NOT NULL DEFAULT '',
  set_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (user_id, preference_key, scope, scope_target)
);
INSERT INTO user_preference_memory_new
  (user_id, preference_key, preference_value, scope, scope_target, set_at)
SELECT user_id, preference_key, preference_value, scope, scope_target, set_at
FROM user_preference_memory;
DROP TABLE user_preference_memory;
ALTER TABLE user_preference_memory_new RENAME TO user_preference_memory;
"""


def ensure_schema(db: str | None = None) -> None:
    """Idempotent DDL so an unmigrated DB still accepts ``path`` and learned rules.

    Worktree DBs are routinely unmigrated (see migrations
    ``0065_preference_path_scope.sql`` and ``0074_preference_origin.sql``, whose
    effects this mirrors). The CHECK widening and the origin/source_ids/active
    columns are two INDEPENDENT checks: a table already carrying ``path`` still
    needs the provenance columns, so the first cannot short-circuit the second.
    The CHECK is widened by rebuilding (create-new → copy rows → drop → rename),
    the three indexes the drop takes with it are recreated, and any view/trigger
    that references the table is dropped for the rebuild and recreated verbatim —
    otherwise the rename aborts on SQLite >= 3.45 (see
    ``_dependent_schema_objects``). The columns are then added in place with
    ``ALTER TABLE ADD COLUMN``. Cold-safe: a missing table is created with every
    column from the start.
    """
    db_path = _resolve_db(db)
    con = sqlite3.connect(db_path)
    try:
        _ensure_schema_con(con)
        con.commit()
    finally:
        con.close()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _dependent_schema_objects(
    con: sqlite3.Connection, table: str,
) -> list[tuple[str, str, str]]:
    """Every view/trigger whose SQL references ``table`` by name, views first.

    The rebuild drops ``table`` and later renames ``<table>_new`` onto its name.
    On SQLite >= 3.45 (``legacy_alter_table`` off — the runtime default under
    Python 3.13) that ``ALTER TABLE … RENAME`` re-parses every view and trigger
    in the schema, so a dependent left dangling across the drop aborts the
    rebuild with ``error in view <name>: no such table: main.<table>``. The
    caller drops these around the rebuild and recreates them from this SQL.
    """
    deps = [
        (typ, name, sql)
        for typ, name, sql in con.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('view','trigger') AND sql IS NOT NULL")
        if table in sql
    ]
    # Views before triggers: a trigger body may select from a view, not vice versa.
    deps.sort(key=lambda d: 0 if d[0] == "view" else 1)
    return deps


def _ensure_schema_con(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ("user_preference_memory",),
    ).fetchone()
    if row is None:
        con.executescript(_CREATE_TABLE_SQL + _CREATE_INDEXES_SQL)
        return
    ddl = row[0] or ""
    if "'path'" not in ddl and '"path"' not in ddl:
        _rebuild_for_path_scope(con)
    _add_origin_columns(con)


def _rebuild_for_path_scope(con: sqlite3.Connection) -> None:
    """Widen the CHECK by rebuilding the table (see ``_REBUILD_TABLE_SQL``)."""
    deps = _dependent_schema_objects(con, "user_preference_memory")
    stmts = [
        # Drop dependents first so the RENAME's schema re-parse stays clean.
        *[f"DROP {typ.upper()} IF EXISTS {_quote_ident(name)};"
          for typ, name, _ in deps],
        _REBUILD_TABLE_SQL,
        _CREATE_INDEXES_SQL,
        # Recreate verbatim; the renamed table now answers to the old name.
        *[sql.strip().rstrip(";") + ";" for _, _, sql in deps],
    ]
    try:
        con.executescript("BEGIN;\n" + "\n".join(stmts) + "\nCOMMIT;\n")
    except sqlite3.Error:
        # Never leave the DB without its views: unwind the whole transaction.
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


# (name, column definition) — the three columns migration 0074 declares. Kept
# as data so the ALTER and the comment above cannot drift from the migration.
_ORIGIN_COLUMNS = (
    ("origin", "TEXT NOT NULL DEFAULT 'operator'"),
    ("source_ids", "TEXT"),
    ("active", "INTEGER NOT NULL DEFAULT 1"),
)


def _add_origin_columns(con: sqlite3.Connection) -> None:
    """Add any missing origin/source_ids/active column. Mirrors migration 0074.

    This check must be INDEPENDENT of the CHECK-scope check above: the fast path
    used to ``return`` as soon as ``'path'`` was in the DDL, so a DB migrated to
    0065 but not 0074 (the common case — the table already accepts ``path``)
    skipped every later addition. A DB in the migration-ordered state has all
    three already, so this is a no-op there; ``PRAGMA table_info`` + ``ALTER
    TABLE ADD COLUMN`` also avoids the rebuild path entirely, which is what keeps
    it safe on a table that has a dependent view.
    """
    have = {r[1] for r in con.execute("PRAGMA table_info(user_preference_memory)")}
    for name, decl in _ORIGIN_COLUMNS:
        if name not in have:
            con.execute(
                f"ALTER TABLE user_preference_memory ADD COLUMN {name} {decl}")


# ─── set_pref / remove_pref ──────────────────────────────────────────────


def set_pref(key: str, value, *, scope: str = "global", target: str = "",
            origin: str = "operator", source_ids=None, active: bool = True,
            db: str | None = None) -> dict:
    """Upsert a preference. Returns the stored row as a dict with ``source``.

    Raises ``ValueError`` for an invalid ``scope``/``origin`` or a ``global`` pref
    carrying a non-empty ``target``. ``value`` is plain text stored as a JSON
    string so the same column holds strings, numbers, and small objects.

    ``origin='learned'`` with ``active=False`` is the LF-R4 draft: the row is
    stored (``list_prefs`` shows it, the Rules tab offers Approve) but
    ``prefs_for`` drops it, so it never reaches a prompt. ``source_ids`` is the
    list of finding keys the rule was derived from, stored as a JSON array.
    """
    _validate_scope(scope, target)
    if origin not in _VALID_ORIGINS:
        raise ValueError(
            f"invalid origin {origin!r}; allowed: {', '.join(_VALID_ORIGINS)}"
        )
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    if source_ids is not None and not isinstance(source_ids, str):
        source_ids = json.dumps(list(source_ids), ensure_ascii=False)
    db_path = _resolve_db(db)
    # An unmigrated DB (one that never ran migration 0065/0074) still rejects a
    # 'path' scope at the DDL CHECK and lacks the provenance columns; ensure the
    # table accepts both before writing.
    ensure_schema(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO user_preference_memory "
            "(user_id, preference_key, preference_value, scope, scope_target, set_at, "
            " origin, source_ids, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, preference_key, scope, scope_target) DO UPDATE SET "
            "  preference_value=excluded.preference_value, "
            "  set_at=excluded.set_at, "
            "  origin=excluded.origin, "
            "  source_ids=excluded.source_ids, "
            "  active=excluded.active",
            (_USER_ID, key, value, scope, target, _now_iso(),
             origin, source_ids, 1 if active else 0),
        )
        con.commit()
    finally:
        con.close()
    return {
        "key": key, "value": value, "scope": scope, "target": target,
        "set_at": _now_iso(), "source": "db", "origin": origin,
        "source_ids": source_ids, "active": 1 if active else 0,
    }


def approve_pref(key: str, *, scope: str, target: str, text: str | None = None,
                 db: str | None = None) -> bool:
    """Promote a drafted rule to ``active=1``. Returns True when a row changed.

    Approving does NOT re-brand the rule: a ``learned`` rule stays ``learned``
    and keeps its provenance, it merely starts reaching ``prefs_for`` (and so the
    "Learned rules" block). With ``text``, the approved value is replaced by it —
    the operator edits the generalised draft before it goes live.

    A row that does not exist reports failure rather than success, so the CLI and
    the Rules tab can tell "approved" from "there was nothing to approve".
    """
    _validate_scope(scope, target)
    db_path = _resolve_db(db)
    ensure_schema(db_path)
    con = sqlite3.connect(db_path)
    try:
        where = "user_id=? AND preference_key=? AND scope=? AND scope_target=?"
        if text is None:
            cur = con.execute(
                "UPDATE user_preference_memory SET active=1 WHERE " + where,
                (_USER_ID, key, scope, target))
        else:
            cur = con.execute(
                "UPDATE user_preference_memory SET active=1, preference_value=? "
                "WHERE " + where,
                (str(text), _USER_ID, key, scope, target))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


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


def _db_rows(con: sqlite3.Connection) -> list[dict]:
    """Every ``user_preference_memory`` row, oldest first, with LF-R4 provenance.

    The provenance columns are selected only when the table actually has them
    (``PRAGMA table_info``): a state.db that predates migration 0074 is read as
    all-operator, active rules rather than raising ``no such column`` on a page
    that must never blank.
    """
    cols = ["preference_key", "preference_value", "scope", "scope_target", "set_at"]
    have = {r[1] for r in con.execute("PRAGMA table_info(user_preference_memory)")}
    has_origin = {"origin", "source_ids", "active"} <= have
    if has_origin:
        cols += ["origin", "source_ids", "active"]
    out: list[dict] = []
    for row in con.execute(
            f"SELECT {', '.join(cols)} FROM user_preference_memory "
            "WHERE user_id=? ORDER BY set_at ASC", (_USER_ID,)):
        r = dict(zip(cols, row))
        out.append({
            "key": r["preference_key"], "value": r["preference_value"],
            "scope": r["scope"], "target": r["scope_target"],
            "set_at": r["set_at"], "source": "db",
            "origin": r["origin"] if has_origin else "operator",
            "source_ids": r["source_ids"] if has_origin else None,
            "active": r["active"] if has_origin else 1,
        })
    return out


def list_prefs(*, db: str | None = None) -> list[dict]:
    """DB rows plus read-only entries from legacy JSON files (when present).

    Every DB row carries ``origin``, ``source_ids`` and ``active`` (LF-R4): a
    draft is LISTED here — the Rules tab is where it gets approved — even though
    ``prefs_for`` drops it. Legacy file-sourced entries are operator rules with
    no provenance, so they read back ``origin='operator', source_ids=None,
    active=1``.

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
            out.extend(_db_rows(con))
        finally:
            con.close()

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
                        # A file-sourced entry is an operator rule by definition
                        # and cannot be a draft: it is read-only.
                        "origin": "operator",
                        "source_ids": None,
                        "active": 1,
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
                    "origin": "operator",
                    "source_ids": None,
                    "active": 1,
                })
        except (OSError, json.JSONDecodeError):
            pass
    return out


# ─── prefs_for (operator rules reach the prompt node) ──────


def prefs_for(task_class: str, workflow: str = "",
              *, paths: list[str] | None = None,
              db: str | None = None) -> list[dict]:
    """Return prefs relevant to a node dispatch: globals first, then scoped.

    Ordering is stable: globals (set_at ASC), task_class-scoped (set_at ASC),
    workflow-scoped (set_at ASC), then path-scoped (set_at ASC). A ``path`` rule
    is included only when ``paths`` is given and its glob matches at least one
    path; with ``paths=None`` no path rule is returned (a run that declared no
    file scope gets none). Caps: 12 entries total, 600 chars per value. Caps are
    applied per-row so the slice never truncates mid-row.

    Only ACTIVE rows are returned (LF-R4): an unapproved learned draft is listed
    by ``list_prefs`` but must never reach a prompt, so it is dropped here — the
    one place every injection path funnels through. Operator rules are always
    active; file-sourced entries default to active.
    """
    tc = task_class or ""
    rows = [r for r in list_prefs(db=db) if r.get("active", 1)]
    selected: list[dict] = []
    by_scope: dict[str, list[dict]] = {
        "global": [], "task_class": [], "workflow": [], "path": [],
    }
    for r in rows:
        s = r["scope"]
        if s == "global":
            by_scope["global"].append(r)
        elif s == "task_class" and r["target"] == tc:
            by_scope["task_class"].append(r)
        elif s == "workflow" and r["target"] == workflow:
            by_scope["workflow"].append(r)
        elif s == "path" and paths and _glob_matches_any(r["target"], paths):
            by_scope["path"].append(r)
    for scope_bucket in ("global", "task_class", "workflow", "path"):
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
    """Format prefs as fenced prompt block(s); empty string when no prefs.

    Two blocks (LF-R4), operator first:
        --- Operator preferences and constraints (set by the user; follow them) ---
        - <value>   [scope: task_class=<x>]
        --- /operator preferences ---
        --- Learned rules (from review findings — check they still apply before following) ---
        - <value>   [scope: path=<glob>]
        --- /learned rules ---

    A learned rule is advice harvested from review findings; it is NOT an
    operator order, so it must not appear under the "set by the user" heading
    (that heading is a claim about WHO set the rule). A prompt with only one kind
    shows only that block. The operator-only output is byte-identical to the
    pre-LF-R4 single block — ``mini_ork/cli/execute.py`` and its tests pin it.
    """
    if not prefs:
        return ""
    operator = [p for p in prefs if str(p.get("origin") or "operator") != "learned"]
    learned = [p for p in prefs if str(p.get("origin") or "operator") == "learned"]
    blocks = [
        b for b in (_render_lines(operator, OPERATOR_HEADING, OPERATOR_CLOSE),
                    _render_lines(learned, LEARNED_HEADING, LEARNED_CLOSE)) if b
    ]
    return "\n".join(blocks)


def _render_lines(prefs: list[dict], heading: str, close: str) -> str:
    """One fenced block, or ``""`` when every value is blank."""
    lines = [heading]
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
    lines.append(close)
    return "\n".join(lines) + "\n"


# ─── internal helpers ────────────────────────────────────────────────────


def _now_iso() -> str:
    """ISO-8601 UTC, millisecond precision (matches migration default)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"