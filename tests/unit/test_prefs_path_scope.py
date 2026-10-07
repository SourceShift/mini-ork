"""Contract tests for path-scoped preferences + ``mini-ork prefs preview``.

Covers the eng-path-rules kickoff surfaces:

- migration ``0065`` / ``preferences.ensure_schema``: an unmigrated DB (old
  CHECK) accepts ``scope='path'`` after the rebuild, and a pre-existing row
  survives; the three indexes the drop takes are recreated.
- the glob matcher (``**`` any depth, ``*`` one segment, trailing ``/``),
- ``scope_paths`` on the live ``scope_allow`` shapes,
- ``prefs_for(paths=…)`` include/exclude + ordering,
- ``_learned_block`` carrying a path rule only for a matching run, and
- ``prefs preview`` output shape + retrieval-ledger read-onlyness.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import context_assembler  # noqa: E402
from mini_ork.cli import execute, prefs_cmd  # noqa: E402
from mini_ork.memory import preferences  # noqa: E402


_OLD_CHECK_DDL = """
CREATE TABLE user_preference_memory (
  user_id             TEXT    NOT NULL,
  preference_key      TEXT    NOT NULL,
  preference_value    TEXT    NOT NULL DEFAULT '{}',
  scope               TEXT    NOT NULL DEFAULT 'global'
                      CHECK (scope IN ('global','task_class','workflow')),
  scope_target        TEXT    NOT NULL DEFAULT '',
  set_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (user_id, preference_key, scope, scope_target)
);
CREATE INDEX idx_user_pref_user_id ON user_preference_memory(user_id);
"""


# A DB whose pref table is referenced BY NAME by a view and a trigger — the
# shape the live state.db has (``v_memory_health``) and the fixture DBs did not.
# `ensure_schema`'s rebuild drops the table: on SQLite >= 3.45 the following
# ALTER TABLE RENAME re-parses the schema and aborts on the dangling view, and
# the drop takes the trigger with it. Both must be restored.
_PATH_VIEW_DDL = """
CREATE VIEW v_memory_health AS
SELECT
  'task_memory'            AS namespace,
  COUNT(*)                 AS row_count,
  MAX(created_at)          AS last_write
FROM task_memory

UNION ALL

SELECT
  'user_preference_memory',
  COUNT(*),
  MAX(set_at)
FROM user_preference_memory;
"""

_PATH_DEPENDENTS_DDL = (
    _OLD_CHECK_DDL
    + """
CREATE TABLE task_memory (row_id INTEGER PRIMARY KEY, created_at TEXT);
CREATE TABLE pref_audit (preference_key TEXT);
"""
    + _PATH_VIEW_DDL
    + """
CREATE TRIGGER trg_pref_audit AFTER INSERT ON user_preference_memory
BEGIN
  INSERT INTO pref_audit(preference_key) VALUES (NEW.preference_key);
END;
"""
)

# The remaining memory-namespace tables the migration's v_memory_health selects
# from (task_memory comes from _PATH_DEPENDENTS_DDL), so the recreated live view
# is queryable rather than merely creatable.
_NAMESPACE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS workflow_memory (row_id INTEGER PRIMARY KEY, created_at TEXT);
CREATE TABLE IF NOT EXISTS agent_performance_memory (row_id INTEGER PRIMARY KEY, last_updated TEXT);
CREATE TABLE IF NOT EXISTS failure_memory (row_id INTEGER PRIMARY KEY, occurred_at TEXT);
CREATE TABLE IF NOT EXISTS recovery_memory (row_id INTEGER PRIMARY KEY, recovered_at TEXT);
CREATE TABLE IF NOT EXISTS artifact_memory (row_id INTEGER PRIMARY KEY, produced_at TEXT);
CREATE TABLE IF NOT EXISTS benchmark_memory (row_id INTEGER PRIMARY KEY, ran_at TEXT);
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A temp DB built via ``db/init.sh`` (so migration 0065 applies), with the
    env wired so ``preferences`` / ``context_assembler`` resolve it."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    return dbp


def _seed_gradient(db: str, task_class: str = "framework_edit") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal, "
        "suggested_change, evidence, confidence, created_at, task_class) "
        "VALUES ('g1','auth.middleware','tests skipped silently','run pytest -x',"
        "'e',0.9,?,?)", (int(time.time()), task_class))
    con.commit()
    con.close()


def _seed_lessoned_pattern(db: str) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
        "member_item_ids_json, feature_set_json, strength_score, "
        "suggested_meta_adr, status, lesson_text, detected_at) "
        "VALUES ('emg-yes','taught lesson','[]',?,7.0,'meta','approved',?,?)",
        (json.dumps(["adr"]), "auth: always run the gate", int(time.time())))
    con.commit()
    con.close()


def _index_names(db: str) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_user_pref%'")}
    finally:
        con.close()


def _table_ddl(db: str) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='user_preference_memory'").fetchone()[0]
    finally:
        con.close()


def _schema_object_count(db: str, typ: str, name: str) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type=? AND name=?",
            (typ, name)).fetchone()[0]
    finally:
        con.close()


def _pref_row_count_via_view(db: str) -> int:
    """The user_preference_memory row_count reported by v_memory_health."""
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT row_count FROM v_memory_health "
            "WHERE namespace='user_preference_memory'").fetchone()[0]
    finally:
        con.close()


def _audit_count(db: str) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM pref_audit").fetchone()[0]
    finally:
        con.close()


# ─── migration 0065 / ensure_schema ────────────────────────────────────────


def test_migration_keeps_view_indexes_and_accepts_path(db):
    """0065 rebuilds the table without losing the view or the indexes."""
    con = sqlite3.connect(db)
    views = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='view' "
        "AND name='v_memory_health'").fetchone()[0]
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='user_preference_memory'").fetchone()[0]
    con.close()
    assert views == 1, "v_memory_health must survive the rebuild"
    assert _index_names(db) == {
        "idx_user_pref_user_id", "idx_user_pref_key", "idx_user_pref_scope"}
    assert "'path'" in ddl
    # The migrated DDL accepts a path rule.
    preferences.set_pref("p", "x", scope="path", target="a/**")
    assert any(r["scope"] == "path" for r in preferences.list_prefs())


def test_ensure_schema_rebuilds_old_check(tmp_path, monkeypatch):
    """An unmigrated DB (old CHECK) survives the rebuild and gains path scope."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    con = sqlite3.connect(dbp)
    con.executescript(_OLD_CHECK_DDL)
    con.execute(
        "INSERT INTO user_preference_memory "
        "(user_id, preference_key, preference_value, scope, scope_target, set_at) "
        "VALUES ('default','tone','be terse','global','','2026-01-01T00:00:00.000Z')")
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    preferences.ensure_schema()

    # The pre-existing global row survived the copy.
    rows = preferences.list_prefs()
    assert any(r["key"] == "tone" and r["value"] == "be terse" for r in rows)
    # A path rule can now be inserted.
    preferences.set_pref("ide-tests", "run tests", scope="path",
                         target="mini_ork/ide_pages/**")
    assert any(r["scope"] == "path" for r in preferences.list_prefs())
    # All three indexes were recreated by the rebuild.
    assert _index_names(dbp) == {
        "idx_user_pref_user_id", "idx_user_pref_key", "idx_user_pref_scope"}
    # Idempotent: a second call is a no-op.
    preferences.ensure_schema()


def test_ensure_schema_recreates_dependent_view_and_trigger(tmp_path, monkeypatch):
    """The rebuild must restore a view AND a trigger that name the table.

    On SQLite >= 3.45 (the Python 3.13 runtime) ALTER TABLE RENAME re-parses the
    schema, so a view left dangling across the drop aborts the rebuild with
    ``error in view v_memory_health: no such table: main.user_preference_memory``;
    and DROP TABLE takes any trigger on the table with it. Both must survive.
    """
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    con = sqlite3.connect(dbp)
    con.executescript(_PATH_DEPENDENTS_DDL)
    con.execute(
        "INSERT INTO user_preference_memory "
        "(user_id, preference_key, preference_value, scope, scope_target, set_at) "
        "VALUES ('default','tone','be terse','global','','2026-01-01T00:00:00.000Z')")
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    preferences.ensure_schema()

    assert "'path'" in _table_ddl(dbp)
    # The view survived and still counts the pre-existing row.
    assert _schema_object_count(dbp, "view", "v_memory_health") == 1
    assert _pref_row_count_via_view(dbp) == 1
    # A path rule inserts into the rebuilt table and the view sees it.
    preferences.set_pref("ide-tests", "run tests", scope="path",
                         target="mini_ork/ide_pages/**")
    assert _pref_row_count_via_view(dbp) == 2
    # The trigger survived and still fires.
    assert _schema_object_count(dbp, "trigger", "trg_pref_audit") == 1
    before = _audit_count(dbp)
    con = sqlite3.connect(dbp)
    con.execute(
        "INSERT INTO user_preference_memory "
        "(user_id, preference_key, preference_value, scope, scope_target, set_at) "
        "VALUES ('default','k','v','global','','2026-01-02T00:00:00.000Z')")
    con.commit()
    con.close()
    assert _audit_count(dbp) == before + 1, "the recreated trigger must still fire"


def test_migration_0065_recreates_dependent_view(tmp_path, monkeypatch):
    """0065 must drop and recreate v_memory_health around its table rebuild.

    SQL cannot introspect, so the migration names the one live dependent view
    explicitly; without that drop the RENAME re-parse fails on SQLite >= 3.45.
    """
    dbp = str(tmp_path / "state.db")
    con = sqlite3.connect(dbp)
    con.executescript(_PATH_DEPENDENTS_DDL + _NAMESPACE_TABLES_DDL)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);")
    con.execute(
        "INSERT INTO user_preference_memory "
        "(user_id, preference_key, preference_value, scope, scope_target, set_at) "
        "VALUES ('default','tone','be terse','global','','2026-01-01T00:00:00.000Z')")
    con.commit()
    con.close()

    from mini_ork.stores import migrate  # the real runner's statement splitter

    sql = (REPO / "db" / "migrations" / "0065_preference_path_scope.sql").read_text()
    con = sqlite3.connect(dbp)
    con.isolation_level = None  # manual transaction control, as _apply_one uses
    migrate._exec_statements(con, sql, env={})
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum) "
        "VALUES ('0065_preference_path_scope.sql', "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'preference-path-scope-v1')")
    con.close()

    assert "'path'" in _table_ddl(dbp)
    # 0065 recreated the view from the live definition; it is queryable and
    # counts the surviving row.
    assert _schema_object_count(dbp, "view", "v_memory_health") == 1
    assert _pref_row_count_via_view(dbp) == 1
    # The widened CHECK accepts a path rule, and the view sees it.
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    preferences.set_pref("ide-tests", "run tests", scope="path",
                         target="mini_ork/ide_pages/**")
    assert _pref_row_count_via_view(dbp) == 2


# ─── glob matching ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("glob,path,expected", [
    ("mini_ork/ide_pages/**", "mini_ork/ide_pages/learn/memory.py", True),
    ("mini_ork/ide_pages/**", "mini_ork/ide_pages/node.py", True),
    ("mini_ork/ide_pages/**", "mini_ork/cli/x.py", False),
    ("tests/unit/test_*.py", "tests/unit/test_a.py", True),
    ("tests/unit/test_*.py", "tests/unit/sub/test_a.py", False),
    ("tests/unit/test_*.py", "tests/unit/test_a/b.py", False),
    ("mini_ork/ide_pages/", "mini_ork/ide_pages/learn/memory.py", True),
    ("mini_ork/ide_pages/", "mini_ork/ide_pages/node.py", True),
    ("mini_ork/ide_pages/", "mini_ork/cli/x.py", False),
    ("a/**/b.py", "a/b.py", True),
    ("a/**/b.py", "a/x/y/b.py", True),
    # `**/` spans whole directories only: it must not end mid-segment.
    ("a/**/b.py", "a/xb.py", False),
    ("**/test_x.py", "mytest_x.py", False),
    ("tests/**/test_*.py", "tests/unit/notest_a.py", False),
    # …and the positive side of the same rule.
    ("**/test_x.py", "test_x.py", True),
    ("**/test_x.py", "pkg/deep/test_x.py", True),
    ("tests/**/test_*.py", "tests/unit/test_a.py", True),
])
def test_glob_matching(glob, path, expected):
    assert preferences.glob_matches(glob, path) is expected


# ─── set/validate ───────────────────────────────────────────────────────────


def test_set_path_rejects_absolute_target(db, capsys):
    rc = prefs_cmd.main(["set", "x", "v", "--scope", "path", "--target", "/abs/x"]
                        )
    assert rc == 2
    assert "relative" in capsys.readouterr().err


def test_set_path_rejects_empty_target(db, capsys):
    rc = prefs_cmd.main(["set", "x", "v", "--scope", "path"])
    assert rc == 2
    assert "non-empty" in capsys.readouterr().err


def test_set_path_rejects_dotdot_target(db):
    with pytest.raises(ValueError):
        preferences.set_pref("x", "v", scope="path", target="../x/**")


def test_list_shows_path_rule_with_glob(db):
    preferences.set_pref("ide-tests", "run tests", scope="path",
                         target="mini_ork/ide_pages/**")
    out = io.StringIO()
    assert prefs_cmd.main(["list"], stdout=out, stderr=io.StringIO()) == 0
    text = out.getvalue()
    assert "path" in text and "mini_ork/ide_pages/**" in text


# ─── scope_paths ────────────────────────────────────────────────────────────


def test_scope_paths_live_shapes(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run_profile.json").write_text(json.dumps({"scope_allow": [
        "`mini_ork/learning/themes.py`, `tests/unit/test_themes.py`",
        "`mini_ork/cli/reflect.py` (only the themes block, if a signature changes)",
        "`./mini_ork/cli/other.py`",
        "Do NOT modify any other file.",
        "prose that mentions `mini_ork/ide_pages/**` and `mini_ork/cli/x.py`",
    ]}))
    assert preferences.scope_paths(str(run)) == [
        "mini_ork/learning/themes.py",
        "tests/unit/test_themes.py",
        "mini_ork/cli/reflect.py",
        "mini_ork/cli/other.py",
    ]


def test_scope_paths_missing_file_is_empty(tmp_path):
    assert preferences.scope_paths(str(tmp_path / "nope")) == []
    assert preferences.scope_paths(None) == []


# ─── prefs_for(paths=…) ─────────────────────────────────────────────────────


def test_prefs_for_path_scope_filters_and_orders(db):
    preferences.set_pref("g", "global-rule")
    preferences.set_pref("wf", "workflow-rule", scope="workflow",
                         target="framework-edit")
    preferences.set_pref("p1", "ide-rule", scope="path",
                         target="mini_ork/ide_pages/**")
    preferences.set_pref("p2", "cli-rule", scope="path", target="mini_ork/cli/**")

    rows = preferences.prefs_for(
        "framework_edit", "framework-edit", paths=["mini_ork/ide_pages/node.py"])
    keys = [r["key"] for r in rows]
    assert "g" in keys and "wf" in keys and "p1" in keys
    assert "p2" not in keys
    assert keys[-1] == "p1", "path bucket sorts last"

    # render_block tags the path scope with its glob.
    block = preferences.render_block(rows)
    assert "[scope: path=mini_ork/ide_pages/**]" in block

    # No paths → no path rules.
    rows2 = preferences.prefs_for("framework_edit", "framework-edit")
    assert all(r["scope"] != "path" for r in rows2)


# ─── _learned_block ─────────────────────────────────────────────────────────


def test_learned_block_injects_path_rule_for_matching_run(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    preferences.set_pref("ide-tests", "run the ide page tests", scope="path",
                         target="mini_ork/ide_pages/**")
    run = tmp_path / "run"
    run.mkdir()
    (run / "run_profile.json").write_text(
        json.dumps({"scope_allow": ["`mini_ork/ide_pages/node.py`"]}))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run))

    sources: list[dict] = []
    block = execute._learned_block(None, "framework_edit", "implementer",
                                   lane="", node_id="implementer", sources=sources)
    assert "run the ide page tests" in block
    assert any(
        s["kind"] == "preference"
        and s["id"] == "pref:path:mini_ork/ide_pages/**:ide-tests"
        for s in sources)


def test_learned_block_skips_path_rule_for_other_run(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    preferences.set_pref("ide-tests", "run the ide page tests", scope="path",
                         target="mini_ork/ide_pages/**")
    run = tmp_path / "run"
    run.mkdir()
    (run / "run_profile.json").write_text(
        json.dumps({"scope_allow": ["`mini_ork/cli/x.py`"]}))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run))

    block = execute._learned_block(None, "framework_edit", "implementer",
                                   lane="", node_id="implementer", sources=[])
    assert "run the ide page tests" not in block


# ─── prefs preview ──────────────────────────────────────────────────────────


def _write_kickoff(path: Path, scope_line: str) -> Path:
    path.write_text(
        "# Demo kickoff\n\n"
        "task_class: framework_edit\n\n"
        "## Files in scope (touch ONLY these)\n\n"
        f"- {scope_line}\n"
    )
    return path


def test_prefs_preview_prints_path_rule_and_failure_header(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")
    # Raw gradients reach the block only under the operator opt-in (only
    # verified learnings reach prompts by default, commit b13a285d). This test
    # asserts the unverified-gradient path, so it must opt in explicitly.
    monkeypatch.setenv("MO_INJECT_UNVERIFIED", "1")
    _seed_gradient(db)
    preferences.set_pref("ide-tests", "run the ide page tests", scope="path",
                         target="mini_ork/ide_pages/**")
    kick = _write_kickoff(tmp_path / "kickoff.md", "`mini_ork/ide_pages/node.py`")

    out, err = io.StringIO(), io.StringIO()
    rc = prefs_cmd.main(["preview", str(kick), "--node", "implementer"],
                        stdout=out, stderr=err)
    text = out.getvalue()
    assert rc == 0, err.getvalue()
    assert "run the ide page tests" in text
    assert "[scope: path=mini_ork/ide_pages/**]" in text
    assert "--- Learned failure modes" in text
    # Read-only: no retrieval-ledger rows.
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM semantic_memory_uses").fetchone()[0]
    con.close()
    assert n == 0


def test_prefs_preview_omits_non_matching_path_rule(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")
    preferences.set_pref("ide-tests", "run the ide page tests", scope="path",
                         target="mini_ork/ide_pages/**")
    kick = _write_kickoff(tmp_path / "kickoff.md", "`mini_ork/cli/x.py`")
    out = io.StringIO()
    assert prefs_cmd.main(["preview", str(kick)], stdout=out,
                          stderr=io.StringIO()) == 0
    assert "run the ide page tests" not in out.getvalue()


def test_prefs_preview_json_shape(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")
    preferences.set_pref("ide-tests", "run the ide page tests", scope="path",
                         target="mini_ork/ide_pages/**")
    kick = _write_kickoff(tmp_path / "kickoff.md", "`mini_ork/ide_pages/node.py`")
    out = io.StringIO()
    assert prefs_cmd.main(["preview", str(kick), "--node", "researcher", "--json"],
                          stdout=out, stderr=io.StringIO()) == 0
    payload = json.loads(out.getvalue().strip())
    assert payload["task_class"] == "framework_edit"
    assert payload["node"] == "researcher"
    assert "mini_ork/ide_pages/node.py" in payload["paths"]
    assert "run the ide page tests" in payload["block"]


def test_prefs_preview_does_not_write_ledger_even_with_run_id(db, monkeypatch, tmp_path):
    """The MINI_ORK_RUN_ID mask is what keeps preview clean: with a run id set,
    the same assembly DOES log a retrieval."""
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "1")
    _seed_lessoned_pattern(db)

    # With a run id: a ledger row is written.
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-x")
    context_assembler.failure_modes_md("framework_edit", 5, db=db,
                                       node_type="implementer", sources=[])
    con = sqlite3.connect(db)
    with_run = con.execute(
        "SELECT COUNT(*) FROM semantic_memory_uses").fetchone()[0]
    con.close()
    assert with_run >= 1, "the semantic path should log a retrieval under a run"

    # Preview masks the id → still no new rows.
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    preferences.set_pref("ide-tests", "run the ide page tests", scope="path",
                         target="mini_ork/ide_pages/**")
    kick = _write_kickoff(tmp_path / "kickoff.md", "`mini_ork/ide_pages/node.py`")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-y")  # a stale/leaked id
    assert prefs_cmd.main(["preview", str(kick)], stdout=io.StringIO(),
                          stderr=io.StringIO()) == 0
    con = sqlite3.connect(db)
    after = con.execute(
        "SELECT COUNT(*) FROM semantic_memory_uses").fetchone()[0]
    con.close()
    assert after == with_run, "preview must not add retrieval rows"


def test_prefs_preview_bad_node_exits_2(db, tmp_path):
    kick = _write_kickoff(tmp_path / "kickoff.md", "`mini_ork/ide_pages/node.py`")
    assert prefs_cmd.main(["preview", str(kick), "--node", "planner"],
                          stdout=io.StringIO(), stderr=io.StringIO()) == 2
