"""Contract tests for the LF-R4 learned-rule model: origin / source_ids / active.

A rule the "Your code" tab drafts is ``origin='learned'`` and INACTIVE. It is
listed (the Rules tab offers Approve on it) but it must never reach a prompt
until it is approved — and once approved it renders under the "Learned rules"
heading, never under "set by the user", because that heading is a claim about
WHO set the rule.

``ensure_schema`` is the runtime twin of ``db/migrations/0074_preference_origin.sql``:
a state.db that never ran the migration must still accept a learned draft. Every
DB here lives under ``tmp_path``; nothing touches the live home.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mini_ork.memory import preferences

_OPERATOR_HEADING = (
    "--- Operator preferences and constraints (set by the user; follow them) ---"
)
_LEARNED_HEADING = (
    "--- Learned rules (from review findings — check they still apply "
    "before following) ---"
)
_LEARNED_CLOSE = "--- /learned rules ---"

# The post-0065 / pre-0074 shape: 'path' is already in the CHECK (so the old
# fast path returned early) but none of the provenance columns exist.
_OLD_DDL = """
CREATE TABLE user_preference_memory (
  user_id             TEXT    NOT NULL,
  preference_key      TEXT    NOT NULL,
  preference_value    TEXT    NOT NULL DEFAULT '{}',
  scope               TEXT    NOT NULL DEFAULT 'global'
                      CHECK (scope IN ('global','task_class','workflow','path')),
  scope_target        TEXT    NOT NULL DEFAULT '',
  set_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (user_id, preference_key, scope, scope_target)
);
"""


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    return home


def _row(key, db) -> dict:
    return next(r for r in preferences.list_prefs(db=str(db)) if r["key"] == key)


def _columns(path) -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(user_preference_memory)")}
    finally:
        con.close()


def _draft(db, key="review-1", *, text="Reviewers repeatedly found: leak",
           target="mini_ork/ide_pages/node.py", active=False) -> None:
    preferences.set_pref(
        key, text, scope="path", target=target, origin="learned",
        source_ids=["k1", "k2"], active=active, db=str(db))


# ─── set_pref / list_prefs ─────────────────────────────────────────────────


def test_set_pref_defaults_to_an_active_operator_rule(tmp_path):
    """An existing caller (no new kwargs) keeps writing a live operator rule."""
    db = tmp_path / "state.db"
    preferences.set_pref("op", "be terse", db=str(db))
    row = _row("op", db)
    assert row["origin"] == "operator"
    assert row["source_ids"] is None
    assert bool(row["active"]) is True


def test_set_pref_stores_a_learned_draft_with_provenance(tmp_path):
    db = tmp_path / "state.db"
    _draft(db)
    row = _row("review-1", db)
    assert row["origin"] == "learned"
    assert not row["active"]
    assert json.loads(row["source_ids"]) == ["k1", "k2"]


def test_set_pref_rejects_an_unknown_origin(tmp_path):
    with pytest.raises(ValueError) as exc:
        preferences.set_pref("x", "v", origin="magic", db=str(tmp_path / "state.db"))
    assert "operator" in str(exc.value) and "learned" in str(exc.value)


# ─── prefs_for / render_block ──────────────────────────────────────────────


def test_a_draft_is_listed_but_never_returned_or_rendered(tmp_path):
    """The Rules tab can offer Approve on it; no prompt can ever see it."""
    db = tmp_path / "state.db"
    _draft(db)
    assert any(r["key"] == "review-1" for r in preferences.list_prefs(db=str(db)))

    got = preferences.prefs_for(
        "framework_edit", paths=["mini_ork/ide_pages/node.py"], db=str(db))
    assert not any(r["key"] == "review-1" for r in got)
    block = preferences.render_block(got)
    assert "leak" not in block
    assert _LEARNED_HEADING not in block and _OPERATOR_HEADING not in block


def test_approve_pref_promotes_and_keeps_the_learned_origin(tmp_path):
    db = tmp_path / "state.db"
    _draft(db)
    assert preferences.approve_pref(
        "review-1", scope="path", target="mini_ork/ide_pages/node.py", db=str(db))

    got = preferences.prefs_for(
        "framework_edit", paths=["mini_ork/ide_pages/node.py"], db=str(db))
    row = next(r for r in got if r["key"] == "review-1")
    assert row["origin"] == "learned", "approving promotes, it does not re-brand"
    block = preferences.render_block(got)
    assert _LEARNED_HEADING in block
    assert "set by the user" not in block


def test_approve_pref_with_text_replaces_the_value(tmp_path):
    db = tmp_path / "state.db"
    _draft(db)
    assert preferences.approve_pref(
        "review-1", scope="path", target="mini_ork/ide_pages/node.py",
        text="prefer the context manager", db=str(db))
    row = _row("review-1", db)
    assert row["value"] == "prefer the context manager"
    assert bool(row["active"]) is True


def test_approve_pref_reports_a_missing_row(tmp_path):
    db = tmp_path / "state.db"
    preferences.ensure_schema(db=str(db))
    assert not preferences.approve_pref(
        "review-nope", scope="path", target="mini_ork/cli/*", db=str(db))


def test_operator_and_learned_render_as_two_blocks(tmp_path):
    """Two blocks, operator first, each with its own heading and close."""
    db = tmp_path / "state.db"
    preferences.set_pref("op-mix", "Operator first", db=str(db))
    preferences.set_pref(
        "review-mix", "Reviewers repeatedly found: leak", scope="path",
        target="mini_ork/cli/*", origin="learned", source_ids=["k"], db=str(db))

    got = preferences.prefs_for("framework_edit", paths=["mini_ork/cli/a.py"],
                                db=str(db))
    block = preferences.render_block(got)
    assert _OPERATOR_HEADING in block and "--- /operator preferences ---" in block
    assert _LEARNED_HEADING in block and _LEARNED_CLOSE in block
    assert block.index(_OPERATOR_HEADING) < block.index(_LEARNED_HEADING)
    assert "Operator first" in block and "leak" in block


def test_operator_only_render_is_byte_identical(tmp_path):
    """The out-of-scope consumers pin the pre-LF-R4 single-block shape."""
    db = tmp_path / "state.db"
    preferences.set_pref("op-global", "Be kind to reviewers", db=str(db))
    got = preferences.prefs_for("framework_edit", db=str(db))
    assert preferences.render_block(got) == (
        f"{_OPERATOR_HEADING}\n"
        "- Be kind to reviewers\n"
        "--- /operator preferences ---\n"
    )


# ─── ensure_schema (the migration's runtime twin) ──────────────────────────


def test_ensure_schema_backfills_a_pre_migration_db(tmp_path):
    """A DB migrated to 0065 but not 0074 gains the columns, rows keep working.

    The old fast path returned as soon as ``'path'`` was in the CHECK, so this
    shape silently kept the table without any provenance column.
    """
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.executescript(_OLD_DDL)
    con.execute(
        "INSERT INTO user_preference_memory "
        "(user_id, preference_key, preference_value, scope, scope_target) "
        "VALUES ('default', 'legacy-rule', 'be careful', 'global', '')")
    con.commit()
    con.close()

    preferences.ensure_schema(db=str(db))

    assert {"origin", "source_ids", "active"} <= _columns(db)
    row = _row("legacy-rule", db)
    assert row["origin"] == "operator"
    assert row["source_ids"] is None
    assert int(row["active"]) == 1
    # Idempotent, and the table still accepts a path-scoped draft.
    preferences.ensure_schema(db=str(db))
    _draft(db)
    assert _row("review-1", db)["origin"] == "learned"


def test_ensure_schema_cold_start_has_the_columns(tmp_path):
    db = tmp_path / "cold" / "state.db"
    db.parent.mkdir(parents=True)
    preferences.ensure_schema(db=str(db))
    assert {"origin", "source_ids", "active"} <= _columns(db)
