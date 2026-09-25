"""Unit tests for the framework-edit ground-truth harvest.

framework-edit's implementer self-writes ``framework-edit.diff`` and nothing
validated that artifact against reality. A hallucinating agent emitted a corrupt
diff — garbage hunk context, already-landed files re-declared as new creations —
*and* fabricated its verification claims (``git apply --check -> EXIT=0``),
burning the whole verifier+reviewer wave before the lie surfaced.

The working tree is the only witness that cannot lie, so
``_harvest_framework_edit_ground_truth`` re-derives the delta from git and
rewrites the artifact from it.

Every case below runs against a real temp git repo, and each one fails on the
pre-fix behaviour (the helper did not exist; ``AttributeError``) as well as on
the specific regression it pins.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex
from mini_ork.cli import execute_handlers as exh


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\ntwo\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    return repo


def _baseline(run_dir: Path, repo: Path, monkeypatch) -> None:
    """Simulate the pre-implementer snapshot: run_dir holds the baseline ref and
    the pre-existing untracked inventory, exactly as the run writes them before
    the first implementer iteration."""
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    run_dir.mkdir(parents=True, exist_ok=True)
    ex._capture_pre_impl_baseline(str(run_dir))
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    assert (run_dir / "pre-implementer-ref").is_file()
    assert (run_dir / "pre-implementer-untracked").is_file()


def test_baseline_records_the_untracked_inventory(tmp_path, monkeypatch):
    """``git stash create`` snapshots TRACKED state only, so the untracked
    inventory must be captured separately or the harvest cannot tell
    implementer-created files from pre-existing untracked dirt."""
    repo = _mk_repo(tmp_path)
    (repo / "pre_existing.txt").write_text("someone else's scratch\n")
    run_dir = tmp_path / "run"

    _baseline(run_dir, repo, monkeypatch)

    assert "pre_existing.txt" in (run_dir / "pre-implementer-untracked").read_text()


def test_empty_tree_without_a_diff_fails_the_node(tmp_path, monkeypatch, capsys):
    """The agent exits 0 having written nothing and shipped no diff artifact.

    Pre-fix this was an unchecked pass; the run then spent five more nodes
    discovering there was nothing to verify.
    """
    repo = _mk_repo(tmp_path)
    run_dir = tmp_path / "run"
    _baseline(run_dir, repo, monkeypatch)

    ok, reason = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert (ok, reason) == (False, "impl_no_changes")
    assert "no tree changes" in capsys.readouterr().err


def test_tree_untouched_but_a_usable_agent_diff_is_applied(
        tmp_path, monkeypatch, capsys):
    """A diff-only agent is legitimate: the artifact is applied once, and only
    then does the tree become the source of the rewritten diff."""
    repo = _mk_repo(tmp_path)
    run_dir = tmp_path / "run"
    _baseline(run_dir, repo, monkeypatch)
    (repo / "a.txt").write_text("one\nCHANGED\n")
    patch = _git(repo, "diff").stdout
    _git(repo, "checkout", "--", "a.txt")  # tree clean again, diff held aside
    (run_dir / "framework-edit.diff").write_text(patch)

    ok, reason = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert (ok, reason) == (True, "")
    assert (repo / "a.txt").read_text() == "one\nCHANGED\n"
    # Node-visible progress goes to stdout; only failures use stderr.
    assert "agent diff applied cleanly" in capsys.readouterr().out


def test_unusable_agent_diff_fails_immediately(tmp_path, monkeypatch, capsys):
    """Both signals are empty/lying: fail the implementer node NOW rather than
    letting a corrupt artifact reach the reviewers."""
    repo = _mk_repo(tmp_path)
    run_dir = tmp_path / "run"
    _baseline(run_dir, repo, monkeypatch)
    (run_dir / "framework-edit.diff").write_text("this is not a diff\n")

    ok, reason = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert (ok, reason) == (False, "impl_diff_unusable")
    assert "does not apply" in capsys.readouterr().err


def test_tree_delta_wins_and_the_agents_artifact_is_preserved(
        tmp_path, monkeypatch, capsys):
    """The fabricated artifact must not survive, but it is kept alongside the
    rewrite (as ``.agent``) so the lie stays auditable."""
    repo = _mk_repo(tmp_path)
    run_dir = tmp_path / "run"
    _baseline(run_dir, repo, monkeypatch)
    (run_dir / "framework-edit.diff").write_text("agent's fabricated diff\n")
    (repo / "a.txt").write_text("one\nREAL\n")

    ok, reason = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert (ok, reason) == (True, "")
    rewritten = (run_dir / "framework-edit.diff").read_text()
    assert "REAL" in rewritten
    assert "fabricated" not in rewritten
    assert (run_dir / "framework-edit.diff.agent").read_text() == "agent's fabricated diff\n"
    assert "rewritten from tree delta" in capsys.readouterr().out


def test_implementer_created_untracked_file_lands_in_the_diff(
        tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    run_dir = tmp_path / "run"
    _baseline(run_dir, repo, monkeypatch)
    (repo / "brand_new.py").write_text("print('new')\n")

    ok, _ = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert ok is True
    assert "brand_new.py" in (run_dir / "framework-edit.diff").read_text()


def test_preexisting_untracked_dirt_is_not_claimed_as_the_implementers_work(
        tmp_path, monkeypatch):
    """The phantom-untracked trap: a concurrent session's scratch file was
    already there before the run, so publishing it as the implementer's delta
    would ship another session's work."""
    repo = _mk_repo(tmp_path)
    (repo / "someone_elses_scratch.txt").write_text("not mine\n")
    run_dir = tmp_path / "run"
    _baseline(run_dir, repo, monkeypatch)
    (repo / "mine.py").write_text("x = 1\n")

    ok, _ = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert ok is True
    diff = (run_dir / "framework-edit.diff").read_text()
    assert "mine.py" in diff
    assert "someone_elses_scratch.txt" not in diff


def test_run_mirror_evidence_is_never_harvested(tmp_path, monkeypatch):
    """Agents write to ``<target>/.mini-ork/`` when sandboxed; that is run-mirror
    evidence, not a product change, and must never enter the shipped diff."""
    repo = _mk_repo(tmp_path)
    run_dir = tmp_path / "run"
    _baseline(run_dir, repo, monkeypatch)
    (repo / ".mini-ork" / "runs" / "r1").mkdir(parents=True)
    (repo / ".mini-ork" / "runs" / "r1" / "verdict.json").write_text("{}\n")
    (repo / "a.txt").write_text("one\nCHANGED\n")

    ok, _ = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))

    assert ok is True
    assert ".mini-ork" not in (run_dir / "framework-edit.diff").read_text()


def test_guard_defers_when_it_cannot_read_the_tree(tmp_path):
    """No run dir, or a target that is not a git repo: inapplicable, not a
    failure — the verifiers remain the net for the legacy artifact."""
    repo = _mk_repo(tmp_path)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert ex._harvest_framework_edit_ground_truth("", str(repo)) == (True, "")
    assert ex._harvest_framework_edit_ground_truth(
        str(tmp_path / "run"), str(plain)) == (True, "")


def test_delegate_forwards_so_monkeypatches_stay_observable(monkeypatch):
    """execute_handlers reaches the helper through a delegate, not a direct
    import: a direct import would bind the original function at import time and
    silently ignore a monkeypatch on ``execute`` — the seam the whole suite uses
    to stub this surface."""
    seen = {}

    def fake(run_dir, target):
        seen["args"] = (run_dir, target)
        return (True, "")

    monkeypatch.setattr(ex, "_harvest_framework_edit_ground_truth", fake)

    assert exh._harvest_framework_edit_ground_truth("r", "t") == (True, "")
    assert seen["args"] == ("r", "t")
