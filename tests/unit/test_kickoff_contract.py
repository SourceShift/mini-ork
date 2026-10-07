"""Acceptance tests for the kickoff contract lint + blocked-run ASK flow (I3).

AC1: ``kickoff_lint.lint`` names every missing contract section and requires an
     AC id in the Acceptance section, always as warns.
AC2: a headless run blocked on profile questions writes one schema-valid ASK
     file per question, exits 6, stays non-terminal (planned), and is
     reaper-paused instead of reaped.
AC3: ``mini-ork resume --answer`` records answers, and once every question is
     answered applies them to the profile and continues the same run.

The AC2 reaper gates (probe paused, close_run_record no-op, reap skip) are each
falsified once: with the unanswered-ASK branch removed the assertions fail.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import jsonschema
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import kickoff_lint
from mini_ork.cli import plan
from mini_ork.cli import resume as resume_mod
from mini_ork.orchestration import run_reaper
from mini_ork.stores import migrate as mig

ASK_SCHEMA = json.loads((REPO / "schemas" / "ask.schema.json").read_text(encoding="utf-8"))


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ── AC1: contract lint ───────────────────────────────────────────────────────

@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def lint_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp ``.mini-ork`` home with one implementer recipe."""
    h = tmp_path / ".mini-ork"
    h.mkdir()
    _write(h / "recipes" / "framework-edit" / "workflow.yaml", """
        version: '0.1.0'
        task_class: framework_edit
        nodes:
          - {name: implementer, type: implementer}
        edges: []
    """)
    _write(h / "recipes" / "framework-edit" / "task_class.yaml", "name: framework_edit\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    return h


_COMPLETE = (
    "# T\n\n"
    "## Goal\n- ship\n\n"
    "## Acceptance\n- AC1: done\n\n"
    "## Files in scope\n- `present.py`\n\n"
    "## Out of scope\n- none\n\n"
    "## Verification command\n- runs\n"
)

_SECTIONS = ("Goal", "Acceptance", "Files in scope", "Out of scope", "Verification command")


def _named_sections(out):
    """The set of section names flagged by a ``no '## X' section`` warn."""
    named: set[str] = set()
    for f in out:
        for name in _SECTIONS:
            if f["msg"] == f"The kickoff has no '## {name}' section.":
                named.add(name)
    return named


def test_ac1_complete_kickoff_has_no_contract_warns(project: Path, lint_home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    out = kickoff_lint.lint(_COMPLETE, project=project, recipe="framework-edit", home=lint_home)
    assert _named_sections(out) == set()
    assert all(f["sev"] == "warn" for f in out)


def test_ac1_drop_each_section_names_exactly_that_section(project: Path, lint_home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    blocks = {
        "Goal": "## Goal\n- ship\n\n",
        "Acceptance": "## Acceptance\n- AC1: done\n\n",
        "Files in scope": "## Files in scope\n- `present.py`\n\n",
        "Out of scope": "## Out of scope\n- none\n\n",
        "Verification command": "## Verification command\n- runs\n",
    }
    for name, block in blocks.items():
        body = _COMPLETE.replace(block, "")
        out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=lint_home)
        assert _named_sections(out) == {name}, name


def test_ac1_synonyms_are_accepted(project: Path, lint_home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    cases = [
        ("Acceptance", "## Acceptance criteria"),
        ("Acceptance", "## Success criteria"),
        ("Acceptance", "## Done when"),
        ("Files in scope", "## Scope"),
        ("Out of scope", "## Non-goals"),
    ]
    for name, heading in cases:
        body = _COMPLETE.replace(f"## {name}\n", f"{heading}\n", 1)
        out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=lint_home)
        assert name not in _named_sections(out), (name, heading)


def test_ac1_acceptance_without_ac_id_warns(project: Path, lint_home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    body = _COMPLETE.replace("## Acceptance\n- AC1: done\n\n", "## Acceptance\n- ship it\n\n")
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=lint_home)
    msgs = [f["msg"] for f in out]
    assert "The Acceptance section has no AC ids (AC1, AC2, …)." in msgs
    assert all(f["sev"] == "warn" for f in out if "AC ids" in f["msg"])


def test_ac1_acceptance_ac_id_variants_accepted(project: Path, lint_home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    for ac in ("AC1", "AC-1", "AC 1", "ac2"):
        body = _COMPLETE.replace("- AC1: done", f"- {ac}: done")
        out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=lint_home)
        assert "The Acceptance section has no AC ids (AC1, AC2, …)." not in [
            f["msg"] for f in out
        ], ac


def test_ac1_researcher_only_recipe_is_exempt(project: Path, lint_home: Path):
    """A recipe without an implementer node skips the contract lint."""
    _write(lint_home / "recipes" / "audit-only" / "workflow.yaml", """
        version: '0.1.0'
        task_class: audit
        nodes:
          - {name: auditor, type: researcher}
        edges: []
    """)
    _write(lint_home / "recipes" / "audit-only" / "task_class.yaml", "name: audit\n")
    out = kickoff_lint.lint("# T\n\n## Goal\n- x\n", project=project, recipe="audit-only",
                            home=lint_home)
    assert _named_sections(out) == set()
    assert "The Acceptance section has no AC ids (AC1, AC2, …)." not in [
        f["msg"] for f in out
    ]


# ── AC2/AC3: blocked-run ASK flow ────────────────────────────────────────────

@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed_row(home: Path, run_id: str, status: str = "planned") -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "framework-edit", status, 0.0, now - 60, now, "framework_edit", "", "latest"))
    con.commit()
    con.close()


def _row(home: Path, run_id: str) -> dict:
    con = sqlite3.connect(home / "state.db")
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute(
            "SELECT status, verdict FROM task_runs WHERE id = ?", (run_id,)).fetchone())
    finally:
        con.close()


def _row_count(home: Path, run_id: str) -> int:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute("SELECT COUNT(*) FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    finally:
        con.close()


def _write_profile(run_dir: Path, *, status="needs_answers", questions=("q1", "q2"),
                   recipe="framework-edit", kickoff="/tmp/kickoff.md") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(
        json.dumps({
            "recipe": recipe,
            "kickoff_path": kickoff,
            "profile_status": status,
            "human_questions": list(questions),
            "confidence": 0.35,
        }, indent=2) + "\n", encoding="utf-8")


def _write_ask(run_dir: Path, ask_id: str, question: str, answer=None) -> None:
    asks_dir = run_dir / "asks"
    asks_dir.mkdir(parents=True, exist_ok=True)
    (asks_dir / f"{ask_id}.json").write_text(json.dumps({
        "schema_version": "ask@1",
        "ask_id": ask_id,
        "run_id": run_dir.name,
        "question": question,
        "blocking_refs": ["run_profile"],
        "options": [],
        "default": None,
        "created_at": int(time.time()),
        "answer": answer,
        "answered_at": None,
    }, indent=2) + "\n", encoding="utf-8")


def _drive_gate_block(home: Path, run_id: str, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run plan.main up to (and including) the profile gate-block."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = run_dir / "kickoff.md"
    kickoff.write_text("# T\n\n## Goal\n- x\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    monkeypatch.setenv("MINI_ORK_PROFILE_PATH", str(run_dir / "run_profile.json"))
    monkeypatch.setenv("MO_AUTO_ANSWER_PROFILE", "0")
    monkeypatch.setenv("MINI_ORK_NONINTERACTIVE", "1")
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "0")
    return plan.main([str(kickoff)], root=str(REPO), dispatch=None)


def test_ac2_gate_block_writes_valid_asks_and_exits_6(home: Path, monkeypatch):
    run_id = "run-blocked"
    _seed_row(home, run_id)
    _write_profile(home / "runs" / run_id, questions=("q1", "q2"))

    rc = _drive_gate_block(home, run_id, monkeypatch)

    assert rc == 6
    asks_dir = home / "runs" / run_id / "asks"
    ask_files = sorted(asks_dir.glob("ask-*.json"))
    assert [p.name for p in ask_files] == ["ask-1.json", "ask-2.json"]
    for p in ask_files:
        data = json.loads(p.read_text(encoding="utf-8"))
        jsonschema.validate(instance=data, schema=ASK_SCHEMA)
        assert data["schema_version"] == "ask@1"
        assert data["run_id"] == run_id
        assert data["answer"] is None and data["answered_at"] is None
    # one run_events row for the block, with the run id embedded in the event_id
    con = sqlite3.connect(home / "state.db")
    events = con.execute(
        "SELECT event_id, event_type FROM run_events WHERE run_id = ?", (run_id,)).fetchall()
    con.close()
    assert [e[1] for e in events] == ["asks_blocked"]
    assert events[0][0].startswith(f"evt-asks_blocked-{run_id}-")


def test_ac2_blocked_run_stays_planned_and_is_not_reaped(home: Path, monkeypatch):
    run_id = "run-planned"
    _seed_row(home, run_id)
    _write_profile(home / "runs" / run_id)

    rc = _drive_gate_block(home, run_id, monkeypatch)
    assert rc == 6

    run_dir = home / "runs" / run_id
    assert _row(home, run_id) == {"status": "planned", "verdict": None}
    assert run_reaper.probe(run_dir).verdict == "paused"
    assert run_reaper.close_run_record(home / "state.db", run_id, run_dir, crashed=False, rc=6) is None
    assert _row(home, run_id) == {"status": "planned", "verdict": None}
    assert run_reaper.reap(home) == []
    assert _row(home, run_id) == {"status": "planned", "verdict": None}


def test_ac3_partial_answer_updates_ask_and_stays_paused(home: Path):
    run_id = "run-partial"
    run_dir = home / "runs" / run_id
    _seed_row(home, run_id)
    _write_profile(run_dir)
    _write_ask(run_dir, "ask-1", "q1")
    _write_ask(run_dir, "ask-2", "q2")

    rc, msg = resume_mod.resume_answers(run_id, {"ask-1": "hello"}, home=str(home))

    assert rc == 0
    assert "ask-2" in msg and "still unanswered" in msg
    one = json.loads((run_dir / "asks" / "ask-1.json").read_text(encoding="utf-8"))
    two = json.loads((run_dir / "asks" / "ask-2.json").read_text(encoding="utf-8"))
    assert one["answer"] == "hello" and isinstance(one["answered_at"], int)
    assert two["answer"] is None and two["answered_at"] is None
    assert run_reaper.probe(run_dir).verdict == "paused"
    assert run_reaper.close_run_record(home / "state.db", run_id, run_dir, crashed=False, rc=6) is None


def test_ac3_full_answer_applies_profile_and_continues_same_run(home: Path, monkeypatch):
    run_id = "run-full"
    run_dir = home / "runs" / run_id
    _seed_row(home, run_id)
    _write_profile(run_dir, questions=("q1", "q2"))
    _write_ask(run_dir, "ask-1", "q1")
    _write_ask(run_dir, "ask-2", "q2")

    calls: list[str] = []

    def fake_reenter(run_id_, *, home=None, root=None):
        calls.append(run_id_)
        return 0

    monkeypatch.setattr(resume_mod, "_reenter_lifecycle", fake_reenter)

    rc, msg = resume_mod.resume_answers(
        run_id, {"ask-1": "a1", "ask-2": "a2"}, home=str(home))

    assert rc == 0
    assert calls == [run_id]  # continuation invoked for the SAME run_id
    for ask_id, ans in (("ask-1", "a1"), ("ask-2", "a2")):
        data = json.loads((run_dir / "asks" / f"{ask_id}.json").read_text(encoding="utf-8"))
        assert data["answer"] == ans and isinstance(data["answered_at"], int)
    profile = json.loads((run_dir / "run_profile.json").read_text(encoding="utf-8"))
    assert profile["profile_status"] == "ready"
    assert profile["answers"] == {"q1": "a1", "q2": "a2"}
    assert profile["human_questions"] == []
    answers_file = json.loads((run_dir / "profile-answers.json").read_text(encoding="utf-8"))
    assert answers_file == {"q1": "a1", "q2": "a2"}
    # the answers path never touches task_runs — no second row is inserted
    assert _row_count(home, run_id) == 1


def test_ac3_resume_without_answer_keeps_cost_pause(home: Path, monkeypatch):
    run_id = "run-cost"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".cost-pause").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    # no --answer → the cost-pause resume() path; the sentinel is removed.
    assert resume_mod.main([run_id]) == 0
    assert not (run_dir / ".cost-pause").exists()


# ── falsification: the reaper paused branch is what holds AC2 together ───────

def test_falsify_removing_the_unanswered_ask_branch(home: Path, monkeypatch):
    run_id = "run-falsify"
    run_dir = home / "runs" / run_id
    _seed_row(home, run_id)
    _write_profile(run_dir)
    _write_ask(run_dir, "ask-1", "q1")

    monkeypatch.setattr(run_reaper, "_has_unanswered_ask", lambda d: False)

    # With the branch removed the run is no longer paused, and close_run_record
    # would fail it instead of leaving it alone — proving the branch is load-bearing.
    assert run_reaper.probe(run_dir).verdict != "paused"
    assert run_reaper.close_run_record(home / "state.db", run_id, run_dir,
                                       crashed=False, rc=6) is not None
    assert _row(home, run_id)["status"] == "failed"
