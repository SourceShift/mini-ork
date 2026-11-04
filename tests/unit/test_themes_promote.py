"""Tests for ``themes.promote`` — the lesson_themes producer.

Clusters live in ``theme_clusters``; ``lesson_themes`` holds only the authored
rows ``promote`` copies in. The v1/v2 lesson blocks read only
``status='verified'`` rows with a non-blank ``lesson_text``, so a cluster-only
store renders nothing. These lock the producer: gating, the support bar,
member resolution, the ``candidate → verified`` transition, idempotency, and
the consumer read that the promotion unblocks.

``induce_cluster`` (the LLM author) is stubbed so the DB logic is tested
deterministically, with no live lane.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
INIT_SH = REPO / "db" / "init.sh"

from mini_ork.learning import pattern_induction, themes  # noqa: E402

LESSON = "Prefer the repo's existing helper over a new one; the new one drifts."


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


@pytest.fixture
def stub_inductor(monkeypatch):
    def _fake(con, *, target, member_trace_ids, dispatch_fn=None, model=None):
        return LESSON, {"target": target, "n_kept": 1}
    monkeypatch.setattr(pattern_induction, "induce_cluster", _fake)


def _seed_theme(dbp, theme_id, *, n_runs, task_class="code_fix"):
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        "INSERT INTO theme_clusters(theme_id, kind, task_class, representative,"
        " centroid, n_gradients, n_runs)"
        " VALUES (?, 'task', ?, ?, ?, ?, ?)",
        (theme_id, task_class, "rep-" + theme_id, b"\x00", n_runs, n_runs),
    )
    con.commit()
    con.close()


def _seed_members(dbp, theme_id, n):
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA busy_timeout=5000")
    for i in range(n):
        gid = f"{theme_id}-g{i}"
        con.execute(
            "INSERT INTO gradient_records(gradient_id, target, signal,"
            " suggested_change, evidence, confidence, created_at)"
            " VALUES (?, 't', 's', 'c', ?, 0.5, 0)",
            (gid, f"tr-{theme_id}-{i}"),
        )
        con.execute(
            "INSERT INTO gradient_theme(gradient_id, theme_id, similarity)"
            " VALUES (?, ?, 0.9)", (gid, theme_id))
    con.commit()
    con.close()


def _row(dbp, theme_id):
    """The authored ``lesson_themes`` row for a theme, or ``None``.

    Clusters stay in ``theme_clusters``; only ``promote`` copies one into
    ``lesson_themes``, so an absent row means "never authored".
    """
    con = sqlite3.connect(dbp)
    r = con.execute(
        "SELECT status, lesson_text FROM lesson_themes WHERE theme_id=?",
        (theme_id,)).fetchone()
    con.close()
    return r


def test_disabled_writes_nothing(db, monkeypatch):
    monkeypatch.setenv("MO_PATTERN_INDUCE", "0")
    _seed_theme(db, "t1", n_runs=5)
    _seed_members(db, "t1", 3)

    report = themes.promote(db_path=db, min_runs=3)

    assert report["enabled"] is False
    assert report["promoted"] == 0
    assert _row(db, "t1") is None


def test_authors_and_verifies(db, stub_inductor):
    _seed_theme(db, "t1", n_runs=5)
    _seed_members(db, "t1", 3)

    report = themes.promote(db_path=db, min_runs=3)

    assert report["promoted"] == 1
    assert _row(db, "t1") == ("verified", LESSON)


def test_below_support_bar_not_selected(db, stub_inductor):
    _seed_theme(db, "t1", n_runs=1)   # < min_runs=3
    _seed_members(db, "t1", 3)

    report = themes.promote(db_path=db, min_runs=3)

    assert report["promoted"] == 0
    assert _row(db, "t1") is None


def test_too_few_members_skipped(db, stub_inductor):
    _seed_theme(db, "t1", n_runs=5)
    _seed_members(db, "t1", 1)        # < min_members=3

    report = themes.promote(db_path=db, min_runs=3)

    assert report["promoted"] == 0
    assert report["skipped"][0]["reason"] == "too few member traces"
    assert _row(db, "t1") is None


def test_idempotent(db, stub_inductor):
    _seed_theme(db, "t1", n_runs=5)
    _seed_members(db, "t1", 3)

    assert themes.promote(db_path=db, min_runs=3)["promoted"] == 1
    # A verified row is no longer a blank candidate → nothing left to promote.
    assert themes.promote(db_path=db, min_runs=3)["promoted"] == 0
    assert _row(db, "t1") == ("verified", LESSON)


def test_consumer_reads_promoted_lesson(db, stub_inductor):
    """The point of the producer: the v2 pack's verified_themes() consumer
    returns [] before promotion and the lesson after — closing B3."""
    from mini_ork import context_v2

    _seed_theme(db, "t1", n_runs=5)
    _seed_members(db, "t1", 3)

    assert context_v2.verified_themes("code_fix", db=db) == []
    themes.promote(db_path=db, min_runs=3)
    out = context_v2.verified_themes("code_fix", db=db)
    assert out and out[0]["text"] == LESSON
    assert out[0]["id"] == "t:t1"


def test_cli_promote_emits_json(db, stub_inductor, capsys):
    _seed_theme(db, "t1", n_runs=5)
    _seed_members(db, "t1", 3)

    rc = themes.main(["promote", "--db", db, "--min-runs", "3"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["promoted"] == 1


# ── the split, end to end on a temp DB ──────────────────────────────────────

_AUTHORED = (
    "Verified lesson: confirm the artifact exists before claiming a step "
    "is complete."
)

_TASK_SIGNAL_A = (
    "the upload quietly loses the finished payload when the connection "
    "drops mid retry"
)
_TASK_SIGNAL_B = (
    "the summary widget truncates long titles without any ellipsis marker"
)
_FRAMEWORK_SIGNAL = (
    "verifier_output records finish_reason done for a researcher node "
    "with zero files_written"
)


def test_assign_then_promote_keeps_clusters_out_of_lesson_table(
        tmp_path, monkeypatch):
    """Drive assign_new → promote over fresh gradients on a temp DB.

    Nothing authored may appear in ``lesson_themes`` for a below-bar or
    framework cluster; the one qualifying task cluster is authored with
    non-identifier text; and the two tables together retain every theme id and
    every ``gradient_theme`` row. The ambient ``MINI_ORK_DB`` is pointed at a
    canary and must never be created — this test cannot manufacture G8's
    verdict.
    """
    dbp = str(tmp_path / "state.db")
    canary = tmp_path / "ambient-canary.db"
    monkeypatch.setenv("MINI_ORK_DB", str(canary))
    monkeypatch.setenv("MO_PATTERN_INDUCE", "1")
    monkeypatch.setenv("MO_LESSON_CONFLICT_CHECK", "0")
    monkeypatch.setenv("MO_THEME_PROMOTE_MIN_RUNS", "3")
    monkeypatch.setenv("MO_THEME_PROMOTE_MIN_MEMBERS", "3")

    calls = []

    def _fake_induce(con, *args, **kwargs):
        calls.append(kwargs.get("member_trace_ids") or [])
        return _AUTHORED, {"n_kept": 1}

    monkeypatch.setattr(pattern_induction, "induce_cluster", _fake_induce)

    themes.ensure_schema(dbp)
    con = sqlite3.connect(dbp)
    con.executescript(
        """
        CREATE TABLE gradient_records (
          gradient_id TEXT PRIMARY KEY, target TEXT NOT NULL,
          signal TEXT NOT NULL, suggested_change TEXT NOT NULL,
          evidence TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.0,
          created_at INTEGER NOT NULL, task_class TEXT);
        CREATE TABLE execution_traces (trace_id TEXT PRIMARY KEY, run_id TEXT);
        """
    )

    def _add(gid, signal, trace, run_id, ts):
        con.execute(
            "INSERT INTO gradient_records (gradient_id, target, signal,"
            " suggested_change, evidence, confidence, created_at, task_class)"
            " VALUES (?, 'workflow.node.implementer', ?, 'do the change',"
            " ?, 0.9, ?, 'code_fix')",
            (gid, signal, trace, ts),
        )
        con.execute(
            "INSERT INTO execution_traces (trace_id, run_id) VALUES (?, ?)",
            (trace, run_id),
        )

    # A: 5 identical task signals across 5 distinct runs — meets the bar.
    for i in range(1, 6):
        _add(f"g-a{i}", _TASK_SIGNAL_A, f"tr-a{i}", f"run-a{i}", 3000 + i)
    # B: 2 identical task signals — below the support bar.
    for i in range(1, 3):
        _add(f"g-b{i}", _TASK_SIGNAL_B, f"tr-b{i}", f"run-b{i}", 2000 + i)
    # C: 5 identical framework signals — never authorable.
    for i in range(1, 6):
        _add(f"g-c{i}", _FRAMEWORK_SIGNAL, f"tr-c{i}", f"run-c{i}", 1000 + i)
    con.commit()
    con.close()

    themes.assign_new(dbp)
    report = themes.promote(db_path=dbp)
    assert report["promoted"] == 1
    assert len(calls) == 1

    con = sqlite3.connect(dbp)
    con.row_factory = sqlite3.Row
    lessons = con.execute("SELECT * FROM lesson_themes").fetchall()
    clusters = con.execute("SELECT theme_id, kind FROM theme_clusters").fetchall()
    grouped = con.execute(
        "SELECT theme_id, COUNT(*) AS n FROM gradient_theme GROUP BY theme_id"
    ).fetchall()
    linked = con.execute("SELECT COUNT(*) FROM gradient_theme").fetchone()[0]
    con.close()

    assert len(lessons) == 1, (
        "only the one qualifying task cluster may enter lesson_themes")
    authored = lessons[0]
    assert authored["status"] == "verified"
    text = (authored["lesson_text"] or "").strip()
    assert text == _AUTHORED
    assert text != authored["theme_id"], "the lesson must not be an identifier"
    assert text != _TASK_SIGNAL_A, "the lesson must be authored, not the raw signal"

    cluster_ids = {r["theme_id"] for r in clusters}
    lesson_ids = {r["theme_id"] for r in lessons}
    assert len(cluster_ids) == 3, "every cluster stays in theme_clusters"
    assert authored["theme_id"] in cluster_ids
    unauthorable = cluster_ids - lesson_ids
    assert unauthorable, "below-bar and framework clusters must stay unauthored"
    assert not (unauthorable & lesson_ids)
    assert {r["kind"] for r in clusters} == {"task", "framework"}

    # Every assigned gradient keeps its gradient_theme row and resolves to a
    # theme held by one of the two tables.
    assert linked == 12
    assert sorted(r["n"] for r in grouped) == [2, 5, 5]
    assert {r["theme_id"] for r in grouped} <= (cluster_ids | lesson_ids)

    # The flow never wrote the ambient DB.
    assert not canary.exists()
