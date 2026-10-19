"""Unit tests: the reversible retirement lifecycle layered on the semantic
memory win/loss ledger.

Hermetic — a fresh tmp ``db_path`` per test, no network, no lane, no model.
``uses`` / ``wins`` are set by direct SQL so the number under assertion is
exactly the number written, never an inference about the ledger.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import memory_lifecycle
from mini_ork.memory import (
    RETIRE_ENTER_UTILITY,
    RETIRE_EXIT_UTILITY,
    RETIRE_MIN_USES,
    candidates,
    rank_with_prior,
    reactivate,
    retire,
    retirement_state,
    search,
    upsert,
)
from mini_ork.memory.retirement import rule_candidates, theme_candidates
from mini_ork.memory.semantic import _connect, _utility


def _seed(db: str, scope: str, text: str, key: str, uses: int, wins: int) -> int:
    """Upsert one keyed memory, then pin its uses/wins by direct SQL."""
    mid = upsert(text, scope=scope, key=key, db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE semantic_memory SET uses = ?, wins = ? WHERE id = ?",
        (uses, wins, mid),
    )
    conn.commit()
    conn.close()
    return mid


# ─── 1. reversibility of the flag itself ────────────────────────────────────


def test_retire_then_reactivate_is_reversible(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("use the auth middleware", scope="eng", key="k1", db_path=db)

    assert retire(mid, "wrong guidance", evidence_ref="run-42", db_path=db) is True
    st = retirement_state(mid, db_path=db)
    assert st is not None
    assert st["retired"] is True
    assert st["reason"] == "wrong guidance"
    assert st["evidence"] == "run-42"

    assert reactivate(mid, db_path=db) is True
    st = retirement_state(mid, db_path=db)
    assert st is not None
    assert st["retired"] is False
    assert st["retired_at"] == 0
    assert st["reason"] == ""
    assert st["evidence"] == ""


# ─── 2. unknown ids ─────────────────────────────────────────────────────────


def test_unknown_id(tmp_path):
    db = str(tmp_path / "mem.db")
    assert retire(999, "reason", db_path=db) is False
    assert reactivate(999, db_path=db) is False
    assert retirement_state(999, db_path=db) is None


# ─── 3. re-retiring preserves the original stamp ────────────────────────────


def test_reretire_preserves_original(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("t", scope="eng", key="k", db_path=db)

    assert retire(mid, "first reason", evidence_ref="ev1", db_path=db) is True
    first = retirement_state(mid, db_path=db)
    assert first is not None

    assert retire(mid, "second reason", evidence_ref="ev2", db_path=db) is True
    second = retirement_state(mid, db_path=db)
    assert second is not None

    assert second["reason"] == "first reason"
    assert second["evidence"] == "ev1"
    assert second["retired_at"] == first["retired_at"]


# ─── 4. an empty reason is rejected ─────────────────────────────────────────


def test_retire_requires_reason(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("t", scope="eng", key="k", db_path=db)
    with pytest.raises(ValueError):
        retire(mid, "", db_path=db)
    with pytest.raises(ValueError):
        retire(mid, "   ", db_path=db)


# ─── 5. end-to-end suppression through the real retrieval path ──────────────


def test_search_suppresses_and_recovers(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("the dispatcher must resolve an empty home", scope="eng",
                 key="k1", db_path=db)

    assert any(r["memory_id"] == mid for r in search("empty home", scope="eng", db_path=db))
    assert retire(mid, "obsolete", db_path=db) is True
    assert not any(r["memory_id"] == mid for r in search("empty home", scope="eng", db_path=db))
    assert reactivate(mid, db_path=db) is True
    assert any(r["memory_id"] == mid for r in search("empty home", scope="eng", db_path=db))


# ─── 6. rank_with_prior also excludes a retired memory ──────────────────────


def test_rank_with_prior_excludes_retired(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("alpha fact", scope="eng", key="k1", db_path=db)

    assert any(r["memory_id"] == mid
               for r in rank_with_prior([(mid, 1.0)], scope="eng", db_path=db))
    retire(mid, "obsolete", db_path=db)
    assert not any(r["memory_id"] == mid
                   for r in rank_with_prior([(mid, 1.0)], scope="eng", db_path=db))


# ─── 7. the resurrection hazard: re-upsert must not clear the flag ──────────


def test_upsert_does_not_resurrect_retired(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("original text", scope="eng", key="k1", db_path=db)
    retire(mid, "obsolete", db_path=db)

    mid2 = upsert("refreshed text", scope="eng", key="k1", db_path=db)
    assert mid2 == mid

    st = retirement_state(mid, db_path=db)
    assert st is not None
    assert st["retired"] is True
    assert st["reason"] == "obsolete"
    assert not any(r["memory_id"] == mid
                   for r in search("refreshed text", scope="eng", db_path=db))


# ─── 8. candidates hysteresis ───────────────────────────────────────────────


def test_candidates_hysteresis(tmp_path):
    db = str(tmp_path / "mem.db")
    m_retire = _seed(db, "eng", "always wrong", "k1", 6, 0)   # utility 0.125 < enter
    m_ok = _seed(db, "eng", "always right", "k2", 6, 6)        # utility 0.875
    m_recover = _seed(db, "eng", "recovered", "k3", 6, 6)      # utility 0.875 > exit
    retire(m_recover, "was wrong then", db_path=db)
    m_new = _seed(db, "eng", "too new", "k4", 2, 0)            # below min_uses

    rows = candidates("eng", db_path=db)
    by_id = {r["memory_id"]: r for r in rows}

    assert m_retire in by_id
    assert by_id[m_retire]["state"] == "active"
    assert by_id[m_retire]["recommendation"] == "retire"

    assert m_ok not in by_id

    assert m_recover in by_id
    assert by_id[m_recover]["state"] == "retired"
    assert by_id[m_recover]["recommendation"] == "reactivate"

    assert m_new not in by_id

    # sorted by utility ascending
    utils = [r["utility"] for r in rows]
    assert utils == sorted(utils)


# ─── 9. non-asymmetric hysteresis is rejected ───────────────────────────────


def test_candidates_rejects_non_asymmetric(tmp_path):
    db = str(tmp_path / "mem.db")
    with pytest.raises(ValueError):
        candidates("eng", db_path=db, enter=RETIRE_EXIT_UTILITY, exit=RETIRE_EXIT_UTILITY)
    with pytest.raises(ValueError):
        candidates("eng", db_path=db, enter=0.5, exit=0.4)


# ─── 10. the guarded ALTER adds the retire columns to an older DB ───────────


def test_ensure_columns_adds_retire_columns(tmp_path):
    db_path = str(tmp_path / "mem.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE semantic_memory ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " scope TEXT NOT NULL, text TEXT NOT NULL, embedding BLOB NOT NULL,"
        " created_at REAL NOT NULL, meta TEXT,"
        " uses INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    conn = _connect(db_path)  # bootstrap: _BOOTSTRAP_SQL + _ensure_columns
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory)")}
    conn.close()

    assert {"retired_at", "retire_reason", "retire_evidence"} <= cols


# ─── 11. the CLI is registered ──────────────────────────────────────────────


def test_cli_registered():
    from mini_ork.cli.main import SUBCOMMAND_REGISTRY
    assert "memory-lifecycle" in SUBCOMMAND_REGISTRY


# ─── constants exist as documented ──────────────────────────────────────────


def test_module_constants():
    assert RETIRE_ENTER_UTILITY < RETIRE_EXIT_UTILITY
    assert RETIRE_MIN_USES > 0
    assert _utility(6, 0) == 0.125


# ─── 12. theme candidates: report-only, age window, missing schema ───────────


def _theme_db(tmp_path, rows):
    """A tmp DB with just enough ``lesson_themes`` for the reader."""
    db = str(tmp_path / "state.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE lesson_themes (
          theme_id      TEXT PRIMARY KEY,
          kind          TEXT NOT NULL,
          role          TEXT,
          task_class    TEXT,
          representative TEXT NOT NULL,
          centroid      BLOB NOT NULL,
          n_gradients   INTEGER NOT NULL DEFAULT 0,
          n_runs        INTEGER NOT NULL DEFAULT 0,
          n_at_refresh  INTEGER NOT NULL DEFAULT 0,
          first_seen    INTEGER,
          last_seen     INTEGER,
          lesson_text   TEXT,
          status        TEXT NOT NULL DEFAULT 'candidate',
          bug_report_id INTEGER,
          updated_at    INTEGER
        );
        """
    )
    for theme_id, last_seen, status in rows:
        conn.execute(
            "INSERT INTO lesson_themes (theme_id, kind, representative, centroid, "
            "n_runs, last_seen, status) VALUES (?, 'task', ?, X'', 2, ?, ?)",
            (theme_id, f"rep {theme_id}", last_seen, status),
        )
    conn.commit()
    conn.close()
    return db


def test_theme_candidates_default_window_and_sorting(tmp_path):
    now = int(time.time())
    day = 86400
    db = _theme_db(tmp_path, [
        ("oldest", now - 200 * day, "verified"),
        ("middle", now - 100 * day, "verified"),
        ("fresh", now - 10 * day, "verified"),
        ("candidate", now - 300 * day, "candidate"),
    ])
    # Default 90-day window ending at now: the 200- and 100-day themes are
    # older than it; the 10-day theme is not; the non-verified row never
    # appears. Oldest first.
    rows = theme_candidates(db_path=db, now=now)
    assert [r["theme_id"] for r in rows] == ["oldest", "middle"]
    assert rows[0]["kind"] == "theme"
    assert rows[0]["state"] == "verified"
    assert rows[0]["recommendation"] == "retire"
    assert "200 days ago" in rows[0]["evidence"]

    # A wider window reaches further back from now, so it admits FEWER rows:
    # nothing verified here is older than 365 days.
    wide = theme_candidates(db_path=db, max_age_days=365, now=now)
    assert wide == []


def test_theme_candidates_max_age_zero_disables(tmp_path):
    now = int(time.time())
    db = _theme_db(tmp_path, [("t", now - 500 * 86400, "verified")])
    assert theme_candidates(db_path=db, max_age_days=0, now=now) == []
    assert theme_candidates(db_path=db, max_age_days=-1, now=now) == []


def test_theme_candidates_missing_schema_is_empty(tmp_path):
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    assert theme_candidates(db_path=db) == []
    assert theme_candidates(db_path=str(tmp_path / "absent.db")) == []
    # A pre-last_seen table is a missing-column error → [] rather than a raise.
    old = str(tmp_path / "old.db")
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE lesson_themes (theme_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert theme_candidates(db_path=old) == []


def test_theme_candidates_never_writes(tmp_path):
    now = int(time.time())
    db = _theme_db(tmp_path, [("t", now - 200 * 86400, "verified")])
    before = Path(db).read_bytes()
    theme_candidates(db_path=db, now=now)
    assert Path(db).read_bytes() == before


# ─── 13. rule candidates: all sources stale ─────────────────────────────────


def _rule_db(tmp_path, prefs, findings):
    db = str(tmp_path / "state.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE user_preference_memory (
          user_id TEXT NOT NULL,
          preference_key TEXT NOT NULL,
          preference_value TEXT NOT NULL DEFAULT '{}',
          scope TEXT NOT NULL DEFAULT 'global',
          scope_target TEXT NOT NULL DEFAULT '',
          set_at TEXT NOT NULL DEFAULT '',
          origin TEXT NOT NULL DEFAULT 'operator',
          source_ids TEXT,
          active INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (user_id, preference_key, scope, scope_target)
        );
        CREATE TABLE code_findings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          fingerprint TEXT NOT NULL UNIQUE,
          run_id TEXT NOT NULL,
          source TEXT NOT NULL,
          file TEXT,
          line INTEGER,
          severity TEXT NOT NULL,
          category TEXT NOT NULL,
          issue TEXT NOT NULL,
          snippet TEXT,
          verdict TEXT,
          ts INTEGER NOT NULL,
          target_repo TEXT
        );
        """
    )
    for fid, ts in findings:
        conn.execute(
            "INSERT INTO code_findings (id, fingerprint, run_id, source, "
            "severity, category, issue, ts) VALUES (?, ?, 'r', 'reviewer', "
            "'medium', 'correctness', 'issue', ?)",
            (fid, f"fp-{fid}", ts),
        )
    for key, ids, origin, active in prefs:
        conn.execute(
            "INSERT INTO user_preference_memory (user_id, preference_key, "
            "preference_value, origin, source_ids, active) "
            "VALUES ('u', ?, ?, ?, ?, ?)",
            (key, f"value for {key}", origin,
             json.dumps(ids) if ids is not None else None, active),
        )
    conn.commit()
    conn.close()
    return db


def test_rule_candidates_reports_only_all_stale_active_learned(tmp_path):
    now = int(time.time())
    old = now - 400 * 86400
    db = _rule_db(
        tmp_path,
        prefs=[
            ("rule-stale", [1, 2], "learned", 1),
            ("rule-fresh", [3], "learned", 1),
            ("rule-mixed", [1, 3], "learned", 1),
            ("rule-operator", [1], "operator", 1),
            ("rule-inactive", [1], "learned", 0),
            ("rule-empty", [], "learned", 1),
            ("rule-unparsable", None, "learned", 1),
            ("rule-gone", [999], "learned", 1),
        ],
        findings=[(1, old), (2, old), (3, now)],
    )
    rows = rule_candidates(db_path=db, now=now)
    assert [r["preference_key"] for r in rows] == ["rule-stale"]
    row = rows[0]
    assert row["kind"] == "rule"
    assert row["state"] == "active"
    assert row["recommendation"] == "retire"
    assert row["source_ids"] == [1, 2]
    assert row["evidence"] == "2 of 2 source findings stale"


def test_rule_candidates_missing_schema_is_empty(tmp_path):
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    assert rule_candidates(db_path=db) == []
    assert rule_candidates(db_path=str(tmp_path / "absent.db")) == []


def test_rule_candidates_never_writes(tmp_path):
    now = int(time.time())
    db = _rule_db(tmp_path, [("k", [1], "learned", 1)],
                  [(1, now - 400 * 86400)])
    before = Path(db).read_bytes()
    rule_candidates(db_path=db, now=now)
    assert Path(db).read_bytes() == before


# ─── 14. CLI --kind, always exit 0 ──────────────────────────────────────────


def test_cli_kind_theme_is_report_only_and_exits_zero(tmp_path, monkeypatch):
    now = int(time.time())
    db = _theme_db(tmp_path, [("t", now - 200 * 86400, "verified")])
    monkeypatch.setenv("MINI_ORK_DB", db)
    out, err = io.StringIO(), io.StringIO()

    rc = memory_lifecycle.main(["--candidates", "--kind", "theme", "--json"],
                               stdout=out, stderr=err)
    assert rc == 0
    rows = json.loads(out.getvalue())
    assert [r["theme_id"] for r in rows] == ["t"]
    assert err.getvalue() == ""


def test_cli_kind_rule_json(tmp_path, monkeypatch):
    now = int(time.time())
    db = _rule_db(tmp_path, [("k", [1], "learned", 1)],
                  [(1, now - 400 * 86400)])
    monkeypatch.setenv("MINI_ORK_DB", db)
    out = io.StringIO()
    rc = memory_lifecycle.main(["--candidates", "--kind", "rule", "--json"],
                               stdout=out, stderr=io.StringIO())
    assert rc == 0
    assert [r["preference_key"] for r in json.loads(out.getvalue())] == ["k"]


def test_cli_invalid_kind_exits_zero_and_prints_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    out, err = io.StringIO(), io.StringIO()
    rc = memory_lifecycle.main(["--candidates", "--kind", "bogus"],
                               stdout=out, stderr=err)
    assert rc == 0
    assert "invalid --kind: bogus" in err.getvalue()
    assert "Usage: mini-ork memory-lifecycle" in out.getvalue()


def test_cli_kind_all_with_scope_includes_semantic(tmp_path, monkeypatch):
    db = str(tmp_path / "mem.db")
    mid = _seed(db, "eng", "always wrong", "k1", 6, 0)
    _theme_db(tmp_path, [("t", int(time.time()) - 200 * 86400, "verified")])
    # The theme row lives in state.db; point the reader at the same DB the
    # semantic reader uses (semantic bootstraps on the same tmp file).
    monkeypatch.setenv("MINI_ORK_DB", db)
    out = io.StringIO()
    rc = memory_lifecycle.main(
        ["--candidates", "--kind", "all", "--scope", "eng", "--json"],
        stdout=out, stderr=io.StringIO())
    assert rc == 0
    rows = json.loads(out.getvalue())
    assert any(r.get("memory_id") == mid for r in rows)
