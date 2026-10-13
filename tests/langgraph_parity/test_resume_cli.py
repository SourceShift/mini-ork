"""LangGraph parity tests for the P1.3 resume CLI surface.

Pins AC1 of ``p1-3-resume-cli.md``:

  * ``mini-ork run --resume <run_id> [--checkpoint-id <id>]`` is parsed
    correctly by the argparse surface (both ``--flag value`` and
    ``--flag=value`` forms; the lifecycle's positional args survive).
  * The one-line resume plan is printed to stdout in the F2 shape
    (``[mini-ork-resume] resume run '<id>' skip=[...] redispatch=[...]``).
  * ``build_resume_plan`` rebuilds the dispatch set per the checkpointer
    contract — succeeded nodes skip with persisted outputs, failed /
    error / never-ran nodes re-dispatch (F1).
  * ``--checkpoint-id <id>`` rewinds the resume so steps at or after
    the checkpoint are forced into the dispatch set (F3).
  * Unknown ``run_id`` exits non-zero with a clear stderr message and
    never silently starts fresh (C1).
  * Corrupt checkpoint rows hard-error naming the run + node, never
    silently re-execute.

These tests use the same ``migrate_db`` / ``tmp_path`` isolation pattern
as ``test_checkpointer.py`` — no network, no model calls, no full
``mini-ork run`` lifecycle fork. The CLI surface itself is exercised
through the module's pure helpers (``parse_resume_flags``,
``validate_resume_target``, ``build_resume_plan``, ``run_with_resume``)
and the wiring through ``_run_handler`` is asserted at the registry
table only (a subprocess fork of the full lifecycle would pull in
classify / plan / execute and is intentionally out of scope for AC1).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Force the checkpointer gate ON for these tests so the per-row INSERTs
# in the fixture helpers land; ``mini_ork.checkpoints`` reads the env
# at import time.
os.environ["CHECKPOINTER_ENABLED"] = "1"

from mini_ork import checkpoints as ck
from mini_ork.cli import run_resume
from mini_ork.cli.main import SUBCOMMAND_REGISTRY
from mini_ork.stores import migrate as mc_migrate


# ─── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def migrate_db(tmp_path: Path) -> str:
    db = str(tmp_path / "state.db")
    rc, _out, _err = mc_migrate.init_db(db=db, root=str(REPO))
    assert rc == 0, f"init_db failed; rc={rc}"
    return db


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> str:
    """A minimal home with ``<home>/state.db`` already migrated + a
    writable ``<home>/runs/`` directory. ``MINI_ORK_HOME`` is pointed at
    it for the duration of the test.
    """
    h = str(tmp_path / "home")
    os.makedirs(os.path.join(h, "runs"))
    monkeypatch.setenv("MINI_ORK_HOME", h)
    monkeypatch.setenv("MINI_ORK_DB", os.path.join(h, "state.db"))
    # Re-run init_db against the new location so the test's state.db is
    # the one the resume module reads.
    rc, _out, _err = mc_migrate.init_db(
        db=os.path.join(h, "state.db"), root=str(REPO),
    )
    assert rc == 0
    return h


@pytest.fixture
def run_id(home: str) -> str:
    """Create a run dir so ``--resume <id>`` finds a valid run."""
    rid = "run-test-1"
    os.makedirs(os.path.join(home, "runs", rid))
    return rid


@pytest.fixture(autouse=True)
def _gate_on():
    ck.enable(True)
    yield
    ck.enable(True)


def _record(run_id: str, step_id: str, status: str, *,
            artifact_path: str | None = None,
            artifact_sha256: str | None = None) -> None:
    """Write a completion row in a form ``record_completion`` accepts."""
    db = os.environ["MINI_ORK_DB"]
    ck.record_completion(
        db,
        run_id=run_id, step_id=step_id, node_id=step_id,
        status=status,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        rc=0 if status == "succeeded" else 1,
        started_at=0, ended_at=1,
    )


# ─── flag parsing ──────────────────────────────────────────────────────────


def test_parse_resume_flags_extracts_run_id():
    """AC1: ``--resume <run_id>`` is parsed and the positional argv
    survives intact for the downstream lifecycle parser."""
    resume_id, rest, info = run_resume.parse_resume_flags(
        ["my-recipe", "kickoff.md", "--resume", "run-abc-1"],
    )
    assert resume_id == "run-abc-1"
    assert rest == ["my-recipe", "kickoff.md"]
    # No --checkpoint-id ⇒ no reset info.
    assert info == {}


def test_parse_resume_flags_accepts_equals_form():
    """The CLI accepts both ``--flag value`` and ``--flag=value``."""
    resume_id, rest, _info = run_resume.parse_resume_flags(
        ["--resume=run-eq-1", "recipe", "kickoff.md"],
    )
    assert resume_id == "run-eq-1"
    assert rest == ["recipe", "kickoff.md"]


def test_parse_resume_flags_extracts_checkpoint_id():
    """AC1: ``--checkpoint-id <id>`` rewinds the resume."""
    resume_id, rest, info = run_resume.parse_resume_flags(
        ["--resume", "run-abc-1", "--checkpoint-id", "step-3",
         "recipe", "kickoff.md"],
    )
    assert resume_id == "run-abc-1"
    assert rest == ["recipe", "kickoff.md"]
    assert info.get("reset") is True


def test_parse_resume_flags_passes_through_unknown_flags():
    """Flags we don't own are LEFT IN PLACE so the lifecycle parser can
    reject them with its own diagnostics — we never swallow or silently
    accept them."""
    _resume_id, rest, _info = run_resume.parse_resume_flags(
        ["--resume", "run-x", "--not-a-real-flag", "recipe", "kickoff.md"],
    )
    assert "--not-a-real-flag" in rest
    assert "recipe" in rest


def test_parse_resume_flags_absent_returns_none():
    """A fresh-run invocation (no --resume) parses cleanly with resume_id=None
    — the lifecycle runs as a normal start, no resume preflight engaged."""
    resume_id, _rest, _info = run_resume.parse_resume_flags(
        ["recipe", "kickoff.md"],
    )
    assert resume_id is None


# ─── preflight: unknown run_id ─────────────────────────────────────────────


def test_unknown_run_id_exits_non_zero(home, capsys):
    """AC1: ``--resume <bogus>`` exits non-zero with a clear stderr
    message and DOES NOT print a resume plan line — no silent fresh
    start (F4 / C1)."""
    rc, sink = run_resume.run_with_resume(
        ["--resume", "run-bogus", "recipe", "kickoff.md"],
    )
    captured = capsys.readouterr()
    assert rc == run_resume.RC_UNKNOWN_RUN
    # The success-state keys MUST NOT be set on the error path — only
    # the intent (``resume_id``) is preserved so the caller can log it.
    assert "run_id" not in sink
    assert "resume_dispatch" not in sink
    assert "resume_db" not in sink
    assert "resume_run_dir" not in sink
    assert "[mini-ork-resume]" in captured.err
    assert "run-bogus" in captured.err
    assert "Refusing" in captured.err or "unknown" in captured.err.lower()
    # The plan line must NOT have been printed — that would imply a
    # silent fresh start, which is exactly what F4 forbids.
    assert "[mini-ork-resume] resume run" not in captured.out


def test_unknown_run_id_does_not_bind_mini_ork_run_id(home, monkeypatch):
    """The error path MUST NOT bind ``MINI_ORK_RUN_ID`` to the bogus
    id, otherwise the lifecycle would proceed under the pretense of a
    resume. F4 contract."""
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    rc, _sink = run_resume.run_with_resume(
        ["--resume", "run-bogus", "recipe", "kickoff.md"],
    )
    assert rc == run_resume.RC_UNKNOWN_RUN
    assert os.environ.get("MINI_ORK_RUN_ID") != "run-bogus"


# ─── preflight: valid run_id ───────────────────────────────────────────────


def test_valid_run_id_binds_mini_ork_run_id(home, run_id):
    """A valid resume binds ``MINI_ORK_RUN_ID`` so the lifecycle re-uses
    the existing run dir (C1 — re-use the original run, never spin up a
    new run id for a resume)."""
    prev = os.environ.pop("MINI_ORK_RUN_ID", None)
    try:
        rc, sink = run_resume.run_with_resume(
            ["--resume", run_id, "recipe", "kickoff.md"],
        )
        assert rc == run_resume.RC_OK
        assert os.environ.get("MINI_ORK_RUN_ID") == run_id
        assert sink["resume_id"] == run_id
        assert sink["run_id"] == run_id
        # argv_after_resume must drop the resume flags but keep the rest.
        assert sink["argv_after_resume"] == ["recipe", "kickoff.md"]
    finally:
        if prev is not None:
            os.environ["MINI_ORK_RUN_ID"] = prev


def test_absent_resume_is_passthrough(home, capsys):
    """No ``--resume`` on the invocation → the module is a no-op and
    the lifecycle proceeds as a fresh start."""
    rc, sink = run_resume.run_with_resume(["recipe", "kickoff.md"])
    captured = capsys.readouterr()
    assert rc == run_resume.RC_OK
    assert sink == {}
    assert captured.out == ""


# ─── F2 plan line shape ─────────────────────────────────────────────────────


def test_plan_line_format_matches_f2_contract(home, run_id, capsys):
    """AC1: the one-line plan line lists exactly which nodes are skipped
    and which are re-dispatched, on stdout (F2)."""
    _record(run_id, "A", "succeeded")
    _record(run_id, "B", "failed")
    rc, sink = run_resume.run_with_resume(
        ["--resume", run_id, "recipe", "kickoff.md"],
    )
    captured = capsys.readouterr()
    assert rc == run_resume.RC_OK
    plan_line = captured.out
    # The plan line is the ONLY stdout output and it must contain both
    # node ids in their correct buckets.
    assert plan_line.startswith("[mini-ork-resume]")
    assert f"resume run {run_id!r}" in plan_line
    assert "skip=[A]" in plan_line
    assert "redispatch=[B]" in plan_line
    # No logger noise — the plan line must land first so a grep on
    # stdout can isolate it (F2).
    assert plan_line.endswith("\n")
    # And the sink carries the dispatch set the lifecycle will consume.
    assert sink["resume_dispatch"] == ["B"]


def test_plan_line_no_prior_progress(home, run_id, capsys):
    """A resume against a run that has no checkpoints yet prints a plan
    with ``skip=[]`` and every candidate in the dispatch set. This is
    the C1 contract: the run is "replayed" from scratch via re-dispatch,
    not via a silent fresh start."""
    rc, _sink = run_resume.run_with_resume(
        ["--resume", run_id, "recipe", "kickoff.md"],
    )
    captured = capsys.readouterr()
    assert rc == run_resume.RC_OK
    assert "skip=[]" in captured.out
    # Without a candidate set provided by the caller, the plan defaults
    # to no redispatch targets (the lifecycle owns the candidate set —
    # it will intersect its workflow candidate list against this list
    # at the dispatch step).
    assert "redispatch=[]" in captured.out


# ─── F1 dispatch-set rebuild ───────────────────────────────────────────────


def test_dispatch_set_skips_succeeded_redispatches_failed(home, run_id):
    """AC1 (interrupted run): A succeeded → skip; B failed → redispatch.
    The checkpointer rebuild is the engine-level contract this surface
    surfaces on stdout."""
    _record(run_id, "A", "succeeded")
    _record(run_id, "B", "failed")

    db = os.environ["MINI_ORK_DB"]
    plan_line, dispatch = run_resume.build_resume_plan(
        db, run_id, candidate_steps=["A", "B"],
    )
    assert dispatch == ["B"], "B must re-enter the dispatch set"
    assert "skip=[A]" in plan_line
    assert "redispatch=[B]" in plan_line


def test_dispatch_set_treats_error_as_redispatch(home, run_id):
    """The F4 control signal ('error') MUST re-enter the dispatch set —
    never silently skipped."""
    _record(run_id, "A", "succeeded")
    _record(run_id, "B", "error")

    db = os.environ["MINI_ORK_DB"]
    plan_line, dispatch = run_resume.build_resume_plan(
        db, run_id, candidate_steps=["A", "B"],
    )
    assert dispatch == ["B"]
    assert "skip=[A]" in plan_line
    assert "redispatch=[B]" in plan_line


def test_dispatch_set_never_ran_is_redispatch(home, run_id):
    """A candidate with no completion row is the "never ran" case —
    kept in the dispatch set so it actually runs on resume."""
    db = os.environ["MINI_ORK_DB"]
    plan_line, dispatch = run_resume.build_resume_plan(
        db, run_id, candidate_steps=["never-ran"],
    )
    assert dispatch == ["never-ran"]
    assert "redispatch=[never-ran]" in plan_line


def test_dispatch_set_preserves_input_order(home, run_id):
    """The dispatch set preserves the candidate order so the lifecycle
    sees the same topological intent it built for the original pass."""
    _record(run_id, "middle", "succeeded")
    db = os.environ["MINI_ORK_DB"]
    _, dispatch = run_resume.build_resume_plan(
        db, run_id, candidate_steps=["first", "middle", "last"],
    )
    assert dispatch == ["first", "last"]


# ─── F3 checkpoint-id rewind ───────────────────────────────────────────────


def test_checkpoint_id_rewinds_dispatch(home, run_id):
    """AC1: ``--checkpoint-id <id>`` addresses an earlier point in the
    run — every step recorded up to and including the checkpoint is
    treated as succeeded for skip purposes, and every step beyond it
    is in the dispatch set."""
    # The run progressed through A, B, C — all completed.
    _record(run_id, "A", "succeeded")
    _record(run_id, "B", "succeeded")
    _record(run_id, "C", "succeeded")

    db = os.environ["MINI_ORK_DB"]
    # Rewind to B: A is "before" the checkpoint (skip), B/C are "at or
    # after" (redispatch).
    plan_line, dispatch = run_resume.build_resume_plan(
        db, run_id,
        candidate_steps=["A", "B", "C"],
        checkpoint_id="B",
    )
    assert dispatch == ["B", "C"]
    assert "skip=[A]" in plan_line
    assert "redispatch=[B,C]" in plan_line


def test_checkpoint_id_plan_line_includes_rewind_phrase(home, run_id, capsys):
    """The plan line MUST mark the rewind so the operator can see the
    resume address is an earlier point, not the current state."""
    _record(run_id, "A", "succeeded")
    _record(run_id, "B", "succeeded")

    rc, _sink = run_resume.run_with_resume(
        ["--resume", run_id, "--checkpoint-id", "A", "recipe", "kickoff.md"],
    )
    captured = capsys.readouterr()
    assert rc == run_resume.RC_OK
    assert "from checkpoint-id='A'" in captured.out


# ─── Errors contract ───────────────────────────────────────────────────────


def test_corrupt_checkpoint_row_hard_errors(home, run_id, monkeypatch):
    """A checkpoint row that cannot be parsed MUST hard-error naming
    the run + node — never silently re-execute. The Errors contract in
    the SpecCard is the C1 zero-fallback rule applied at the row level."""
    from mini_ork.checkpoints.store import CompletionRow

    bad = CompletionRow(
        run_id=run_id, step_id="step-7", node_id="node-7",
        status="mystery",  # outside the F1 vocabulary — CheckpointCorrupt
        artifact_path=None, artifact_sha256=None,
        rc=1, started_at=0, ended_at=1, recorded_at=2,
    )
    # Patch the SQL reader (NOT the public list_completions, which
    # would short-circuit from_row). Driving the failure through the
    # SQL layer means the real list_completions / from_row validation
    # raises — same path the production code hits on a hand-edited DB.
    monkeypatch.setattr(
        ck, "list_completions_db", lambda db, *, run_id: [bad],
    )

    db = os.environ["MINI_ORK_DB"]
    with pytest.raises(run_resume.ResumeError) as excinfo:
        run_resume.build_resume_plan(
            db, run_id, candidate_steps=["step-7"],
        )
    err = excinfo.value
    assert err.rc == run_resume.RC_CORRUPT
    assert err.run_id == run_id
    # The message names the bad node + step so the operator can find it.
    assert "node-7" in err.message
    assert "step-7" in err.message


# ─── CLI wiring (registry table) ───────────────────────────────────────────


def test_run_handler_is_registered():
    """The ``run`` subcommand is wired through the SUBCOMMAND_REGISTRY
    table; the resume flags are intercepted inside that handler."""
    assert "run" in SUBCOMMAND_REGISTRY
    # The handler is the in-process ``_run_handler`` (not a subprocess
    # fork) so the resume preflight can read the same argv the CLI
    # entry saw.
    assert SUBCOMMAND_REGISTRY["run"].__name__ == "_run_handler"
