"""IDE page ``learn`` → Lessons: every row opens and reads in full.

Seeds the tables the detail panels read (gradients + themes, patterns, failure
modes, the injection ledger, the task_runs ⇄ execution_traces join) and asserts
the full-text detail, the paging, and the row ``do``/``sel`` contract.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import learn
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]

# Longer than any list-cell truncation, so "appears unabridged" is meaningful.
_LONG_SIGNAL = ("The verifier returned only the node_type field for the planner, so the retry "
                "logic cannot tell a deliberate fallback from a crashed node. ") * 8
_LONG_CHANGE = ("Record verifier_output for the planner node as a structured verdict object so "
                "the retry logic can distinguish an intentional fallback. ") * 6
_LONG_LESSON = ("When a pattern cluster has no authored lesson, treat it as a frequency count and "
                "never inject it as guidance. ") * 6
_MULTILINE_ERR = "node planner failed\nrc=124\nno verdict.json written"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Gradient Demo Run\n\nbody\n", encoding="utf-8")
    con = sqlite3.connect(h / "state.db")
    now = int(time.time())
    # A resolvable run: gradient.evidence → execution_traces.run_id → task_runs.id.
    con.execute("INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, created_at, "
                "updated_at) VALUES ('grad-run-1', 'code_fix', 'code-fix', ?, 'published', ?, ?)",
                (str(kickoff), now, now))
    con.execute("INSERT INTO execution_traces (trace_id, run_id, task_class, status, created_at) "
                "VALUES ('tr-1', 'grad-run-1', 'code_fix', 'failure', ?)", (now,))
    con.execute("INSERT INTO task_runs (id, task_class, kickoff_path, status, created_at, updated_at) "
                "VALUES ('fail-run-1', 'code_fix', ?, 'published', ?, ?)", (str(kickoff), now, now))
    con.execute("INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, "
                "confidence, created_at, task_class) VALUES ('g1', 'verifier.planner', ?, ?, 'tr-1', "
                "0.91, ?, 'code_fix')", (_LONG_SIGNAL, _LONG_CHANGE, now))
    for gid, ts in (("g2", now - 10), ("g3", now - 20)):
        con.execute("INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, "
                    "evidence, confidence, created_at, task_class) VALUES (?, 'verifier.planner', "
                    "'sibling note in the same theme', 'fix it', 'tr-1', 0.5, ?, 'code_fix')",
                    (gid, ts))
    con.execute("INSERT INTO lesson_themes (theme_id, kind, representative, centroid, n_gradients, "
                "n_runs, status) VALUES ('th-1', 'task', 'planner fallback detection', x'00', 3, 2, "
                "'active')")
    for gid in ("g1", "g2", "g3"):
        con.execute("INSERT INTO gradient_theme (gradient_id, theme_id, similarity) VALUES (?, 'th-1', 0.9)",
                    (gid,))
    # The injection ledger — 3 real gradient injections, 2 for the pattern.
    for i in range(3):
        con.execute("INSERT INTO lesson_injections (run_id, node_id, source_kind, source_id, ts) "
                    "VALUES (?, ?, 'gradient', 'g1', ?)", (f"run-{i}", "planner", now - i))
    for i in range(2):
        con.execute("INSERT INTO lesson_injections (run_id, node_id, source_kind, source_id, ts) "
                    "VALUES (?, ?, 'pattern', 'p1', ?)", (f"run-{i}", "planner", now - i))
    # emergent_patterns gains lesson_text via a guarded ALTER (migration 0056).
    cols = {r[1] for r in con.execute("PRAGMA table_info(emergent_patterns)")}
    if "lesson_text" not in cols:
        con.execute("ALTER TABLE emergent_patterns ADD COLUMN lesson_text TEXT")
    members = json.dumps([{"item_table": "execution_traces", "item_id": "tr-1"}])
    con.execute("INSERT INTO emergent_patterns (pattern_id, cluster_label, member_item_ids_json, "
                "feature_set_json, strength_score, status, detected_at, lesson_text) VALUES "
                "('p1', 'cluster: planner fallback', ?, '[]', 0.9, 'approved', ?, ?)",
                (members, now, _LONG_LESSON))
    con.execute("INSERT INTO emergent_patterns (pattern_id, cluster_label, member_item_ids_json, "
                "feature_set_json, strength_score, status, detected_at) VALUES "
                "('p2', 'cluster: timeout retries', '[]', '[]', 0.4, 'proposed', ?)", (now,))
    # Two failures in one (category, stage); one resolves to a run, one does not.
    # ``failure_memory.run_id`` is an INTEGER-affinity column that holds the text
    # ``task_runs.id`` in this fixture (SQLite stores it as text) — the realistic
    # resolvable shape; a numeric id (live data writes 0) does not resolve.
    con.execute("INSERT INTO failure_memory (failure_id, run_id, workflow_stage, failure_category, "
                "error_message, occurred_at) VALUES ('f1', 'fail-run-1', 'execute', 'dispatch_error', "
                "?, '2026-10-05T10:00:00Z')", (_MULTILINE_ERR,))
    con.execute("INSERT INTO failure_memory (failure_id, run_id, workflow_stage, failure_category, "
                "error_message, occurred_at) VALUES ('f2', '0', 'execute', 'dispatch_error', "
                "'single line error', '2026-10-04T10:00:00Z')")
    con.commit()
    con.close()
    return h


def _first(page: dict, kind: str) -> dict:
    return next(s for s in page["sections"] if s["type"] == kind)


def _by_title(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def _kv(page: dict) -> dict:
    return _first(page, "kv")


def _items(kv: dict) -> dict[str, dict]:
    return {i["k"]: i for i in kv["items"]}


def _labels(section: dict) -> list[str]:
    return [a["label"] for a in section["actions"]]


def test_default_build_has_three_openable_sections(home: Path) -> None:
    page = learn.build(home, "lessons", {})
    assert [s["title"] for s in page["sections"]] == ["Gradients", "Patterns", "Failure modes"]
    grads = _by_title(page, "Gradients")
    assert grads["head"] == ["target", "signal", "task class", "conf", "injected"]
    # The newest gradient (g1) carries real injection counts, not a hardcoded dash.
    row = next(r for r in grads["rows"] if r["cells"][0]["t"] == "verifier.planner")
    assert row["do"] == {"set": {"item": "g:g1"}}
    assert row["cells"][4]["t"] == "3"


def test_gradient_detail_reads_in_full(home: Path) -> None:
    page = learn.build(home, "lessons", {"item": "g:g1"})
    md = page["sections"][0]
    assert md["type"] == "markdown" and md["title"] == "Learning · verifier.planner"
    # The whole signal and suggested change, unabridged.
    assert _LONG_SIGNAL in md["text"] and _LONG_CHANGE in md["text"]
    kv = _kv(page)
    facts = _items(kv)
    assert facts["Confidence"]["v"] == "0.91"
    assert facts["Task class"]["v"] == "code_fix"
    # Evidence names the run fetched through the trace, titled from the kickoff heading.
    assert facts["Evidence"]["v"] == "Gradient Demo Run"
    assert "grad-run-1" in facts["Evidence"]["sub"]
    assert facts["Theme"]["v"] == "planner fallback detection"
    assert "3 similar notes across 2 runs" in facts["Theme"]["sub"]
    assert "about the task" in facts["Theme"]["sub"]
    assert facts["Given to agents"]["v"].startswith("3×")
    assert set(_labels(kv)) == {"Open run", "Close"}
    assert next(a for a in kv["actions"] if a["label"] == "Open run")["do"]["run"] == "grad-run-1"
    # Similar notes lists the other theme members and each row opens that gradient.
    similar = next(s for s in page["sections"] if s["title"] == "Similar notes in this theme")
    assert {r["do"]["set"]["item"] for r in similar["rows"]} == {"g:g2", "g:g3"}


def test_selected_gradient_row_is_marked(home: Path) -> None:
    page = learn.build(home, "lessons", {"item": "g:g1"})
    grads = _by_title(page, "Gradients")
    selected = [r for r in grads["rows"] if r["sel"]]
    assert len(selected) == 1 and selected[0]["cells"][0]["t"] == "verifier.planner"


def test_pattern_detail_shows_authored_lesson(home: Path) -> None:
    page = learn.build(home, "lessons", {"item": "p:p1"})
    md = page["sections"][0]
    assert md["title"] == "Pattern · p1" and _LONG_LESSON in md["text"]
    facts = _items(_kv(page))
    assert facts["Status"]["v"] == "approved"
    assert facts["Evidence runs"]["v"] == "1"          # one distinct run behind tr-1
    assert facts["Given to agents"]["v"].startswith("2×")


def test_pattern_without_lesson_is_honest(home: Path) -> None:
    page = learn.build(home, "lessons", {"item": "p:p2"})
    assert "No lesson authored yet — this pattern is only a frequency count." in page["sections"][0]["text"]
    # The list marks it as missing an authored lesson.
    page2 = learn.build(home, "lessons", {})
    pats = next(s for s in page2["sections"] if s["title"] == "Patterns")
    texts = [r["cells"][0]["t"] for r in pats["rows"]]
    assert any(t.endswith("(no lesson)") for t in texts)


def test_failure_mode_detail_shows_full_errors(home: Path) -> None:
    page = learn.build(home, "lessons", {"item": "f:dispatch_error|execute"})
    md = page["sections"][0]
    assert md["title"] == "Failure mode · dispatch_error · execute"
    assert "rc=124" in md["text"] and "no verdict.json written" in md["text"]
    assert "```" in md["text"]                          # multi-line errors are fenced
    kv = _kv(page)
    assert "Open run" in _labels(kv) and "Close" in _labels(kv)
    facts = _items(kv)
    assert facts["Occurrences"]["v"] == "2" and facts["Runs"]["v"] == "2"
    assert facts["Last seen"]["v"] == "2026-10-05"


def test_unknown_item_reports_missing(home: Path) -> None:
    page = learn.build(home, "lessons", {"item": "g:does-not-exist"})
    assert page["sections"][0]["type"] == "list"
    assert "That learning no longer exists" in json.dumps(page)


def test_gradient_paging(home: Path) -> None:
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    for i in range(30):
        con.execute("INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, "
                    "evidence, confidence, created_at, task_class) VALUES (?, 't', 's', 'c', 'tr-1', "
                    "0.5, ?, 'code_fix')", (f"page-{i:02d}", now + i))
    con.commit()
    con.close()
    first = next(s for s in learn.build(home, "lessons", {})["sections"] if s["title"] == "Gradients")
    assert len(first["rows"]) == 25
    assert "Older" in _labels(first) and "Newer" not in _labels(first)
    assert "showing 1–25 of 33" in first["note"]
    second = next(s for s in learn.build(home, "lessons", {"goff": "25"})["sections"]
                  if s["title"] == "Gradients")
    assert len(second["rows"]) == 8                      # 33 rows total, 25 per page
    assert "Newer" in _labels(second) and "Older" not in _labels(second)
    assert "showing 26–33 of 33" in second["note"]
