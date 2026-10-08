"""A failed run is handed to auto-repair right after execute, and post-run
verify does not rebuild a rolled-back tree.

Two fixes from the ``failed-run-lifecycle`` kickoff (run
``ide-orca-f2b-flow-20261008172728``):

1. ``mini_ork/cli/main._run_lifecycle_impl`` ran the post-run verify, rubric
   and reflect tail *before* the auto-repair hook, so a lifecycle that died in
   that fragile tail (here: verify re-running a full Rust build against the
   reverted tree) never reached ``auto_repair.maybe_repair`` — no run has ever
   been repaired since the feature shipped. The hook now runs right after
   ``execute`` returns, before the rubric pre-screen.

2. Post-run verify re-ran a verifier the DAG had skipped on a *rolled-back*
   tree: with no DAG result to reuse it ran the suite against the reverted
   tree, which both destroys the run's only failure evidence and can hang on a
   lock held by a dead implementer's orphan. When the run rolled back, the
   skipped verifier is now left un-run and recorded as an abstention
   (``"reused": "skipped"``), never as a pass or a fail.

The verify-side fixtures (``_scenario``) and the ``MO_VERIFY_RERUN=1`` escape
hatch are shared with ``test_post_run_verify_reuse`` / ``test_mini_ork_verify_py``.
"""
from __future__ import annotations

import inspect
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

# Sibling test module (tests/unit is on sys.path under pytest's default import
# mode); it inserts the repo root into sys.path, so the engine imports below
# resolve without an editable install.
from test_mini_ork_verify_py import REPO, _scenario

from mini_ork.cli import main as cli_main
from mini_ork.cli import verify as ver


def _write_stub(home, marker):
    """A verifier that writes ``marker`` if the verify path ever executes it."""
    (home / "verifiers" / "test.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "print('{\"verifier\":\"test\",\"pass\":true}')\n",
        encoding="utf-8",
    )


def _run(home, db, plan, *, run_dir, rerun=False):
    """Run ``verify.main`` in-process with ``MINI_ORK_RUN_DIR`` set. Mirrors
    ``test_post_run_verify_reuse._run``; returns (doc, stdout, stderr, rc)."""
    out, err = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_RUN_DIR"] = str(run_dir)
    # Scrub the run-scoped knobs so the scenario is hermetic: MINI_ORK_RECIPE in
    # particular would make ``_find_verifier_script`` prefer the real
    # ``recipes/<recipe>/verifiers/test.py`` over this scenario's stub.
    for key in ("MINI_ORK_DRY_RUN", "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS",
                "MINI_ORK_RECIPE"):
        os.environ.pop(key, None)
    if rerun:
        os.environ["MO_VERIFY_RERUN"] = "1"
    else:
        os.environ.pop("MO_VERIFY_RERUN", None)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = ver.main(["art.txt", "--plan", plan], db=db, root=str(REPO))
    finally:
        os.environ.clear()
        os.environ.update(old)
    text = out.getvalue()
    doc = json.loads(text[text.index("{"):])
    return doc, text, err.getvalue(), rc


DECLARED = "verifiers/test.py"


def _row(doc, name):
    rows = [r for r in doc["results"] if r["verifier"] == name]
    assert len(rows) == 1, rows
    return rows[0]


def _skip_payload(doc):
    """The required row shape from the kickoff, keyed on the declared name."""
    return _row(doc, DECLARED)


def test_skipped_verifier_is_not_run_on_a_rolled_back_tree(tmp_path):
    # rolled-back.json exists and there is no verifier_test.json → the workflow
    # skipped this verifier; do NOT run the script against the reverted tree.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rolled-back.json").write_text(
        json.dumps({"paths": [str(tmp_path / "reverted.py")]}), encoding="utf-8")

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert not marker.exists(), "a rolled-back tree must not re-run the verifier"
    row = _skip_payload(doc)
    assert row["reused"] == "skipped"
    assert row["pass"] is None
    assert row["evidence_path"] == ""
    assert row["detail"] == (
        "not run: the workflow skipped this verifier and the run was rolled back")
    # An abstention counts as neither pass nor fail.
    assert doc["pass_count"] == 0 and doc["fail_count"] == 0
    assert "[verify] verifiers/test.py: not re-run on a rolled-back tree" in err
    assert "MO_VERIFY_RERUN=1 to force" in err


def test_rerun_flag_forces_execution_on_a_rolled_back_tree(tmp_path):
    # MO_VERIFY_RERUN=1 is the documented escape hatch: it restores execution
    # even when the run rolled back.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rolled-back.json").write_text(
        json.dumps({"paths": []}), encoding="utf-8")

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir, rerun=True)

    assert marker.exists(), "MO_VERIFY_RERUN=1 must re-run the verifier"
    assert "not re-run on a rolled-back tree" not in err
    assert '"reused":"skipped"' not in text


def test_without_a_rollback_the_verifier_runs_unchanged(tmp_path):
    # No rolled-back.json → the rolled-back guard is inert; behaviour is the
    # pre-existing one (no DAG result → run the script).
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert marker.exists(), "a tree that was not rolled back must still verify"
    assert "not re-run on a rolled-back tree" not in err
    assert '"reused":"skipped"' not in text
    assert "reused DAG result" not in err


def test_a_dag_result_still_wins_over_the_rollback_guard(tmp_path):
    # A persisted DAG result (the verifier DID run before rollback) is reused as
    # before — the new guard only fires when there is nothing to reuse.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rolled-back.json").write_text(json.dumps({"paths": []}), encoding="utf-8")
    (run_dir / "verifier_test.json").write_text(
        json.dumps({"verifier": "test", "pass": False,
                    "error_summary": "cargo rc 101"}),
        encoding="utf-8")

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert not marker.exists()
    row = _skip_payload(doc)
    assert row["reused"] == "dag" and row["pass"] is False
    assert doc["fail_count"] == 1


def test_main_hands_the_failure_to_auto_repair_before_the_rubric_prescreen():
    # Kickoff: exactly ONE ``maybe_repair`` call in the lifecycle, placed right
    # after execute's log write and before the rubric pre-screen. A second call
    # (the old tail site) or a placement after the prescreen re-introduces the
    # bug where a lifecycle that dies in verify never repairs the run.
    src = inspect.getsource(cli_main._run_lifecycle_impl)

    assert src.count("maybe_repair(") == 1, (
        "exactly one auto-repair hand-off is expected in _run_lifecycle_impl")

    repair_at = src.index("maybe_repair(")
    assert src.index("_write_execute_log(") < repair_at, (
        "the hook must run after execute returns")
    assert repair_at < src.index("rubric pre-screen"), (
        "the hook must run before the rubric pre-screen")


def test_main_auto_repair_hook_is_unconditional_and_never_raises():
    # The withheld-publish case: status='failed' is set during execute while
    # run_rc == 0, so the hook must NOT be gated on run_rc — that would silently
    # miss every abstain case. It must also swallow its own errors so a green
    # run stays quiet.
    src = inspect.getsource(cli_main._run_lifecycle_impl)
    lines = src.splitlines()
    call = next(i for i, ln in enumerate(lines) if "maybe_repair(" in ln)
    # Walk back to the ``try:`` that opens the hook's guard.
    guard = max(i for i in range(call) if lines[i].strip() == "try:")

    # Nothing between the hook's guard and the call conditions it on run_rc.
    assert "run_rc" not in "\n".join(lines[guard:call + 1])
    # And the guard is a try/except, not a bare ``if``: the except swallows.
    assert any(ln.strip().startswith("except Exception") for ln in lines[call:call + 3])
    assert "wait_for_exit=True" in lines[call]
