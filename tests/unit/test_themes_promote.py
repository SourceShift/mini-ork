"""Tests for ``themes.promote`` — the lesson_themes producer.

Every ``lesson_themes`` row is a cluster statistic until an authored lesson is
written; the v1/v2 lesson blocks read only ``status='verified'`` rows with a
non-blank ``lesson_text``, so a candidate-only table renders nothing. These
lock the producer: gating, the support bar, member resolution, the
``candidate → verified`` transition, idempotency, and the consumer read that
the promotion unblocks.

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
        "INSERT INTO lesson_themes(theme_id, kind, task_class, representative,"
        " centroid, n_gradients, n_runs, status)"
        " VALUES (?, 'task', ?, ?, ?, ?, ?, 'candidate')",
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
    assert _row(db, "t1") == ("candidate", None)


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
    assert _row(db, "t1") == ("candidate", None)


def test_too_few_members_skipped(db, stub_inductor):
    _seed_theme(db, "t1", n_runs=5)
    _seed_members(db, "t1", 1)        # < min_members=3

    report = themes.promote(db_path=db, min_runs=3)

    assert report["promoted"] == 0
    assert report["skipped"][0]["reason"] == "too few member traces"
    assert _row(db, "t1") == ("candidate", None)


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
