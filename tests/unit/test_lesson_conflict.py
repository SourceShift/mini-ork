"""Unit tests for ``mini_ork.learning.lesson_conflict`` and its two writers.

Hermetic: every test builds its own temp sqlite database and injects a fake
dispatch callable at the historic 5-arg transport contract; no lane, no
credentials, no network.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import mini_ork.learning.lesson_conflict as lc
import mini_ork.learning.pattern_induction as pi
import mini_ork.learning.themes as themes

LIVE_ID = "th-live1"
CAND_TEXT = "When the verifier reports vacuous: rerun the gate on the diff itself"
LIVE_TEXT = "When the verifier reports vacuous: always rerun the gate on the diff itself"
FAR_TEXT = "When cost budgets are exceeded: prefer cheaper sibling lenses first"
PAT_ID = "pat-1"
CAND_ID = "th-cand1"
MEMBER_TRACES = ["tr-a", "tr-b", "tr-c"]


# ── fixtures ────────────────────────────────────────────────────────────────


def _env(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_LESSON_CONFLICT_CHECK", raising=False)
    monkeypatch.delenv("MO_PATTERN_INDUCE", raising=False)
    monkeypatch.setenv("MO_THEME_PROMOTE_MIN_MEMBERS", "1")
    return db


def _minimal_db(tmp_path):
    """The two lesson sources + the oversight channel; no migrations."""
    con = sqlite3.connect(str(tmp_path / "state.db"))
    con.executescript(
        """
        CREATE TABLE lesson_themes (
          theme_id TEXT PRIMARY KEY, kind TEXT NOT NULL, role TEXT, task_class TEXT,
          representative TEXT NOT NULL, centroid BLOB NOT NULL,
          n_gradients INTEGER NOT NULL DEFAULT 0, n_runs INTEGER NOT NULL DEFAULT 0,
          n_at_refresh INTEGER NOT NULL DEFAULT 0, first_seen INTEGER, last_seen INTEGER,
          lesson_text TEXT, status TEXT NOT NULL DEFAULT 'candidate',
          bug_report_id INTEGER, updated_at INTEGER);
        CREATE TABLE emergent_patterns (
          pattern_id TEXT PRIMARY KEY, cluster_label TEXT NOT NULL,
          member_item_ids_json TEXT NOT NULL, feature_set_json TEXT NOT NULL,
          strength_score REAL NOT NULL, suggested_meta_adr TEXT,
          status TEXT NOT NULL DEFAULT 'proposed', detected_at INTEGER NOT NULL,
          resolved_at INTEGER, lesson_text TEXT, task_class TEXT);
        CREATE TABLE mo_inbox_gates (
          inbox_id INTEGER PRIMARY KEY AUTOINCREMENT, gate_id TEXT NOT NULL,
          feature TEXT NOT NULL, phase TEXT, context_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', review_note TEXT,
          enqueued_at INTEGER NOT NULL, resolved_at INTEGER, blocks_dispatch_for TEXT);
        """
    )
    con.commit()
    return con


def _theme(con, tid, task_class, lesson_text, status="verified"):
    con.execute(
        "INSERT INTO lesson_themes (theme_id, kind, task_class, representative,"
        " centroid, n_gradients, n_runs, first_seen, last_seen, lesson_text, status)"
        " VALUES (?, 'task', ?, 'rep', x'7b7d', 3, 9, 1000, 2000, ?, ?)",
        (tid, task_class, lesson_text, status),
    )
    con.commit()


def _writer_db(tmp_path):
    """A promotable candidate theme, a pending pattern, and one live lesson."""
    db = str(tmp_path / "state.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE lesson_themes (
          theme_id TEXT PRIMARY KEY, kind TEXT NOT NULL, role TEXT, task_class TEXT,
          representative TEXT NOT NULL, centroid BLOB NOT NULL,
          n_gradients INTEGER NOT NULL DEFAULT 0, n_runs INTEGER NOT NULL DEFAULT 0,
          n_at_refresh INTEGER NOT NULL DEFAULT 0, first_seen INTEGER, last_seen INTEGER,
          lesson_text TEXT, status TEXT NOT NULL DEFAULT 'candidate',
          bug_report_id INTEGER, updated_at INTEGER);
        CREATE TABLE gradient_records (
          gradient_id TEXT PRIMARY KEY, target TEXT, signal TEXT,
          suggested_change TEXT, evidence TEXT, confidence REAL, created_at INTEGER,
          task_class TEXT);
        CREATE TABLE gradient_theme (gradient_id TEXT PRIMARY KEY, theme_id TEXT NOT NULL,
          similarity REAL);
        CREATE TABLE execution_traces (
          trace_id TEXT PRIMARY KEY, task_class TEXT, status TEXT, verifier_output TEXT,
          reviewer_verdict TEXT, files_written TEXT, files_read TEXT, code_region TEXT,
          reward_g REAL, run_id INTEGER, created_at TEXT);
        CREATE TABLE pattern_records (
          pattern_id TEXT PRIMARY KEY, description TEXT, evidence_trace_ids TEXT,
          frequency INTEGER NOT NULL DEFAULT 1, lesson_text TEXT, task_class TEXT,
          first_seen TEXT, last_seen TEXT, output_type TEXT, cluster_id TEXT);
        CREATE TABLE emergent_patterns (
          pattern_id TEXT PRIMARY KEY, cluster_label TEXT NOT NULL,
          member_item_ids_json TEXT NOT NULL, feature_set_json TEXT NOT NULL,
          strength_score REAL NOT NULL, suggested_meta_adr TEXT,
          status TEXT NOT NULL DEFAULT 'proposed', detected_at INTEGER NOT NULL,
          resolved_at INTEGER, lesson_text TEXT, task_class TEXT);
        CREATE TABLE mo_inbox_gates (
          inbox_id INTEGER PRIMARY KEY AUTOINCREMENT, gate_id TEXT NOT NULL,
          feature TEXT NOT NULL, phase TEXT, context_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', review_note TEXT,
          enqueued_at INTEGER NOT NULL, resolved_at INTEGER, blocks_dispatch_for TEXT);
        """
    )
    con.execute(
        "INSERT INTO lesson_themes (theme_id, kind, task_class, representative,"
        " centroid, n_gradients, n_runs, first_seen, last_seen, lesson_text, status)"
        " VALUES (?, 'task', 'code_fix', 'live rep', x'7b7d', 3, 9, 1000, 2000, ?,"
        " 'verified')",
        (LIVE_ID, LIVE_TEXT),
    )
    con.execute(
        "INSERT INTO lesson_themes (theme_id, kind, task_class, representative,"
        " centroid, n_gradients, n_runs, status)"
        " VALUES (?, 'task', 'code_fix', 'verifier reports vacuous', x'7b7d', 3, 5,"
        " 'candidate')",
        (CAND_ID,),
    )
    for i, tr in enumerate(MEMBER_TRACES):
        con.execute(
            "INSERT INTO gradient_records (gradient_id, target, signal, evidence,"
            " confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?)",
            ("g%d" % i, "verifier.researcher", "verifier reported vacuous output",
             tr, 0.5, 1000 + i, "code_fix"),
        )
        con.execute("INSERT INTO gradient_theme VALUES (?, ?, 1.0)",
                    ("g%d" % i, CAND_ID))
        con.execute(
            "INSERT INTO execution_traces (trace_id, task_class, status,"
            " verifier_output, reviewer_verdict, files_written, files_read,"
            " code_region, reward_g) VALUES (?, 'code_fix', 'failure', 'vacuous',"
            " 'needs_revision', '[]', '[]', '', -1.0)",
            (tr,),
        )
    con.execute(
        "INSERT INTO pattern_records (pattern_id, description, evidence_trace_ids,"
        " frequency, lesson_text, task_class, first_seen, last_seen, output_type,"
        " cluster_id) VALUES (?, 'cluster: task_class=code_fix status=failure"
        " (freq=9 in window)', ?, 9, '', 'code_fix', '1', '2', 'best_practice_rule',"
        " NULL)",
        (PAT_ID, json.dumps(MEMBER_TRACES)),
    )
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label,"
        " member_item_ids_json, feature_set_json, strength_score, status,"
        " detected_at, lesson_text, task_class) VALUES (?, 'cluster: task_class="
        "code_fix status=failure', '[]', '[]', 1.0, 'approved', 1, '', 'code_fix')",
        (PAT_ID,),
    )
    con.commit()
    con.close()
    return db


def _dispatch(reply, calls=None):
    """The historic native dispatch contract: write ``reply`` to ``out_file``."""
    calls = calls if calls is not None else []

    def _fake(model, prompt_text, out_file, timeout_s, max_turns):
        calls.append(model)
        if isinstance(reply, Exception):
            raise reply
        with open(out_file, "w") as fh:
            fh.write(reply)
        return 0

    return _fake


def _rows(con, sql, args=()):
    try:
        cur = con.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


def _count(con, sql, args=()):
    try:
        return int(con.execute(sql, args).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _reasons(report):
    return [
        s.get("reason") for s in report.get("skipped", [])
        if isinstance(s, dict) and s.get("reason")
    ]


CONFLICT_REPLY = json.dumps(
    [{"id": LIVE_ID, "relation": "contradicts", "why": "opposite directive"}])
DUPLICATE_REPLY = json.dumps(
    [{"id": LIVE_ID, "relation": "duplicate", "why": "restatement"}])
REFINES_REPLY = json.dumps(
    [{"id": LIVE_ID, "relation": "refines", "why": "narrower condition"}])
INDEPENDENT_REPLY = json.dumps(
    [{"id": LIVE_ID, "relation": "independent", "why": "distinct lesson"}])


# ── enabled() ───────────────────────────────────────────────────────────────


def test_enabled_defaults_on_and_only_exact_zero_disables(monkeypatch):
    monkeypatch.delenv("MO_LESSON_CONFLICT_CHECK", raising=False)
    assert lc.enabled()
    monkeypatch.setenv("MO_LESSON_CONFLICT_CHECK", "0")
    assert not lc.enabled()
    monkeypatch.setenv("MO_LESSON_CONFLICT_CHECK", "false")
    assert lc.enabled()


# ── live_lessons ────────────────────────────────────────────────────────────


def test_live_lessons_filters_by_status_text_and_class(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    _theme(con, "th-cand", "code_fix", "x", status="candidate")
    _theme(con, "th-blank", "code_fix", "   ")
    _theme(con, "th-star", "*", FAR_TEXT)
    _theme(con, "th-null", None, FAR_TEXT)
    _theme(con, "th-other", "framework_edit", FAR_TEXT)
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label,"
        " member_item_ids_json, feature_set_json, strength_score, status,"
        " detected_at, lesson_text, task_class) VALUES ('ep-1', 'l', '[]', '[]',"
        " 1.0, 'approved', 1, ?, 'code_fix')",
        (LIVE_TEXT,),
    )
    con.commit()
    got = lc.live_lessons(con, "code_fix")
    assert {d["id"] for d in got} == {LIVE_ID, "th-star", "th-null", "ep-1"}
    for d in got:
        assert {"kind", "id", "text"} <= set(d)
    con.close()


def test_live_lessons_missing_table_or_column_skips_source(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    con.execute("DROP TABLE emergent_patterns")
    con.commit()
    assert [d["id"] for d in lc.live_lessons(con, "code_fix")] == [LIVE_ID]
    # A table present but missing task_class: skipped, never a raise.
    con.execute(
        "CREATE TABLE emergent_patterns (pattern_id TEXT PRIMARY KEY,"
        " cluster_label TEXT NOT NULL, member_item_ids_json TEXT NOT NULL,"
        " feature_set_json TEXT NOT NULL, strength_score REAL NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'proposed', detected_at INTEGER NOT NULL,"
        " lesson_text TEXT)"
    )
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label,"
        " member_item_ids_json, feature_set_json, strength_score, status,"
        " detected_at, lesson_text) VALUES ('ep-x','l','[]','[]',1.0,'approved',1,?)",
        (LIVE_TEXT,),
    )
    con.commit()
    assert [d["id"] for d in lc.live_lessons(con, "code_fix")] == [LIVE_ID]
    con.execute("DROP TABLE emergent_patterns")
    con.execute("DROP TABLE lesson_themes")
    con.commit()
    assert lc.live_lessons(con, "code_fix") == []
    con.close()


# ── neighbours ──────────────────────────────────────────────────────────────


def test_neighbours_keeps_a_near_restatement_and_drops_unrelated():
    lessons = [
        {"kind": "theme", "id": LIVE_ID, "text": LIVE_TEXT},
        {"kind": "theme", "id": "th-far", "text": FAR_TEXT},
    ]
    kept = [lesson["id"] for lesson in lc.neighbours(CAND_TEXT, lessons)]
    assert LIVE_ID in kept
    assert "th-far" not in kept


def test_neighbours_uses_min_denominator_and_inclusive_boundary():
    subset = {"kind": "theme", "id": "sub", "text": "RERUN the gate on the diff itself"}
    assert [x["id"] for x in lc.neighbours(CAND_TEXT, [subset], min_overlap=0.4)] == ["sub"]
    # 3 shared of 10 tokens each -> exactly 0.3, kept because the rule is >=.
    x = "one two three four five six seven eight nine ten"
    y = "one two three eleven twelve thirteen fourteen fifteen sixteen seventeen"
    assert [i["id"] for i in lc.neighbours(
        x, [{"kind": "theme", "id": "edge", "text": y}])] == ["edge"]


def test_neighbours_honours_k():
    near = [
        {"kind": "theme", "id": "n%d" % i, "text": LIVE_TEXT + " extra%d" % i}
        for i in range(7)
    ]
    kept = lc.neighbours(CAND_TEXT, near, k=5)
    assert len(kept) == 5
    assert all(lesson in near for lesson in kept)


# ── check ───────────────────────────────────────────────────────────────────


def test_check_with_no_live_lessons_is_ok_and_never_dispatches(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    calls = []
    result = lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="theme",
        source_id="th-src", dispatch_fn=_dispatch("[]", calls),
    )
    assert result == {"verdict": "ok", "relations": []}
    assert calls == []
    con.close()


def test_check_conflict_records_one_row_and_one_oversight_item(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    con.row_factory = sqlite3.Row
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    calls = []
    result = lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="theme",
        source_id="th-src", dispatch_fn=_dispatch(CONFLICT_REPLY, calls),
        model="spec-lane-x",
    )
    assert result["verdict"] == "conflict"
    assert any(r["relation"] == "contradicts" for r in result["relations"])
    assert calls == ["spec-lane-x"]
    rows = _rows(con, "SELECT * FROM lesson_conflicts")
    assert len(rows) == 1
    assert rows[0]["source_kind"] == "theme"
    assert rows[0]["against_kind"] == "theme"
    assert rows[0]["against_id"] == LIVE_ID
    assert rows[0]["relation"] == "contradicts"
    assert rows[0]["candidate_text"] == CAND_TEXT
    overs = _rows(con, "SELECT gate_id, feature FROM mo_inbox_gates")
    assert overs == [{"gate_id": "lesson-conflict",
                      "feature": "lesson conflict: theme th-src"}]
    con.close()


def test_check_second_identical_call_adds_neither(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    fake = _dispatch(CONFLICT_REPLY)
    for _ in range(2):
        assert lc.check(
            con, CAND_TEXT, task_class="code_fix", source_kind="theme",
            source_id="th-src", dispatch_fn=fake,
        )["verdict"] == "conflict"
    assert _count(con, "SELECT COUNT(*) FROM lesson_conflicts") == 1
    assert _count(
        con, "SELECT COUNT(*) FROM mo_inbox_gates"
        " WHERE gate_id='lesson-conflict'") == 1
    con.close()


def test_check_duplicate_verdict_has_no_oversight(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    result = lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="pattern",
        source_id=PAT_ID, dispatch_fn=_dispatch(DUPLICATE_REPLY),
    )
    assert result["verdict"] == "duplicate"
    rows = _rows(con, "SELECT * FROM lesson_conflicts")
    assert len(rows) == 1 and rows[0]["relation"] == "duplicate"
    assert _count(con, "SELECT COUNT(*) FROM mo_inbox_gates") == 0
    con.close()


def test_check_refines_is_recorded_but_never_blocks(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    result = lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="theme",
        source_id="th-src", dispatch_fn=_dispatch(REFINES_REPLY),
    )
    assert result["verdict"] == "ok"
    rows = _rows(con, "SELECT * FROM lesson_conflicts")
    assert len(rows) == 1 and rows[0]["relation"] == "refines"
    assert _count(con, "SELECT COUNT(*) FROM mo_inbox_gates") == 0
    con.close()


def test_check_independent_is_not_recorded(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    result = lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="theme",
        source_id="th-src", dispatch_fn=_dispatch(INDEPENDENT_REPLY),
    )
    assert result["verdict"] == "ok"
    assert _count(con, "SELECT COUNT(*) FROM lesson_conflicts") == 0
    con.close()


def test_check_contradicts_beats_duplicate(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    _theme(con, "th-live2", "code_fix",
           "When the verifier reports vacuous: never rerun the gate on the diff itself")
    reply = json.dumps([
        {"id": LIVE_ID, "relation": "duplicate", "why": "restatement"},
        {"id": "th-live2", "relation": "contradicts", "why": "opposite"},
    ])
    result = lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="theme",
        source_id="th-src", dispatch_fn=_dispatch(reply),
    )
    assert result["verdict"] == "conflict"
    assert _count(
        con, "SELECT COUNT(*) FROM lesson_conflicts WHERE source_id='th-src'") == 2
    con.close()


@pytest.mark.parametrize("reply", [
    RuntimeError("dispatch lane down"),
    "I cannot answer in JSON, sorry.",
    '[{"id": "%s", "relation": "duplicate"' % LIVE_ID,
    json.dumps({"lessons": []}),
    # Well-formed, but judges none of the neighbours shown: fail closed.
    "[]",
    json.dumps([{"id": "no-such-lesson", "relation": "independent"}]),
])
def test_check_failure_or_unparseable_reply_is_unchecked(monkeypatch, tmp_path, reply):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    result = lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="theme",
        source_id="th-src", dispatch_fn=_dispatch(reply),
    )
    assert result["verdict"] == "unchecked"
    assert _count(con, "SELECT COUNT(*) FROM lesson_conflicts") == 0
    assert _count(con, "SELECT COUNT(*) FROM mo_inbox_gates") == 0
    con.close()


def test_check_creates_the_table_with_the_declared_unique_key(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    con = _minimal_db(tmp_path)
    _theme(con, LIVE_ID, "code_fix", LIVE_TEXT)
    lc.check(
        con, CAND_TEXT, task_class="code_fix", source_kind="theme",
        source_id="th-src", dispatch_fn=_dispatch(REFINES_REPLY),
    )
    info = con.execute("PRAGMA table_info(lesson_conflicts)").fetchall()
    assert {row[1] for row in info} == {
        "id", "source_kind", "source_id", "against_kind", "against_id",
        "relation", "why", "candidate_text", "created_at",
    }
    first = con.execute(
        "SELECT source_kind, source_id, against_kind, against_id"
        " FROM lesson_conflicts").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO lesson_conflicts (source_kind, source_id, against_kind,"
            " against_id, relation, created_at) VALUES (?,?,?,?,'duplicate',1)",
            tuple(first),
        )
    con.close()


# ── writers ─────────────────────────────────────────────────────────────────


def test_promote_conflict_blocks_the_write_and_names_it(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _writer_db(tmp_path)
    monkeypatch.setattr(pi, "induce_cluster", lambda *a, **k: (CAND_TEXT, {}))
    report = themes.promote(
        db_path=db, min_runs=1, dispatch_fn=_dispatch(CONFLICT_REPLY))
    assert report["promoted"] == 0
    assert "lesson conflict" in _reasons(report)
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT status, COALESCE(lesson_text, '') FROM lesson_themes"
            " WHERE theme_id=?", (CAND_ID,)).fetchone() == ("candidate", "")
        assert _count(con, "SELECT COUNT(*) FROM lesson_conflicts") == 1
        assert _count(
            con, "SELECT COUNT(*) FROM mo_inbox_gates"
            " WHERE gate_id='lesson-conflict'") == 1
    finally:
        con.close()


def test_promote_ok_verdict_writes_exactly_as_before(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _writer_db(tmp_path)
    monkeypatch.setattr(pi, "induce_cluster", lambda *a, **k: (CAND_TEXT, {}))
    report = themes.promote(
        db_path=db, min_runs=1, dispatch_fn=_dispatch(INDEPENDENT_REPLY))
    assert report["promoted"] == 1
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT status, COALESCE(lesson_text, '') FROM lesson_themes"
            " WHERE theme_id=?", (CAND_ID,)).fetchone() == ("verified", CAND_TEXT)
        assert _count(con, "SELECT COUNT(*) FROM lesson_conflicts") == 0
        assert _count(con, "SELECT COUNT(*) FROM mo_inbox_gates") == 0
    finally:
        con.close()


def test_promote_unchecked_fails_closed(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _writer_db(tmp_path)
    monkeypatch.setattr(pi, "induce_cluster", lambda *a, **k: (CAND_TEXT, {}))
    report = themes.promote(
        db_path=db, min_runs=1, dispatch_fn=_dispatch("not json"))
    assert report["promoted"] == 0
    assert "conflict check unavailable" in _reasons(report)
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT status, COALESCE(lesson_text, '') FROM lesson_themes"
            " WHERE theme_id=?", (CAND_ID,)).fetchone() == ("candidate", "")
    finally:
        con.close()


def test_induce_pending_duplicate_stays_blank(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _writer_db(tmp_path)
    monkeypatch.setattr(pi, "induce_cluster", lambda *a, **k: (CAND_TEXT, {}))
    report = pi.induce_pending(
        db_path=db, min_cluster=2, dispatch_fn=_dispatch(DUPLICATE_REPLY))
    assert report["induced"] == 0
    assert "lesson duplicate" in _reasons(report)
    con = sqlite3.connect(db)
    try:
        pat = con.execute(
            "SELECT COALESCE(lesson_text, '') FROM pattern_records"
            " WHERE pattern_id=?", (PAT_ID,)).fetchone()[0]
        ep = con.execute(
            "SELECT COALESCE(lesson_text, '') FROM emergent_patterns"
            " WHERE pattern_id=?", (PAT_ID,)).fetchone()[0]
        assert pat == "" and ep == ""
        assert _count(con, "SELECT COUNT(*) FROM lesson_conflicts") == 1
        assert _count(con, "SELECT COUNT(*) FROM mo_inbox_gates") == 0
    finally:
        con.close()


def test_induce_pending_unchecked_fails_closed(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _writer_db(tmp_path)
    monkeypatch.setattr(pi, "induce_cluster", lambda *a, **k: (CAND_TEXT, {}))
    report = pi.induce_pending(
        db_path=db, min_cluster=2, dispatch_fn=_dispatch("not json"))
    assert report["induced"] == 0
    assert "conflict check unavailable" in _reasons(report)
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT COALESCE(lesson_text, '') FROM pattern_records"
            " WHERE pattern_id=?", (PAT_ID,)).fetchone()[0] == ""
    finally:
        con.close()


def test_flag_off_both_writers_behave_exactly_as_before(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_LESSON_CONFLICT_CHECK", "0")
    _writer_db(tmp_path)
    monkeypatch.setattr(pi, "induce_cluster", lambda *a, **k: (CAND_TEXT, {}))
    # Even a contradictory judge reply is never consulted.
    report = themes.promote(
        db_path=db, min_runs=1, dispatch_fn=_dispatch(CONFLICT_REPLY))
    assert report["promoted"] == 1
    report = pi.induce_pending(
        db_path=db, min_cluster=2, dispatch_fn=_dispatch(CONFLICT_REPLY))
    assert report["induced"] == 1
    con = sqlite3.connect(db)
    try:
        assert _count(con, "SELECT COUNT(*) FROM lesson_conflicts") == 0
        assert _count(con, "SELECT COUNT(*) FROM mo_inbox_gates") == 0
        assert con.execute(
            "SELECT COALESCE(lesson_text, '') FROM pattern_records"
            " WHERE pattern_id=?", (PAT_ID,)).fetchone()[0] == CAND_TEXT
    finally:
        con.close()
