"""Unit tests for ``mini_ork.learning.promotion_explain``.

The rationales below are copied verbatim from the live home on 2026-10-07
(``$MINI_ORK_HOME/state.db`` → ``promotion_records.rationale``); the expected
labels/reasons/colours are the kickoff's plain-word table.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import learn
from mini_ork.learning import promotion_explain as pe
from mini_ork.stores import migrate as mig
from mini_ork.web.db import db_for

REPO = Path(__file__).resolve().parents[2]

# ── verbatim live rationales ───────────────────────────────────────────────
R_NO_GAIN = (
    "probe: n=2 before=1.00 after=1.00 control_n=3 cost=$1.26; no strict-superset gain over the "
    "control arm (control_n=3): candidate's solved set equals or is a subset of the control's — every "
    "held-out task the candidate solved, the control also solved on at least one of 3 baseline retries, "
    "so no measurement evidence of a real gain (G06-T03) | cost_per_solved_task: before=$0.6319 "
    "after=$0.6319 (n_solved_before=2 n_solved_after=2) | cost_per_solved_task_per_arm: "
    "baseline=$0.1633 candidate=$0.1421 (baseline_cost=$0.3265 candidate_cost=$0.2842 "
    "n_solved_baseline=2 n_solved_candidate=2 control_n=3)"
)
R_NO_REGRESSION = (
    "probe: n=2 before=1.00 after=0.50 control_n=3 cost=$10.36; per-task no-regression gate: 1 "
    "previously-solved held-out task(s) now fail (tolerance=0); aggregate delta was -0.5000 but the "
    "candidate regresses solved work [probe-2.md] (2607.14004: aggregate-up-but-task-regressed is the "
    "collapse signature) | cost_per_solved_task: before=$5.1799 after=$10.3597 (n_solved_before=2 "
    "n_solved_after=1) | cost_per_solved_task_per_arm: baseline=$1.3511 candidate=$2.2530 "
    "(baseline_cost=$2.7022 candidate_cost=$2.2530 n_solved_baseline=2 n_solved_candidate=1 control_n=3)"
)
R_MOCK = (
    "refusing promote: scorer=mock fabricates utility (no real held-out measurement); non-regression "
    "cleared: utility_after=0.5498 >= utility_before=0.0000 (delta=+0.5498 >= threshold=+0.0000)"
)
R_MEASURED_NOTHING = (
    "probe scorer measured nothing (no frozen probe set under recipes/<recipe>/probes/, unresolvable "
    "target file, or budget exhausted) — refusing to promote without held-out evaluation"
)
R_UNVETTED = (
    "UNVETTED promote (scorer=mock fabricates utility; operator-enabled via MO_APPLY_UNVETTED); "
    "non-regression cleared: utility_after=0.5350 >= utility_before=0.0000 "
    "(delta=+0.5350 >= threshold=+0.0000)"
)


# ── explain ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rationale, decision, task_class, source_id, label, reason, colour", [
    (R_NO_GAIN, "quarantined", "code_fix", "gr-104ae59b748b",
     "Not better",
     "Solved nothing the old prompt didn't also solve when simply retried (control: 3 runs)",
     "yellow"),
    (R_NO_REGRESSION, "quarantined", "code_fix", "gr-smoke-harness5-1790702521",
     "Broke a task",
     "A held-out task the old prompt solved now fails (probe-2.md)",
     "red"),
    (R_MOCK, "quarantined", "code_fix", "gr-smoke-harness-1790701221",
     "Score was simulated",
     "Refused: the mock scorer makes up numbers; nothing was measured",
     "muted"),
    (R_MEASURED_NOTHING, "quarantined", "framework_edit", "gr-1c9d2fad50a1",
     "Never evaluated",
     "No probe set for framework_edit (recipes/framework-edit/probes/ missing)",
     "muted"),
    (R_UNVETTED, "promoted", "code_fix", "gr-smoke-harness-1790701221",
     "Applied without evaluation",
     "Promoted by operator override (MO_APPLY_UNVETTED) on a simulated score",
     "orange"),
])
def test_explain_live_rationales(tmp_path: Path, rationale, decision, task_class, source_id,
                                 label, reason, colour) -> None:
    got = pe.explain(decision, rationale, task_class=task_class, source_id=source_id,
                     repo_root=tmp_path)
    assert got["label"] == label
    assert got["reason"] == reason
    assert got["colour"] == colour


def test_explain_flags_test_runs(tmp_path: Path) -> None:
    smoke = pe.explain("quarantined", R_MEASURED_NOTHING, task_class="code_fix",
                       source_id="gr-smoke-harness-1790701221", repo_root=tmp_path)
    real = pe.explain("quarantined", R_MEASURED_NOTHING, task_class="code_fix",
                      source_id="gr-104ae59b748b", repo_root=tmp_path)
    assert smoke["test_run"] is True
    assert real["test_run"] is False


def test_explain_measured_nothing_reports_probes_did_not_run_when_dir_exists(tmp_path: Path) -> None:
    (tmp_path / "recipes" / "framework-edit" / "probes").mkdir(parents=True)
    got = pe.explain("quarantined", R_MEASURED_NOTHING, task_class="framework_edit",
                     source_id="gr-1", repo_root=tmp_path)
    assert got["label"] == "Never evaluated"
    assert got["reason"] == "Probes did not run (launch error or budget)"


def test_explain_promoted_other_is_applied(tmp_path: Path) -> None:
    got = pe.explain("promoted", "Utility improved by 0.8500 (0.0000 → 0.8500); all benchmark tasks passed.",
                     task_class="code_fix", source_id="", repo_root=tmp_path)
    assert got["label"] == "Applied"
    assert got["reason"] == "Solved more held-out tasks: 0.00 → 0.85"
    assert got["colour"] == "green"


# ── live_in_prompt ─────────────────────────────────────────────────────────

def test_live_in_prompt_finds_marker_and_returns_none(tmp_path: Path) -> None:
    d = tmp_path / "recipes" / "framework-edit" / "prompts"
    d.mkdir(parents=True)
    (d / "implementer.md").write_text(
        "# Implementer\n<!-- applied:gradient_records:gr-1c9d2fad50a1 -->\n", encoding="utf-8")
    assert pe.live_in_prompt("gr-1c9d2fad50a1", tmp_path) == \
        "recipes/framework-edit/prompts/implementer.md"
    assert pe.live_in_prompt("gr-nope", tmp_path) is None
    assert pe.live_in_prompt(None, tmp_path) is None


# ── decisions ──────────────────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path) -> int:
    """Two promotions (one real, one a gr-smoke test run), fully joined."""
    now = int(time.time())
    # The malformed tail the live home really stores: seconds replaced by "f".
    iso = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(now)) + ":fZ"
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO promotion_records (promotion_id, candidate_id, from_version_id, to_version_id, "
        "utility_before, utility_after, decision, decided_at, decided_by, rationale) VALUES "
        "('pr-a', 'cand-a', 'v0', 'v1', 1.0, 1.0, 'quarantined', ?, 'gate', ?)", (iso, R_NO_GAIN))
    con.execute(
        "INSERT INTO promotion_records (promotion_id, candidate_id, from_version_id, to_version_id, "
        "utility_before, utility_after, decision, decided_at, decided_by, rationale) VALUES "
        "('pr-b', 'cand-b', 'v0', 'v1', 0.0, 0.0, 'quarantined', ?, 'gate', ?)", (iso, R_MEASURED_NOTHING))
    con.execute(
        "INSERT INTO apply_attempts (attempt_id, task_class, target_kind, target_name, source_kind, "
        "source_id, candidate_id, promotion_id, decision) VALUES "
        "('at-a', 'code_fix', 'prompt_file', 'agent.reviewer.prompt', 'gradient_records', "
        "'gr-104ae59b748b', 'cand-a', 'pr-a', 'quarantined')")
    con.execute(
        "INSERT INTO apply_attempts (attempt_id, task_class, target_kind, target_name, source_kind, "
        "source_id, candidate_id, promotion_id, decision) VALUES "
        "('at-b', 'code_fix', 'prompt_file', 'harness.code-fix.implementer', 'gradient_records', "
        "'gr-smoke-1', 'cand-b', 'pr-b', 'quarantined')")
    con.execute(
        "INSERT INTO workflow_candidates (candidate_id, base_workflow_version_id, mutations, status) "
        "VALUES ('cand-a', 'v0', ?, 'candidate')",
        (json.dumps([{"kind": "prompt_change", "node_name": "agent.reviewer.prompt",
                      "field": "system_prompt", "old_val": None, "new_val": "PROPOSAL TEXT"}]),))
    con.execute(
        "INSERT INTO workflow_candidates (candidate_id, base_workflow_version_id, mutations, status) "
        "VALUES ('cand-b', 'v0', ?, 'candidate')",
        (json.dumps([{"kind": "prompt_change", "node_name": "harness.code-fix.implementer",
                      "field": "system_prompt", "old_val": None, "new_val": "SMOKE PROPOSAL"}]),))
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, "
        "confidence, created_at, task_class) VALUES "
        "('gr-104ae59b748b', 'agent.reviewer.prompt', 'SIGNAL TEXT', 'SUGGESTED TEXT', '{}', 0.9, ?, "
        "'code_fix')", (now,))
    con.commit()
    con.close()
    return now


def test_decisions_joins_explains_and_flags_test_run(home: Path) -> None:
    _seed(home)
    rows = pe.decisions(db_for(home), home.parent)
    by_cand = {r["candidate"]: r for r in rows}
    assert set(by_cand) == {"cand-a", "cand-b"}

    a = by_cand["cand-a"]
    assert a["task_class"] == "code_fix"
    assert a["target"] == "agent.reviewer.prompt"
    assert a["proposal"] == "PROPOSAL TEXT"
    assert a["signal"] == "SIGNAL TEXT"
    assert a["suggested_change"] == "SUGGESTED TEXT"
    assert a["label"] == "Not better"
    assert a["test_run"] is False
    assert a["live_path"] is None
    # The malformed "…:fZ" tail still parses to a sane, non-zero epoch (FM-1).
    assert a["decided_at"] > 0

    # gr-smoke source → test_run.
    assert by_cand["cand-b"]["test_run"] is True


def test_decisions_tolerates_absent_promotion_table(tmp_path: Path) -> None:
    from mini_ork.web.db import StateDB

    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    assert pe.decisions(StateDB(db_path), tmp_path) == []


# ── pages ──────────────────────────────────────────────────────────────────

def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def _live_prompt_tree(home: Path) -> None:
    d = home.parent / "recipes" / "code-fix" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "reviewer.md").write_text(
        "<!-- applied:gradient_records:gr-104ae59b748b -->\n", encoding="utf-8")


def test_overview_shows_plain_label_and_in_use(home: Path) -> None:
    _seed(home)
    _live_prompt_tree(home)
    page = learn.build(home, "overview", {})
    events = _section(page, "Recent learning events")
    titles = [i["t"] for i in events["items"]]
    assert any(t.startswith("Not better: agent.reviewer.prompt") for t in titles), titles
    subs = " | ".join(i["sub"] for i in events["items"])
    assert "in use in recipes/code-fix/prompts/reviewer.md" in subs
    assert "test run" in subs  # the gr-smoke row is flagged


def test_improve_hides_test_runs_by_default_and_shows_detail(home: Path) -> None:
    _seed(home)
    _live_prompt_tree(home)

    default = learn.build(home, "improve", {})
    table = _section(default, "Changes proposed to your prompts")
    changes = [c["cells"][1]["t"] for c in table["rows"]]
    assert any("agent.reviewer.prompt (code_fix)" in c for c in changes)
    assert not any("harness.code-fix.implementer" in c for c in changes)  # test run hidden

    with_tests = learn.build(home, "improve", {"tests": "1"})
    table_all = _section(with_tests, "Changes proposed to your prompts")
    assert len(table_all["rows"]) == 2

    detail = learn.build(home, "improve", {"decision": "cand-a"})
    md = [s for s in detail["sections"] if s["type"] == "markdown"]
    assert md, [s["title"] for s in detail["sections"]]
    assert md[0]["title"] == "Not better · agent.reviewer.prompt"
    assert "PROPOSAL TEXT" in md[0]["text"]
    assert R_NO_GAIN in md[0]["text"]
