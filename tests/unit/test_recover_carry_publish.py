"""A recover publishes exactly what its implementer produced (kickoff
``publish-carry-plus-edits``).

Three contracts pinned here:

1. ``restore.restore_carry_patch`` records ``carry-applied.json`` (patch basename
   + sha256) whenever a carry lands — the proof the publisher needs to count a
   carry patch as authored — on both ``applied`` and ``already_applied``.
2. ``planner._needs_restore`` carries an explicit ``--carry-patch`` even when the
   implementer re-runs (the tree must hold it for the resumed implementer to
   build on); the implicit ``salvage.patch`` / ``rolled-back.json`` rule stays
   limited to the reuse case.
3. ``auto_repair.decide`` treats a run whose publisher finished ``done`` as
   PUBLISHED, whatever ``verdict.json``'s level report still says — the loop must
   not re-verify a run the publisher already delivered.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.recovery import auto_repair as ar  # noqa: E402
from mini_ork.recovery import restore as rs  # noqa: E402
from mini_ork.recovery.plan import RecoveryPlan  # noqa: E402
from mini_ork.recovery.planner import _needs_restore  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")
BASE_A = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
RUN = "run-carry-publish"


def _git(repo: Path, *args: str, check: bool = True):
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")}
    return subprocess.run(["git", *GIT_IDENTITY, *args], cwd=repo, env=env,
                          capture_output=True, text=True, check=check, timeout=60)


def _patch_a_line2() -> str:
    return (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,5 +1,5 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-run\n"
        " line3\n"
        " line4\n"
        " line5\n"
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "a.py").write_text(BASE_A)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "base")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# restore records the applied carry
# ─────────────────────────────────────────────────────────────────────────────


def _run_dir(tmp_path: Path, repo: Path, patch: str, name: str = "salvage.patch") -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"roots": {"target": str(repo)}}))
    (run_dir / name).write_text(patch)
    return run_dir


def test_applied_carry_writes_carry_applied_json(tmp_path, repo):
    patch = _patch_a_line2()
    run_dir = _run_dir(tmp_path, repo, patch)

    status, _msg = rs.restore_carry_patch(str(run_dir), str(repo),
                                          cli_carry_patch="salvage.patch")
    assert status == "applied"

    record = json.loads((run_dir / "carry-applied.json").read_text())
    assert record["patch"] == "salvage.patch"
    assert record["sha256"] == hashlib.sha256(patch.encode("utf-8")).hexdigest()
    assert record["target"] == str(repo)
    assert isinstance(record["applied_at"], float)


def test_already_applied_carry_writes_carry_applied_json(tmp_path, repo):
    patch = _patch_a_line2()
    run_dir = _run_dir(tmp_path, repo, patch)
    # The carry is already on the tree: `git apply --check -R` succeeds.
    (repo / "a.py").write_text(BASE_A.replace("line2\n", "line2-run\n"))

    status, _msg = rs.restore_carry_patch(str(run_dir), str(repo),
                                          cli_carry_patch="salvage.patch")
    assert status == "already_applied"

    record = json.loads((run_dir / "carry-applied.json").read_text())
    assert record["patch"] == "salvage.patch"
    assert record["sha256"] == hashlib.sha256(patch.encode("utf-8")).hexdigest()


def test_dry_run_does_not_write_carry_applied_json(tmp_path, repo):
    run_dir = _run_dir(tmp_path, repo, _patch_a_line2())
    status, _msg = rs.restore_carry_patch(str(run_dir), str(repo), dry_run=True)
    assert status == "applied"
    assert not (run_dir / "carry-applied.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# the explicit --carry-patch is carried even when the implementer re-runs
# ─────────────────────────────────────────────────────────────────────────────


def _plan(reuse: set[str], node_types: dict[str, str]) -> RecoveryPlan:
    return RecoveryPlan(
        run_id="r", recipe="code-fix", task_class="code_fix",
        closure=set(), reuse=set(reuse), failed_node=None, first_node=None,
        from_node=None, strategy="resume", cost_boundary={}, reason={}, sku="",
        node_types=dict(node_types),
    )


def test_explicit_carry_patch_needs_restore_even_when_the_implementer_reruns(tmp_path, repo):
    run_dir = _run_dir(tmp_path, repo, _patch_a_line2(), name="cycle-delta.patch")
    plan = _plan({"implementer"}, {"implementer": "implementer"})

    assert _needs_restore(plan, str(run_dir), "cycle-delta.patch") is True


def test_explicit_carry_patch_that_does_not_resolve_is_not_carried(tmp_path, repo):
    # No patch on disk at all: a --carry-patch that names a missing file is not
    # a restore (and must not fall back to a salvage that is not there either).
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"roots": {"target": str(repo)}}))
    plan = _plan({"implementer"}, {"implementer": "implementer"})

    assert _needs_restore(plan, str(run_dir), "missing.patch") is False


def test_no_carry_patch_and_no_implementer_reuse_needs_no_restore(tmp_path, repo):
    run_dir = _run_dir(tmp_path, repo, _patch_a_line2())
    plan = _plan({"reviewer"}, {"reviewer": "reviewer"})

    assert _needs_restore(plan, str(run_dir)) is False


# ─────────────────────────────────────────────────────────────────────────────
# a published run is not withheld
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for key in ("MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_DB", "MINI_ORK_HOME"):
        monkeypatch.delenv(key, raising=False)
    # tests/conftest.py turns auto-repair off suite-wide; this file exercises it.
    monkeypatch.delenv("MO_AUTO_REPAIR", raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    h.mkdir()
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed_run(home: Path, *, status: str = "failed") -> Path:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": "code-fix"}))
    (run_dir / "verdict.json").write_text(json.dumps({
        "levels_decision": "abstain",
        "levels": {"target": "UNVERIFIED"},
        "levels_reasons": {"target": "no test exercises the change"},
    }))
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "code-fix", status, 0.5, 1791000000, 1791000120, 1791000100,
         "code_fix", "", "latest", "tr-1"),
    )
    con.commit()
    con.close()
    return run_dir


def _seed_publisher_end(home: Path, finish_reason: str) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"evt-node_end-publisher-{finish_reason}", RUN, "node_end",
         json.dumps({"node_id": "publisher", "node_type": "publisher",
                     "finish_reason": finish_reason}), 1791000300),
    )
    con.commit()
    con.close()


_NODES = [
    {"name": "implementer", "type": "implementer"},
    {"name": "test_verifier", "type": "verifier", "verifier_ref": "verifiers/test.py"},
    {"name": "publisher", "type": "publisher"},
]
_EDGES = [
    {"from": "implementer", "to": "test_verifier", "edge_type": "verifies"},
    {"from": "test_verifier", "to": "publisher", "edge_type": "depends_on"},
]


@pytest.fixture(autouse=True)
def _stub_workflow(monkeypatch):
    import mini_ork.recovery.retry_hint as rh
    monkeypatch.setattr(rh, "_recipe_workflow", lambda home, recipe: (list(_NODES), list(_EDGES)))
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: {
        "version": 2, "run_id": RUN, "failed_node": None, "retryable": True,
        "strategy": "verify", "from_node": None, "needs_change": None,
        "notes": [], "command": "", "computed_at": "2026-01-01T00:00:00Z"})


def test_a_publisher_that_finished_done_is_not_reverified(home):
    """Live, 2026-10-08: the recover's auto-repair hook re-verified a PUBLISHED run.

    ``verdict.json`` still carried ``levels_decision: abstain`` while the
    publisher node finished ``done``, so ``decide`` read it as merely withheld
    and spawned a reverify. A publisher that finished ``done`` delivered the run.
    """
    _seed_run(home)
    _seed_publisher_end(home, "done")

    decision = ar.decide(home, RUN)
    assert decision["action"] == "none", decision


def test_a_publisher_that_finished_levels_unverified_is_still_withheld(home):
    """The withheld override survives: ``levels_unverified`` really did withhold."""
    _seed_run(home)
    _seed_publisher_end(home, "levels_unverified")

    decision = ar.decide(home, RUN)
    assert decision["action"] == "reverify", decision
