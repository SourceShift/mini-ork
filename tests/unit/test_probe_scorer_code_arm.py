"""Unit tests for the code arm of the probe scorer (``probe_score_code``).

The code arm exists because ``probe_score`` can only score a DIRECTIVE — it
copies the recipe and appends text to a prompt file. A change to gate
arithmetic, lane routing, or a harness module has no directive to append, so
before this the answer to "run the improvement and see whether it was real" was
"you cannot" for every candidate that was not a prompt edit.

These tests never shell out to a real lane and never boot the real schema: the
launch seam and the DB bootstrap are monkeypatched, so what is under test is the
ARM CONSTRUCTION — which tree carries the patch, whether an unappliable patch is
refused rather than scored, whether the worktrees are cleaned up on every exit
path, and whether an outcome is read from the arm's own database.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mini_ork.learning import probe_scorer as ps


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture
def framework_repo(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like the framework tree.

    One committed file (the thing a patch changes) plus a recipe carrying a
    frozen probe set (`recipes/code-fix/probes/*.md`), which is what
    ``_frozen_probes`` discovers.
    """
    root = tmp_path / "fw"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "mod.py").write_text("VALUE = 1\n")
    probe_dir = root / "recipes" / "code-fix" / "probes"
    probe_dir.mkdir(parents=True)
    (probe_dir / "p1.md").write_text("# probe one\n")
    (probe_dir / "p2.md").write_text("# probe two\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def stub_arm(monkeypatch: pytest.MonkeyPatch):
    """Neutralize the expensive seams and record each arm's tree + file body."""
    seen: dict[str, str] = {}
    counter = {"n": 0}

    def fake_launch(recipe, kickoff, target_cwd=None, root=None):
        counter["n"] += 1
        assert root is not None, "the code arm must always identify its tree"
        seen[str(root)] = (Path(root) / "mod.py").read_text()
        rid = f"run-{counter['n']}-{counter['n']}"
        return (f'mini_ork_result={{"run_id": "{rid}"}}', rid, 0.0)

    monkeypatch.setattr(ps, "_launch_run", fake_launch)
    monkeypatch.setattr(ps, "_run_outcome", lambda run_id, db=None: 1.0)
    monkeypatch.setattr(ps, "_seed_arm_home", lambda tree: None)
    monkeypatch.setattr(
        ps, "_bootstrap_arm_db",
        lambda tree: os.path.join(tree, ".mini-ork", "state.db"))
    # Hermetic: _write_null_calibration writes into ${MINI_ORK_RUN_DIR}, so a
    # unit test running inside a real run would drop a plausible-looking
    # calibration (before/after/n/delta) into that run's artifacts — evidence
    # that reads exactly like a measurement, but came from stubbed seams.
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    return seen


def _patch(tmp_path: Path, body: str) -> str:
    path = tmp_path / "candidate.patch"
    path.write_text(body)
    return str(path)


def test_candidate_arm_carries_patch_and_baseline_does_not(
        framework_repo: Path, tmp_path: Path, stub_arm, monkeypatch) -> None:
    """The whole point of the arm split: one tree is changed, the other is not."""
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    patch = _patch(tmp_path, (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    ))

    result = ps.probe_score_code("code-fix", patch)

    assert result is not None
    assert result["n"] == 2
    assert sorted(stub_arm.values()) == ["VALUE = 1\n", "VALUE = 2\n"]


def test_unappliable_patch_returns_none(
        framework_repo: Path, tmp_path: Path, stub_arm, monkeypatch) -> None:
    """A patch that cannot apply is refused, never scored as a neutral delta."""
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    patch = _patch(tmp_path, (
        "--- a/does-not-exist.py\n"
        "+++ b/does-not-exist.py\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    ))

    assert ps.probe_score_code("code-fix", patch) is None
    assert stub_arm == {}  # nothing was launched against a non-candidate


def test_empty_patch_is_the_null_calibration(
        framework_repo: Path, tmp_path: Path, stub_arm, monkeypatch) -> None:
    """An empty patch is a legitimate no-op arm, not an unappliable patch.

    This is the calibration: identical arms must produce an identical score, and
    a non-zero delta here would mean the instrument measures noise.
    """
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))

    result = ps.probe_score_code("code-fix", _patch(tmp_path, ""))

    assert result is not None
    assert result["before"] == result["after"]
    assert sorted(stub_arm.values()) == ["VALUE = 1\n", "VALUE = 1\n"]


def test_worktrees_removed_on_every_exit_path(
        framework_repo: Path, tmp_path: Path, stub_arm, monkeypatch) -> None:
    """No arm tree survives the call — including the unappliable-patch path."""
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    patch = _patch(tmp_path, (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    ))

    ps.probe_score_code("code-fix", patch)
    trees = list(stub_arm.keys())
    assert trees, "expected the stub to have seen at least one arm tree"
    for tree in trees:
        assert not os.path.exists(tree), f"arm tree leaked: {tree}"

    listed = _git(framework_repo, "worktree", "list", "--porcelain")
    assert "mo-code-arm-" not in listed


def test_unappliable_patch_leaves_no_worktree(
        framework_repo: Path, tmp_path: Path, stub_arm, monkeypatch) -> None:
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    patch = _patch(tmp_path, (
        "--- a/does-not-exist.py\n"
        "+++ b/does-not-exist.py\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    ))

    assert ps.probe_score_code("code-fix", patch) is None
    listed = _git(framework_repo, "worktree", "list", "--porcelain")
    assert "mo-code-arm-" not in listed


def test_run_outcome_reads_the_named_db_not_the_ambient_one(
        tmp_path: Path, monkeypatch) -> None:
    """Reading a score from the wrong arm's database is the failure this guards."""
    arm_db = tmp_path / "arm" / "state.db"
    arm_db.parent.mkdir(parents=True)
    con = sqlite3.connect(arm_db)
    con.execute("CREATE TABLE task_runs (id TEXT, status TEXT)")
    con.execute("INSERT INTO task_runs VALUES ('run-424242-1', 'published')")
    con.commit()
    con.close()

    # Ambient DB: an empty schema, so the same run_id cannot be found there.
    ambient = tmp_path / "ambient" / "state.db"
    ambient.parent.mkdir(parents=True)
    con = sqlite3.connect(ambient)
    con.execute("CREATE TABLE task_runs (id TEXT, status TEXT)")
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_DB", str(ambient))

    assert ps._run_outcome("run-424242-1", db=str(arm_db)) == 1.0
    assert ps._run_outcome("run-424242-1") == 0.0


def test_no_frozen_probe_set_returns_none(tmp_path: Path, monkeypatch) -> None:
    """No probe set means nothing was measured, so there is no score to return."""
    root = tmp_path / "bare"
    (root / "recipes" / "code-fix").mkdir(parents=True)
    monkeypatch.setattr(ps, "_ROOT", str(root))

    assert ps.probe_score_code("code-fix", None) is None


def test_materialized_target_is_a_git_repo(tmp_path: Path) -> None:
    """A bare directory copy is not a target: the edit surface escapes it.

    ``_resolve_target_cwd`` (cli/execute.py) honours ``MO_TARGET_CWD`` only when
    ``git rev-parse --show-toplevel`` succeeds on it, and otherwise falls back to
    the kickoff's toplevel — the framework root. Before this the probe handed the
    run a plain copy, so the implementer edited the framework tree, the review
    diff came back empty, and the probe graded a fixture nothing had touched.
    """
    probe_dir = tmp_path / "recipes" / "code-fix" / "probes"
    fixture = probe_dir / "fixtures" / "p1"
    fixture.mkdir(parents=True)
    (fixture / "tally.py").write_text("VALUE = 1\n")
    probe = probe_dir / "p1.md"
    probe.write_text("# probe\n")

    made = ps._materialize_target(str(probe))
    assert made is not None
    target = Path(made)
    try:
        top = _git(target, "rev-parse", "--show-toplevel").strip()
        assert os.path.realpath(top) == os.path.realpath(target)
        assert (target / "tally.py").read_text() == "VALUE = 1\n"
        # Committed, so the arm's own diff against the fixture is computable.
        assert "tally.py" in _git(target, "ls-files")
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _capture_launch(monkeypatch: pytest.MonkeyPatch, *, timed_out: bool = False) -> dict:
    """Stub the CLI spawn and capture exactly what the arm was launched with."""
    captured: dict = {}

    class _FakePopen:
        returncode = 0

        def __init__(self, cmd, cwd=None, env=None, **kw):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["env"] = dict(env or {})
            self.pid = 4242

        def poll(self):
            # A timed-out arm is one that is still running with no terminal run
            # row to be found; a normal arm has already exited.
            return None if timed_out else 0

        def communicate(self, timeout=None):
            if timed_out:
                raise subprocess.TimeoutExpired(cmd=captured["cmd"], timeout=timeout or 0)
            return ('mini_ork_result={"run_id": "run-1-1"}\n', "")

    monkeypatch.setattr(ps.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(ps, "_root", lambda: "/framework")
    return captured


def test_launch_run_points_the_arm_at_its_own_tree(monkeypatch) -> None:
    """The arm's run resolves the ARM's tree, home, and db — not the framework's.

    ``_launch_run`` used to rebind its own ``root`` parameter to the framework
    root before it was read, so every code arm silently ran against the
    framework tree and wrote into the AMBIENT database: the exact cross-arm
    attribution failure the parameter exists to prevent, and one the stub-based
    arm tests cannot see because they replace ``_launch_run`` wholesale.
    """
    captured = _capture_launch(monkeypatch)

    ps._launch_run("code-fix", "probe.md", target_cwd=None, root="/arm/tree")

    assert captured["cwd"] == "/arm/tree"
    assert captured["env"]["MINI_ORK_ROOT"] == "/arm/tree"
    assert captured["env"]["MINI_ORK_HOME"] == "/arm/tree/.mini-ork"
    assert captured["env"]["MINI_ORK_DB"] == "/arm/tree/.mini-ork/state.db"


def test_launch_run_without_an_arm_still_uses_the_framework_root(monkeypatch) -> None:
    """The directive scorer passes no tree and must keep resolving the framework."""
    captured = _capture_launch(monkeypatch)

    ps._launch_run("code-fix", "probe.md")

    assert captured["cwd"] == "/framework"
    assert captured["env"]["MINI_ORK_ROOT"] == "/framework"


def test_timed_out_arm_kills_the_process_group_and_raises(monkeypatch) -> None:
    """A blown timeout must take the whole process tree with it.

    ``subprocess.run(timeout=)`` signals only the direct child, so the arm's
    verifier chain (``mini_ork.cli.verify`` → the target's ``pytest``) is
    reparented to init and keeps burning CPU against a worktree the caller is
    about to delete. The kill is asserted through the seam that owns it.
    """
    _capture_launch(monkeypatch, timed_out=True)
    monkeypatch.setenv("MO_APPLY_PROBE_TIMEOUT_S", "0")
    killed: list[int] = []
    monkeypatch.setattr(ps, "_kill_process_group", lambda proc: killed.append(proc.pid))

    with pytest.raises(ps.ProbeArmTimeout):
        ps._launch_run("code-fix", "probe.md")

    assert killed == [4242]


def test_lingering_arm_is_scored_once_its_run_is_terminal(monkeypatch) -> None:
    """A run that publishes and then keeps scoring must not be waited on forever.

    ``mini_ork run`` writes the run's terminal status at publish and continues
    into rubric scoring / eval — work the probe does not score. Observed live:
    an arm published at $1.40 in ~3 minutes and was still inside rubric scoring
    when the timeout fired, which recorded a timeout and threw away a completed
    measurement. The run was decided; only the process had not returned.
    """
    _capture_launch(monkeypatch, timed_out=True)  # the process never exits
    killed: list[int] = []
    monkeypatch.setattr(ps, "_kill_process_group", lambda proc: killed.append(proc.pid))
    monkeypatch.setattr(ps, "_await_arm_run", lambda *a, **k: ("terminal", "run-7-4242"))
    monkeypatch.setattr(ps, "_run_cost", lambda run_id, db=None: 1.4)

    out, run_id, cost = ps._launch_run("code-fix", "probe.md")

    assert run_id == "run-7-4242"
    assert cost == 1.4
    assert killed == [4242], "the lingering arm is reaped, not waited on"


def test_timed_out_arm_keeps_the_probes_already_measured(
        framework_repo: Path, tmp_path: Path, monkeypatch) -> None:
    """The sweep stops on a timeout without discarding what it already scored.

    Before this the arm timeout propagated out of ``probe_score_code``, so a
    run that blew its budget on probe 2 threw away probe 1's completed
    measurement — an arm failure destroying a whole sweep's evidence, which is
    the opposite of what a no-regression gate can tolerate.
    """
    launched: list[str] = []

    def fake_launch(recipe, kickoff, target_cwd=None, root=None):
        stem = os.path.basename(kickoff)
        launched.append(stem)
        if stem == "p2.md" and launched.count("p2.md") == 1:
            raise ps.ProbeArmTimeout("probe arm exceeded MO_APPLY_PROBE_TIMEOUT_S=1s")
        return ('mini_ork_result={"run_id": "r"}', f"run-{stem}", 0.0)

    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    monkeypatch.setattr(ps, "_launch_run", fake_launch)
    monkeypatch.setattr(ps, "_run_outcome", lambda run_id, db=None: 1.0)
    monkeypatch.setattr(ps, "_seed_arm_home", lambda tree: None)
    monkeypatch.setattr(
        ps, "_bootstrap_arm_db",
        lambda tree: os.path.join(tree, ".mini-ork", "state.db"))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    result = ps.probe_score_code("code-fix", None)

    assert result is not None
    assert result["n"] == 1, "probe 1 was measured in both arms and must survive"
    assert result["before"] == result["after"]
    timed_out = [r for r in result["runs"] if r.get("timed_out")]
    assert len(timed_out) == 1 and timed_out[0]["probe"] == "p2.md"
    assert launched == ["p1.md", "p1.md", "p2.md"], "p2's candidate arm must not run"
