"""LF-R2 — emergent patterns are scoped to the run's task class.

`_approved_emergent_rows` used to hand every approved pattern to every run, so
72% of injections were cross-class: the code_fix reviewer of a live run read
patterns about `__classify__`, `__reflect__`, goal_loop and self_migrate. The
class was already in every cluster label ("cluster: task_class=code_fix
status=failure (freq=11 in window)") but only as prose.

These tests are hermetic: every one builds its own sqlite DB under pytest's
`tmp_path` (and the migration is applied to such a DB through the real
`mini_ork.stores.migrate` runner, the way db/init.sh does). No network, no
credentials, no run-local state, and never the live DB.
"""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from mini_ork import context_assembler
from mini_ork.learning.reflection_pipeline import _persist_suggestions_upsert
from mini_ork.stores.migrate import migrate_apply

REPO_ROOT = Path(context_assembler.__file__).resolve().parents[1]

# The emergent_patterns shape as it shipped through 0008 + 0056: ten columns
# and no task_class. Selection has to work against this shape (parsing the
# label) and against the 0077 shape (reading the column).
EMERGENT_SQL = """
CREATE TABLE emergent_patterns (
  pattern_id           TEXT PRIMARY KEY,
  cluster_label        TEXT NOT NULL,
  member_item_ids_json TEXT NOT NULL,
  feature_set_json     TEXT NOT NULL,
  strength_score       REAL NOT NULL,
  suggested_meta_adr   TEXT,
  status               TEXT NOT NULL DEFAULT 'proposed',
  detected_at          INTEGER NOT NULL,
  resolved_at          INTEGER,
  lesson_text          TEXT
)
"""

# Fixture shaped like the live labels: two classes with authored lessons, the
# two internal classes, another run's class, and a label carrying no
# `task_class=` token at all. Strengths are distinct so strength order is
# assertable and the internal rows are the STRONGEST ones — the bug was that
# they won the ordering they should never have entered.
# (pattern_id, cluster_label, strength, lesson_text, class)
FIXTURE_ROWS = [
    ("cf-b", "cluster: task_class=code_fix status=success (freq=4 in window)",
     6.0, "code_fix lesson b", "code_fix"),
    ("cf-a", "cluster: task_class=code_fix status=failure (freq=11 in window)",
     9.0, "code_fix lesson a", "code_fix"),
    ("fe-a", "cluster: task_class=framework_edit status=failure (freq=9 in window)",
     8.0, "framework_edit lesson", "framework_edit"),
    ("gl-a", "cluster: task_class=goal_loop status=failure (freq=6 in window)",
     7.0, "goal_loop lesson", "goal_loop"),
    ("ic-a", "cluster: task_class=__classify__ status=success (freq=9 in window)",
     12.0, "internal classify lesson", "__classify__"),
    ("ir-a", "cluster: task_class=__reflect__ status=failure (freq=8 in window)",
     11.0, "internal reflect lesson", "__reflect__"),
    ("nn-a", "cluster: status=mixed (freq=3 in window)",
     10.0, "classless lesson", None),
]

CF_PIDS = ["cf-a", "cf-b"]
CF_LESSONS = ["code_fix lesson a", "code_fix lesson b"]
CROSS_PIDS = {"fe-a", "gl-a"}
INTERNAL_OR_CLASSLESS_PIDS = {"ic-a", "ir-a", "nn-a"}


def make_db(path, rows=FIXTURE_ROWS, *, with_class_column):
    """A temp emergent_patterns DB of `rows`, all approved with a lesson.

    `with_class_column` adds `task_class` and fills it the way the migration
    backfill does; without it the table is the pre-0077 shape and the class is
    only in the label.
    """
    con = sqlite3.connect(str(path))
    con.executescript(EMERGENT_SQL)
    if with_class_column:
        con.execute("ALTER TABLE emergent_patterns ADD COLUMN task_class TEXT")
    for pid, label, strength, lesson, cls in rows:
        cols = ["pattern_id", "cluster_label", "member_item_ids_json",
                "feature_set_json", "strength_score", "status", "detected_at",
                "lesson_text"]
        vals = [pid, label, "[]", "[]", strength, "approved", 1000, lesson]
        if with_class_column:
            cols.append("task_class")
            vals.append(cls)
        con.execute(
            f"INSERT INTO emergent_patterns ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(vals))})", vals)
    con.commit()
    con.close()


def make_lessonless_db(path, label, *, with_class_column):
    """A temp DB with ONE approved pattern carrying no lesson_text.

    The lesson-less row is what the injection paths skip by default; it exists
    here to prove that skipping is the same decision before and after scoping.
    """
    con = sqlite3.connect(str(path))
    con.executescript(EMERGENT_SQL)
    if with_class_column:
        con.execute("ALTER TABLE emergent_patterns ADD COLUMN task_class TEXT")
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
        "member_item_ids_json, feature_set_json, strength_score, status, "
        "detected_at, lesson_text) VALUES (?,?,?,?,?,'approved',1000,NULL)",
        ("nless", label, "[]", "[]", 5.0))
    con.commit()
    con.close()


def pin_env(monkeypatch, *, semantic: str):
    """Pin every knob the injection paths read, so nothing ambient leaks in."""
    for key in ("MO_EMBED_PROVIDER", "MINI_ORK_RUN_ID", "MO_INJECT_UNVERIFIED",
                "MO_EMERGENT_INJECT_UNLESSONED", "MO_PATTERN_CROSS_CLASS_MAX",
                "MO_TARGET_CWD", "MINI_ORK_DB", "MO_SEMANTIC_INJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MO_LIMBO_BUDGET", "0")
    monkeypatch.setenv("MO_EMERGENT_INJECT", "1")
    monkeypatch.setenv("MO_EMERGENT_INJECT_LIMIT", "3")
    monkeypatch.setenv("MO_SEMANTIC_INJECT", semantic)


def injected_lessons(block):
    """The lesson texts of a block's '- ' item lines, sans credit suffix."""
    out = []
    for line in block.splitlines():
        if line.startswith("- "):
            text = line[2:]
            if "  (helped " in text:
                text = text.split("  (helped ", 1)[0]
            out.append(text)
    return out


def pids(rows):
    return [r[0] for r in rows]


def scopes_in(db):
    con = sqlite3.connect(str(db))
    try:
        return {r[0] for r in con.execute(
            "SELECT DISTINCT scope FROM semantic_memory")}
    finally:
        con.close()


# ── the parse ───────────────────────────────────────────────────────────────


def test_pattern_task_class_parses_the_live_label_forms():
    """The class is the token after `task_class=` up to the next space."""
    parse = context_assembler.pattern_task_class
    assert parse("cluster: task_class=code_fix status=failure (freq=11 in window)") == "code_fix"
    assert parse("cluster: task_class=recipe_authoring status=success (freq=46 in window)") == "recipe_authoring"
    assert parse("cluster: task_class=framework_edit status=failure (freq=9 in window)") == "framework_edit"
    # Internal classes parse like any other; selection is what excludes them.
    assert parse("cluster: task_class=__classify__ status=success (freq=2 in window)") == "__classify__"
    # A token at the end of the label is delimited by the end of the string.
    assert parse("cluster: task_class=goal_loop") == "goal_loop"
    # No token -> None, never ''.
    assert parse("cluster: status=mixed (freq=3 in window)") is None
    assert parse("") is None
    assert parse(None) is None


# ── selection ───────────────────────────────────────────────────────────────


def test_no_class_returns_every_approved_row_unchanged(tmp_path):
    """`task_class=None` keeps the pre-scoping contract other readers rely on."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    rows = context_assembler._approved_emergent_rows(str(db))
    assert pids(rows) == ["ic-a", "ir-a", "nn-a", "cf-a", "fe-a", "gl-a", "cf-b"]
    # The explicit kwarg means what omitting it means.
    assert context_assembler._approved_emergent_rows(
        str(db), task_class=None) == rows
    # Rows keep the 5-tuple shape every reader unpacks.
    assert all(len(r) == 5 for r in rows)


def test_scoped_selection_is_own_class_then_a_capped_non_internal_topup(tmp_path):
    """Own class first, in strength order, then at most one other class —
    never an internal `__`-class and never a classless row, even though those
    are the strongest rows in the fixture."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    got = pids(context_assembler._approved_emergent_rows(str(db),
                                                         task_class="code_fix"))
    assert got[:2] == CF_PIDS                       # strength order: cf-a, cf-b
    assert not (set(got) & INTERNAL_OR_CLASSLESS_PIDS)
    assert len(got[2:]) <= 1
    assert set(got[2:]) <= CROSS_PIDS


def test_scoped_selection_parses_the_label_without_the_column(tmp_path):
    """On a DB predating 0077 the same scoping holds via the label parse —
    cold-safe, not a special case that returns everything."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=False)
    got = pids(context_assembler._approved_emergent_rows(str(db),
                                                         task_class="code_fix"))
    assert got[:2] == CF_PIDS
    assert not (set(got) & INTERNAL_OR_CLASSLESS_PIDS)
    assert len(got[2:]) <= 1
    assert set(got[2:]) <= CROSS_PIDS


def test_cross_class_knob_is_a_fail_closed_cap(tmp_path, monkeypatch):
    """MO_PATTERN_CROSS_CLASS_MAX bounds the other-class top-up; 0 is
    own-class only, and an unparseable value falls back to the default 1."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)

    monkeypatch.setenv("MO_PATTERN_CROSS_CLASS_MAX", "0")
    assert pids(context_assembler._approved_emergent_rows(
        str(db), task_class="code_fix")) == CF_PIDS

    monkeypatch.setenv("MO_PATTERN_CROSS_CLASS_MAX", "2")
    got = pids(context_assembler._approved_emergent_rows(str(db),
                                                         task_class="code_fix"))
    assert got[:2] == CF_PIDS
    assert len(got) == 4                 # both non-internal other-class rows
    assert set(got[2:]) <= CROSS_PIDS

    monkeypatch.setenv("MO_PATTERN_CROSS_CLASS_MAX", "not-a-number")
    got = pids(context_assembler._approved_emergent_rows(str(db),
                                                         task_class="code_fix"))
    assert len(got) == 3                 # the documented default of 1


def test_cross_class_topup_needs_room_under_the_callers_limit(tmp_path, monkeypatch):
    """The top-up fills a thin block; it does not overrun the caller's budget.
    With the own-class rows already filling the limit there is no room."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    # Two own-class rows against a limit of 2: nothing borrowed.
    assert pids(context_assembler._approved_emergent_rows(
        str(db), task_class="code_fix", limit=2)) == CF_PIDS
    # Against a limit of 3 there is exactly one slot, and it is filled.
    got = pids(context_assembler._approved_emergent_rows(
        str(db), task_class="code_fix", limit=3))
    assert got[:2] == CF_PIDS and len(got) == 3 and set(got[2:]) <= CROSS_PIDS


def test_unknown_class_gets_only_the_capped_other_class_topup(tmp_path):
    """A class the store knows nothing about gets the capped other-class
    top-up — its own rows do not exist, and internal/classless ones are still
    not guidance about this run's work."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    got = pids(context_assembler._approved_emergent_rows(
        str(db), task_class="no_such_class"))
    assert got == ["cf-a"]                 # strongest non-internal other class
    assert not (set(got) & INTERNAL_OR_CLASSLESS_PIDS)


def test_store_with_no_classes_at_all_reads_unscoped(tmp_path):
    """A database whose every pattern predates classification has no scoped
    pool to offer; the read degrades to the unscoped one rather than to
    nothing, because those lessons are still evidence."""
    db = tmp_path / "state.db"
    make_db(db, [("p1", "cluster: status=mixed (freq=3 in window)", 9.0,
                  "classless lesson one", None),
                 ("p2", "cluster: status=failure (freq=2 in window)", 5.0,
                  "classless lesson two", None)],
            with_class_column=True)
    got = pids(context_assembler._approved_emergent_rows(
        str(db), task_class="code_fix"))
    assert got == ["p1", "p2"]


# ── the two injection paths ─────────────────────────────────────────────────


def test_static_block_scopes_to_the_run_class(tmp_path, monkeypatch):
    """The static pattern block selects from the scoped pool: own class only
    when it fills the limit, and the internal rows (the strongest of all)
    never appear."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    pin_env(monkeypatch, semantic="0")
    monkeypatch.setenv("MO_EMERGENT_INJECT_LIMIT", "2")   # the two own rows fill it
    sources = []
    block = context_assembler.failure_modes_md("code_fix", db=str(db),
                                               sources=sources)
    assert injected_lessons(block) == CF_LESSONS
    assert [s["id"] for s in sources] == CF_PIDS


def test_static_block_borrows_one_pattern_when_its_own_class_is_thin(tmp_path,
                                                                    monkeypatch):
    """One own-class row against a limit of 3 gets that row, then exactly one
    non-internal other-class row — never an internal or classless one."""
    db = tmp_path / "state.db"
    make_db(db, [FIXTURE_ROWS[1], FIXTURE_ROWS[2], FIXTURE_ROWS[3],
                 FIXTURE_ROWS[4], FIXTURE_ROWS[5], FIXTURE_ROWS[6]],
            with_class_column=True)
    pin_env(monkeypatch, semantic="0")
    lessons = injected_lessons(context_assembler.failure_modes_md(
        "code_fix", db=str(db)))
    assert lessons[0] == "code_fix lesson a"
    assert len(lessons) == 2
    assert lessons[1] in {"framework_edit lesson", "goal_loop lesson"}

    monkeypatch.setenv("MO_PATTERN_CROSS_CLASS_MAX", "0")
    assert injected_lessons(context_assembler.failure_modes_md(
        "code_fix", db=str(db))) == ["code_fix lesson a"]


def test_semantic_block_scopes_to_the_run_class(tmp_path, monkeypatch):
    """The UCB path reads the same scoped pool, so both paths agree on which
    patterns a run may see."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    pin_env(monkeypatch, semantic="1")
    sources = []
    block = context_assembler.semantic_lessons_md("code_fix", 2, db=str(db),
                                                  sources=sources)
    assert sorted(injected_lessons(block)) == sorted(CF_LESSONS)
    assert not ({s["id"] for s in sources} & INTERNAL_OR_CLASSLESS_PIDS)


def test_semantic_memory_scope_key_marks_the_scoped_era(tmp_path, monkeypatch):
    """The win/loss bookkeeping moves to `<class>#tc1`, so the scoped era's
    pool is never averaged with the unscoped era's history."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    pin_env(monkeypatch, semantic="1")
    context_assembler.semantic_lessons_md("code_fix", 2, db=str(db))
    assert scopes_in(db) == {"code_fix#tc1"}


def test_semantic_memory_scope_key_is_generic_for_an_empty_class(tmp_path,
                                                               monkeypatch):
    """A falsy class keys under `generic#tc1` — never the bare legacy key."""
    db = tmp_path / "state.db"
    make_db(db, with_class_column=True)
    pin_env(monkeypatch, semantic="1")
    context_assembler.semantic_lessons_md("", 3, db=str(db))
    assert scopes_in(db) <= {"generic#tc1"}
    assert "generic" not in scopes_in(db)


def test_unscoped_store_keeps_the_legacy_memory_key(tmp_path, monkeypatch):
    """A store whose patterns carry no class has no scoped era to isolate: its
    selection degrades to the unscoped pool, so its track record must not be
    silently orphaned under a new key."""
    db = tmp_path / "state.db"
    make_db(db, [("p1", "cluster: status=mixed (freq=3 in window)", 9.0,
                  "classless lesson one", None),
                 ("p2", "cluster: status=failure (freq=2 in window)", 5.0,
                  "classless lesson two", None)],
            with_class_column=True)
    pin_env(monkeypatch, semantic="1")
    block = context_assembler.semantic_lessons_md("code_fix", 3, db=str(db))
    assert injected_lessons(block) == ["classless lesson one",
                                       "classless lesson two"]
    assert scopes_in(db) == {"code_fix"}


def test_lessonless_patterns_are_skipped_the_same_way_after_scoping(tmp_path,
                                                                    monkeypatch):
    """The authored-lesson gate is orthogonal to scoping: a classless,
    lesson-less row is skipped either way."""
    db = tmp_path / "state.db"
    make_lessonless_db(db, "cluster: status=mixed (freq=3 in window)",
                       with_class_column=True)
    pin_env(monkeypatch, semantic="1")
    assert context_assembler.semantic_lessons_md("code_fix", 3, db=str(db)) == ""
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")
    assert context_assembler.failure_modes_md("code_fix", db=str(db)) == ""


# ── the migration ───────────────────────────────────────────────────────────


def find_migration():
    """The new migration, matched on its stem so a renumber on origin/main
    (0077 taken by a concurrent merge) does not fail these tests."""
    hits = sorted((REPO_ROOT / "db" / "migrations").glob(
        "*emergent_pattern_task_class.sql"))
    assert hits, f"no *emergent_pattern_task_class.sql under {REPO_ROOT}/db/migrations"
    return hits[0]


def test_migration_adds_the_column_and_backfills_from_the_label(tmp_path):
    """The migration SQL, applied by the real runner to a temp DB with the
    three live label shapes, sets the class from each label and leaves a
    tokenless label NULL."""
    db = tmp_path / "state.db"
    seed = [("ra-1", "cluster: task_class=recipe_authoring status=success (freq=46 in window)"),
            ("fe-1", "cluster: task_class=framework_edit status=failure (freq=9 in window)"),
            ("cf-1", "cluster: task_class=code_fix status=failure (freq=11 in window)"),
            ("nn-1", "cluster: status=mixed (freq=3 in window)")]
    con = sqlite3.connect(str(db))
    con.executescript(EMERGENT_SQL)
    for pid, label in seed:
        con.execute(
            "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
            "member_item_ids_json, feature_set_json, strength_score, status, "
            "detected_at, lesson_text) VALUES (?,?,?,?,5.0,'approved',1000,?)",
            (pid, label, "[]", "[]", "some lesson"))
    con.commit()
    con.close()

    mig = find_migration()
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    shutil.copy(str(mig), str(migdir / mig.name))
    rc, out = migrate_apply(str(migdir), db=str(db), root=str(REPO_ROOT))
    assert rc == 0, f"{mig.name} failed to apply: {out}"

    con = sqlite3.connect(str(db))
    try:
        cols = {row[1]: (row[2] or "").upper() for row in
                con.execute("PRAGMA table_info('emergent_patterns')")}
        got = dict(con.execute("SELECT pattern_id, task_class FROM emergent_patterns"))
    finally:
        con.close()
    assert cols.get("task_class") == "TEXT"
    assert got == {"ra-1": "recipe_authoring", "fe-1": "framework_edit",
                   "cf-1": "code_fix", "nn-1": None}


def test_migration_is_reapply_safe(tmp_path):
    """A second apply must neither fail nor duplicate the column."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.executescript(EMERGENT_SQL)
    con.commit()
    con.close()
    mig = find_migration()
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    shutil.copy(str(mig), str(migdir / mig.name))
    assert migrate_apply(str(migdir), db=str(db), root=str(REPO_ROOT))[0] == 0
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM schema_migrations")       # force a replay
    con.commit()
    con.close()
    rc, out = migrate_apply(str(migdir), db=str(db), root=str(REPO_ROOT))
    assert rc == 0, f"{mig.name} is not reapply-safe: {out}"


# ── the reflection writer ───────────────────────────────────────────────────


def suggestion(pid, description):
    return {
        "pattern_id": pid,
        "description": description,
        "frequency": 3,
        "suggested_promotion_type": "other",
        "evidence_trace_ids": ["t1"],
        "lesson_text": "a freshly authored lesson",
        "rationale": "meets promotion threshold",
    }


def test_reflection_insert_writes_the_task_class(tmp_path):
    """A pattern the reflection pipeline inserts carries its class, parsed
    from the label it was clustered under."""
    db = str(tmp_path / "state.db")
    con = sqlite3.connect(db)
    con.executescript(EMERGENT_SQL)
    con.execute("ALTER TABLE emergent_patterns ADD COLUMN task_class TEXT")
    con.commit()
    con.close()

    payload = json.dumps([
        suggestion("new-cf", "cluster: task_class=code_fix status=failure (freq=11 in window)"),
        suggestion("new-nn", "cluster: status=mixed (freq=3 in window)"),
    ])
    assert _persist_suggestions_upsert(db, payload) == 2

    con = sqlite3.connect(db)
    try:
        got = dict(con.execute(
            "SELECT pattern_id, task_class FROM emergent_patterns "
            "WHERE pattern_id IN ('new-cf','new-nn')"))
    finally:
        con.close()
    assert got == {"new-cf": "code_fix", "new-nn": None}


def test_reflection_insert_survives_a_db_without_the_column(tmp_path):
    """Cold-safe: on a pre-migration DB the insert still persists — the column
    is a bonus, never a precondition for writing the pattern."""
    db = str(tmp_path / "state.db")
    make_db(db, [], with_class_column=False)
    payload = json.dumps([suggestion(
        "new-cf", "cluster: task_class=code_fix status=failure (freq=11 in window)")])
    assert _persist_suggestions_upsert(db, payload) == 1
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT pattern_id, cluster_label FROM emergent_patterns "
                          "WHERE pattern_id = 'new-cf'").fetchone()
    finally:
        con.close()
    assert row is not None and row[0] == "new-cf"


@pytest.mark.parametrize("with_class_column", [True, False])
def test_reflection_written_pattern_is_selectable_by_its_class(tmp_path,
                                                              with_class_column):
    """End to end: what the reflection stage writes, the read-back selects —
    reading the column on the migrated shape and the label on the
    pre-migration one."""
    db = str(tmp_path / "state.db")
    make_db(db, [], with_class_column=with_class_column)
    payload = json.dumps([
        suggestion("new-cf", "cluster: task_class=code_fix status=failure (freq=11 in window)"),
        suggestion("new-fe", "cluster: task_class=framework_edit status=failure (freq=9 in window)"),
    ])
    assert _persist_suggestions_upsert(db, payload) == 2

    # A new row enters as 'proposed'; only the judge gate approves it, and the
    # read-back never sees an unapproved row at any scope.
    assert context_assembler._approved_emergent_rows(
        db, task_class="code_fix") == []

    con = sqlite3.connect(db)
    con.execute("UPDATE emergent_patterns SET status='approved'")
    con.commit()
    con.close()
    got = pids(context_assembler._approved_emergent_rows(db,
                                                         task_class="code_fix"))
    assert got[0] == "new-cf"          # its own class comes first
    assert "new-fe" in got             # the capped other-class top-up
