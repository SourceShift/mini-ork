"""Verified-only injection contract (user rule 2026-10-07).

Only verified learnings reach prompts: approved+lessoned patterns (unchanged),
verified ``lesson_themes`` rows (new section), and operator preferences. Raw
``gradient_records`` — an extractor's self-rated confidence, not a
verification — are injected ONLY under ``MO_INJECT_UNVERIFIED=1``.

No network. A fresh temp DB (full migrations) is built per test.
"""
from __future__ import annotations

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
from mini_ork import context_assembler as ca  # noqa: E402


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(dbp)
    now = int(time.time())
    con.executemany(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change,"
        " evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
        [("g1", "auth.middleware", "tests skipped silently", "run pytest -x",
          "e", 0.9, now, "code-fix")])
    con.commit()
    con.close()
    return dbp


def _seed_theme(db, theme_id, *, kind="task", task_class="code-fix",
                lesson="a grounded lesson", status="verified", n_runs=3):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO lesson_themes (theme_id, kind, task_class, representative,"
        " centroid, n_gradients, n_runs, first_seen, last_seen, lesson_text,"
        " status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (theme_id, kind, task_class, "rep", b"\x00", 1, n_runs,
         int(time.time()), int(time.time()), lesson, status))
    con.commit()
    con.close()


def _seed_approved_pattern(db, pattern_id, lesson):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
        "member_item_ids_json, feature_set_json, strength_score, "
        "suggested_meta_adr, status, lesson_text, detected_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (pattern_id, "taught", "[]", json.dumps(["adr"]), 7.0,
         "meta-adr", "approved", lesson, int(time.time())))
    con.commit()
    con.close()


def _env(monkeypatch, db, **extra):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")
    monkeypatch.setenv("MO_EMERGENT_INJECT", "1")
    monkeypatch.delenv("MO_INJECT_UNVERIFIED", raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_gradients_only_are_not_injected_by_default(db, monkeypatch):
    """The fixture seeds one gradient and nothing verified → empty block."""
    _env(monkeypatch, db)
    src: list[dict] = []
    md = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    assert md == ""
    assert src == []


def test_unverified_opt_in_restores_the_gradient_section(db, monkeypatch):
    _env(monkeypatch, db, MO_INJECT_UNVERIFIED="1")
    src: list[dict] = []
    md = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    assert "auth.middleware" in md
    assert "Learned failure modes" in md
    assert [s["kind"] for s in src] == ["gradient"]


def test_verified_task_theme_is_injected(db, monkeypatch):
    _env(monkeypatch, db)
    _seed_theme(db, "th-1", n_runs=4,
                lesson="write the failing test before the fix")
    src: list[dict] = []
    md = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    assert "--- Verified lessons from prior runs (code-fix) ---" in md
    assert "- write the failing test before the fix  (seen in 4 runs)" in md
    assert "--- /verified lessons ---" in md
    assert "auth.middleware" not in md, "raw gradient still excluded"
    assert {"kind": "theme", "id": "th-1",
            "text": "write the failing test before the fix"} in src


def test_wildcard_class_theme_is_injected(db, monkeypatch):
    _env(monkeypatch, db)
    _seed_theme(db, "th-any", task_class="*", n_runs=1, lesson="always back up")
    md = ca.failure_modes_md("code-fix", 5, db=db)
    assert "always back up  (seen in 1 runs)" in md


@pytest.mark.parametrize("kwargs", [
    {"status": "candidate"},
    {"kind": "framework"},
    {"task_class": "other-class"},
    {"lesson": None},
    {"lesson": "   "},
])
def test_unverified_shapes_are_not_injected(db, monkeypatch, kwargs):
    _env(monkeypatch, db)
    _seed_theme(db, "th-x", **kwargs)
    src: list[dict] = []
    md = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    assert md == ""
    assert src == []


def test_approved_lessoned_pattern_still_injected(db, monkeypatch):
    _env(monkeypatch, db)
    _seed_approved_pattern(db, "emg-yes", "authored lesson from induction")
    src: list[dict] = []
    md = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    assert "authored lesson from induction" in md
    assert [s["id"] for s in src if s["kind"] == "pattern"] == ["emg-yes"]
    assert not any(s["kind"] == "gradient" for s in src)


def test_repository_mirrors_the_rule(db, monkeypatch):
    """``fetch_failure_mode_gradients`` returns verified themes by default and
    raw gradients only under the opt-in — same rule as ``failure_modes_md``."""
    from mini_ork.web.db import StateDB
    from mini_ork.web.repositories import LearningRepository

    _seed_theme(db, "th-1", n_runs=2, lesson="mirror me")
    repo = LearningRepository(StateDB(db))

    monkeypatch.delenv("MO_INJECT_UNVERIFIED", raising=False)
    rows = repo.fetch_failure_mode_gradients("code-fix")
    assert [r["gradient_id"] for r in rows] == ["th-1"]
    assert rows[0]["signal"] == "mirror me"
    assert rows[0]["target"] == "theme:th-1"

    monkeypatch.setenv("MO_INJECT_UNVERIFIED", "1")
    ids = {r["gradient_id"] for r in repo.fetch_failure_mode_gradients("code-fix")}
    assert ids == {"g1"}
