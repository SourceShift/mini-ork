"""RB-18: only a real verdict starts a revise round.

The revise loop used to fire on three things that are not verdicts about the
code: a node that produced no verdict (dispatch error/timeout), a base that
already satisfies the test gate, and an implementer that filed a spec-test
defect. These tests drive the real executor entry point
(``mini_ork.cli.execute.main``) over a minimal four-node workflow, stubbing
only the LLM seam (``dispatch_fn``) and the recipe verifier script.

Hermetic: temp engine/home/run/target trees, a scratch state.db, a temp git repo
as the target. No network, no provider credentials, no live-DB writes, no
dependence on the outer run's identity (all MINI_ORK_*/MO_* variables are
scrubbed before each drive, per audit RB-04).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import context as mo_context  # noqa: E402
from mini_ork.cli import execute as ex  # noqa: E402

NEEDS_REVISION = json.dumps(
    {"verdict": "needs_revision", "notes": ["tighten the failure handling"]})
NO_DELTA_REASON = ("opaque command: no candidate-pass/base-fail delta "
                   "(candidate rc=0, base rc=0)")

WORKFLOW_YAML = """\
version: "0.1.0"
task_class: generic
description: >
  Minimal revise-loop workflow: an implementer, a test verifier with a retries
  edge back to the implementer, a reviewer with a retries edge back to the
  implementer, and a publisher that only runs when the reviewer passes.

nodes:
  - name: implementer
    type: implementer
    model_lane: stub_impl
    dispatch_mode: serial

  - name: test_gate
    type: verifier
    model_lane: stub_verify
    verifier_ref: verifiers/test.py
    dispatch_mode: serial

  - name: code_reviewer
    type: reviewer
    model_lane: stub_review
    dispatch_mode: serial

  - name: publisher
    type: publisher
    model_lane: stub_publish
    dispatch_mode: serial

edges:
  - { from: implementer,   to: test_gate,     edge_type: verifies }
  - { from: implementer,   to: code_reviewer, edge_type: depends_on }
  - { from: test_gate,     to: code_reviewer, edge_type: depends_on }
  - { from: code_reviewer, to: publisher,     edge_type: depends_on }
  - { from: code_reviewer, to: implementer,   edge_type: retries, max_rounds: 2 }
  - { from: test_gate,     to: implementer,   edge_type: retries, max_rounds: 2 }
"""

# Behaviour is driven per-test through <run_dir>/spec-verifier-control.json.
# It prints the same JSON envelope shape the real test gate records (flat rcs +
# the nested replay dict + the RUN-BUGS reason string) and mirrors it to
# <run_dir>/test-verdict.json.
_VERIFIER_STUB = '''#!/usr/bin/env python3
import json
import os

run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
try:
    with open(os.path.join(run_dir, "spec-verifier-control.json")) as fh:
        control = json.load(fh)
except Exception:
    control = {}
payload = {
    "pass": bool(control.get("pass", True)),
    "reason": control.get(
        "reason",
        "opaque command: no candidate-pass/base-fail delta "
        "(candidate rc=0, base rc=0)"),
    "candidate_rc": int(control.get("candidate_rc", 0)),
    "base_rc": int(control.get("base_rc", 0)),
}
payload["replay"] = {
    "runner": "opaque",
    "candidate_rc": payload["candidate_rc"],
    "base_rc": payload["base_rc"],
}
line = json.dumps(payload)
print(line)
try:
    with open(os.path.join(run_dir, "test-verdict.json"), "w") as fh:
        fh.write(line + "\\n")
except OSError:
    pass
'''


class StubDispatch:
    """The executor's LLM seam: ``dispatch_fn(task_class, lane, prompt)``."""

    def __init__(self, reviewer_rc=0, reviewer_text=NEEDS_REVISION,
                 impl_effect=None):
        self.reviewer_rc = reviewer_rc
        self.reviewer_text = reviewer_text
        self.impl_effect = impl_effect
        self.prompts: list[str] = []

    def __call__(self, task_class, lane, prompt):
        self.prompts.append(prompt)
        if "Review the implementation for:" in prompt:
            return self.reviewer_rc, self.reviewer_text
        if "Write your execution summary to:" in prompt:
            if self.impl_effect is not None:
                self.impl_effect(self.implementer_calls)
            return 0, "stub implementer round complete, plain summary only"
        return 0, "stub dispatch"

    @property
    def implementer_calls(self) -> int:
        return sum(1 for p in self.prompts
                   if "Write your execution summary to:" in p)

    @property
    def reviewer_calls(self) -> int:
        return sum(1 for p in self.prompts
                   if "Review the implementation for:" in p)


class _Harness:
    """A whole hermetic mini-ork run: engine root, home, run dir, target repo."""

    def __init__(self, tmp_path):
        self.run_id = f"revise-{uuid.uuid4().hex[:10]}"
        self.root = tmp_path / "engine"
        self.home = tmp_path / "home"
        self.run_dir = tmp_path / "run"
        self.target = tmp_path / "target"
        for directory in (self.root, self.home, self.run_dir):
            directory.mkdir(parents=True)

        # The reviewer handler shells out to lib/extract_verdict.py.
        lib_dir = self.root / "lib"
        lib_dir.mkdir()
        shutil.copy(REPO / "lib" / "extract_verdict.py",
                    lib_dir / "extract_verdict.py")
        verifiers_dir = self.root / "recipes" / "spectest" / "verifiers"
        verifiers_dir.mkdir(parents=True)
        (verifiers_dir / "test.py").write_text(_VERIFIER_STUB)

        self.target.mkdir()
        (self.target / "README.md").write_text("spec target base\n")
        self._git("init", "-q")
        self._git("add", "README.md")
        self._git("commit", "-q", "-m", "base")

        self.workflow_path = self.run_dir / "workflow.yaml"
        self.workflow_path.write_text(WORKFLOW_YAML)
        self.plan_path = self.run_dir / "plan.json"
        self.plan_path.write_text(json.dumps({
            "objective": "RB-18 test harness",
            "task_class": "generic",
            "decomposition": [],
            "artifact_contract": {"outputs": []},
        }))
        self.set_verifier_control(pass_=True)

        self.db_path = self.home / "state.db"
        con = sqlite3.connect(self.db_path)
        con.execute(
            "CREATE TABLE IF NOT EXISTS task_runs ("
            " id TEXT PRIMARY KEY,"
            " status TEXT NOT NULL DEFAULT 'planned',"
            " notes TEXT,"
            " plan_path TEXT,"
            " created_at INTEGER NOT NULL,"
            " updated_at INTEGER,"
            " ended_at INTEGER,"
            " duration_ms INTEGER NOT NULL DEFAULT 0)")
        con.execute(
            "INSERT INTO task_runs (id, status, created_at, duration_ms) "
            "VALUES (?, 'planned', ?, 0)",
            (self.run_id, int(time.time())))
        con.commit()
        con.close()

        self.env = {
            "MINI_ORK_ROOT": str(self.root),
            "MINI_ORK_HOME": str(self.home),
            "MINI_ORK_DB": str(self.db_path),
            "MINI_ORK_RUN_ID": self.run_id,
            "MINI_ORK_RUN_DIR": str(self.run_dir),
            "MINI_ORK_RECIPE": "spectest",
            "MINI_ORK_WORKFLOW": str(self.workflow_path),
            "MO_TARGET_CWD": str(self.target),
            "MO_REVISE_ROUNDS": "2",
            # Deterministic, provider-free drive: no orchestrator layer, no git
            # checkpoints, no learning writeback, no auto-repair.
            "MO_RUN_ORCHESTRATOR": "0",
            "MO_NODE_GIT_CKPT": "0",
            "MO_LEVEL_VECTOR": "0",
            "MO_INJECT_LEARNINGS": "0",
            "MO_GRADE_RUN_REWARD": "0",
            "MO_LEARNING_WRITEBACK": "0",
            "MO_AUTO_REPAIR": "0",
        }

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.target),
             "-c", "user.email=spec@example.invalid",
             "-c", "user.name=spec harness", *args],
            check=True, capture_output=True)

    def set_verifier_control(self, *, pass_, candidate_rc=0, base_rc=0,
                             reason=None):
        control = {"pass": bool(pass_), "candidate_rc": candidate_rc,
                   "base_rc": base_rc}
        if reason is not None:
            control["reason"] = reason
        (self.run_dir / "spec-verifier-control.json").write_text(
            json.dumps(control))

    def run_row(self):
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute(
                "SELECT status, COALESCE(notes, '') FROM task_runs "
                "WHERE id = ?", (self.run_id,)).fetchone()
        finally:
            con.close()

    def execute(self, dispatch):
        """Drive ``mini_ork.cli.execute.main`` with the stubbed seams.

        Scrubs every inherited MINI_ORK_*/MO_* variable first so a nested run
        cannot adopt the outer run's identity (audit RB-04), and resets the
        executor's run-context contextvars.
        """
        ctx_var = getattr(mo_context, "_RUN_CONTEXT_VARS", None)
        saved_env = dict(os.environ)
        out, err = io.StringIO(), io.StringIO()
        try:
            for key in list(os.environ):
                if key.startswith(("MINI_ORK_", "MO_")):
                    del os.environ[key]
            if ctx_var is not None:
                ctx_var.set({})
            os.environ.update(self.env)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ex.main(
                    [str(self.plan_path)], root=str(self.root),
                    dispatch_fn=dispatch)
            return rc, out.getvalue(), err.getvalue()
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            if ctx_var is not None:
                ctx_var.set({})


def _defect_text():
    """A defect report whose reason must be truncated at 300 characters."""
    sentence = "The spec test demands behaviour the kickoff never specified. "
    return (sentence * 7)[:350] + "ZZBEYOND300ZZ" + sentence * 2


# ── Mechanism 1: a non-verdict finish never triggers a code revise ──────────


def _revise_rounds(harness):
    """Names of the revise *rounds* this run started, if any.

    ``<run>/revise/`` is also where the R5a review ledger writes
    ``findings.json`` (``mini_ork/workflow/review_ledger.py``), so the
    directory existing says nothing about a round. A round is a
    ``round-<n>.md`` file plus the ``current.json`` pointer.
    """
    revise = harness.run_dir / "revise"
    if not revise.is_dir():
        return []
    names = [p.name for p in revise.glob("round-*.md")]
    if (revise / "current.json").exists():
        names.append("current.json")
    return sorted(names)


@pytest.mark.parametrize("reviewer_rc,finish", [(1, "error"), (124, "timeout")])
def test_reviewer_non_verdict_does_not_revise(tmp_path, reviewer_rc, finish):
    harness = _Harness(tmp_path)
    harness.set_verifier_control(pass_=True)
    dispatch = StubDispatch(
        reviewer_rc=reviewer_rc,
        reviewer_text="provider 500: no verdict produced")

    rc, out, err = harness.execute(dispatch)

    assert rc == 1, f"finish_reason={finish!r} takes the failure path"
    assert dispatch.implementer_calls == 1, (
        f"a reviewer finish_reason={finish!r} produced no verdict and must not "
        "revive the implementer")
    assert _revise_rounds(harness) == [], (
        f"no revise round may start on finish_reason={finish!r}")
    assert "code_reviewer" in (out + err), (
        "the skip reason names the reviewer node")
    status, _notes = harness.run_row()
    assert status == "failed"


def test_real_needs_revision_verdict_still_revises(tmp_path):
    """Regression: the happy revise path is unchanged."""
    harness = _Harness(tmp_path)
    harness.set_verifier_control(pass_=True)
    dispatch = StubDispatch(reviewer_rc=0, reviewer_text=NEEDS_REVISION)

    rc, out, err = harness.execute(dispatch)

    assert dispatch.implementer_calls >= 2, (
        "a real needs_revision verdict must start a revise round")
    assert (harness.run_dir / "revise" / "round-1.md").is_file()
    assert json.load(open(harness.run_dir / "revise" / "current.json"))[
        "round"] >= 1
    assert rc == 1, "rounds exhausted with the reviewer still unsatisfied"


# ── Mechanism 2: both sides pass + empty/bytecode diff → not_needed ─────────


def test_both_sides_pass_empty_diff_ends_not_needed(tmp_path):
    harness = _Harness(tmp_path)
    harness.set_verifier_control(pass_=False, candidate_rc=0, base_rc=0,
                                 reason=NO_DELTA_REASON)
    dispatch = StubDispatch(reviewer_rc=0, reviewer_text=NEEDS_REVISION)

    rc, out, err = harness.execute(dispatch)

    status, notes = harness.run_row()
    assert status == "not_needed", (
        "base rc 0 + candidate rc 0 + an empty diff must end not_needed")
    assert "already satisfied at base" in (notes + out + err)
    assert dispatch.implementer_calls == 1
    assert _revise_rounds(harness) == []


def test_both_sides_pass_bytecode_only_diff_ends_not_needed(tmp_path):
    harness = _Harness(tmp_path)
    harness.set_verifier_control(pass_=False, candidate_rc=0, base_rc=0,
                                 reason=NO_DELTA_REASON)

    def bytecode_only(call_no):
        if call_no != 1:
            return
        pycache = harness.target / "__pycache__"
        pycache.mkdir(exist_ok=True)
        (pycache / "mod.cpython-312.pyc").write_bytes(
            b"\x33\x0d\x0a\x00" + b"\x00" * 32)

    dispatch = StubDispatch(reviewer_rc=0, reviewer_text=NEEDS_REVISION,
                            impl_effect=bytecode_only)
    rc, out, err = harness.execute(dispatch)

    status, notes = harness.run_row()
    assert status == "not_needed", (
        "a bytecode-only diff is the 'already satisfied at base' shape")
    assert "already satisfied at base" in (notes + out + err)
    assert dispatch.implementer_calls == 1
    assert _revise_rounds(harness) == []


def test_real_source_diff_is_not_already_satisfied(tmp_path):
    harness = _Harness(tmp_path)
    harness.set_verifier_control(pass_=False, candidate_rc=0, base_rc=0,
                                 reason=NO_DELTA_REASON)

    def real_change(call_no):
        if call_no == 1:
            (harness.target / "feature_module.py").write_text("VALUE = 1\n")

    dispatch = StubDispatch(reviewer_rc=0, reviewer_text=NEEDS_REVISION,
                            impl_effect=real_change)
    rc, out, err = harness.execute(dispatch)

    status, notes = harness.run_row()
    assert status != "not_needed", (
        "a diff touching real source is not an already-satisfied base")
    assert "already satisfied at base" not in (notes + out + err)


# ── Mechanism 3: a filed defect report escalates instead of revising ────────


@pytest.mark.parametrize("defect_name",
                         ["spec-test-defect.md", "questions.md"])
def test_defect_report_escalates_without_another_round(tmp_path, defect_name):
    harness = _Harness(tmp_path)
    harness.set_verifier_control(pass_=True)
    defect_text = _defect_text()
    defect_path = harness.run_dir / defect_name

    def file_defect(call_no):
        if call_no == 1:
            defect_path.write_text(defect_text)

    dispatch = StubDispatch(reviewer_rc=0, reviewer_text=NEEDS_REVISION,
                            impl_effect=file_defect)
    rc, out, err = harness.execute(dispatch)

    assert rc == 1, "an escalation fails the run; it does not quietly pass"
    assert dispatch.implementer_calls == 1, (
        f"{defect_name} present → no further implementer dispatch")
    assert _revise_rounds(harness) == [], (
        "a filed defect report must not start another revise round")
    status, notes = harness.run_row()
    assert status == "failed"

    surfaces = out + err + "\n" + notes
    assert defect_text[:300] in surfaces, (
        "the escalation reason carries the defect file's first 300 chars")
    assert "ZZBEYOND300ZZ" not in surfaces, (
        "the escalation reason is capped at the file's first 300 chars")


# ── Predicate units ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("paths,expected", [
    ([], True),
    (["/t/__pycache__/mod.cpython-312.pyc"], True),
    (["/t/__pycache__/mod.cpython-312.pyo"], True),
    (["/t/pkg/__pycache__/mod.pyc"], True),
    (["/t/pkg/mod.py"], False),
    (["/t/__pycache__/mod.pyc", "/t/pkg/mod.py"], False),
])
def test_is_bytecode_only(paths, expected):
    assert ex._is_bytecode_only(paths) is expected
