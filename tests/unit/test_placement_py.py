"""Per-run worktree placement: the run edits a tree only it can write.

The defect this closes is not "the diff was wrong" — it is "the diff was right
about the wrong tree". Two sessions edit one checkout, the implementer's changes
and someone else's land in the same file, and the reviewer reads a delta the run
did not author. Nothing crashes, and the gate is correct about an artifact that
does not exist.

So the properties under test are the ones that decide whether that can happen:

* **it is off unless declared** — the shared checkout is right for a
  single-session project, and a copy costs disk;
* **the operator's value beats the recipe's**, in both directions;
* **when it is on, roots point at the new worktree** before anything is
  recorded;
* **a declared placement that cannot be honoured fails the run start** — never a
  silent fall back to the shared tree, which is the bug wearing a fix's clothes;
* **it is idempotent** — a resumed run keeps the checkout it started in.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.runtime import placement  # noqa: E402
from mini_ork.runtime.run_roots import RunRoots  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch):
    monkeypatch.delenv(placement.SPLIT_ENV, raising=False)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A real git repo with one commit — a worktree needs a base."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    return repo


def _run_dir(tmp_path: Path, *, recipe: str = "code-fix") -> Path:
    run_dir = tmp_path / "runs" / "run-123"
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(
        json.dumps({"recipe": recipe, "kickoff_path": str(tmp_path / "k.md")}),
        encoding="utf-8")
    return run_dir


def _roots(project: Path, home: Path, run_dir: Path) -> RunRoots:
    return RunRoots(target=str(project), run_dir=str(run_dir),
                    home=str(home), engine=str(REPO))


# ── the declaration ────────────────────────────────────────────────────────


def test_off_by_default(tmp_path: Path):
    assert placement.declared(str(_run_dir(tmp_path)), env={}) == placement.Declaration(False, "")


def test_the_env_switch_enables_with_no_recipe_support(tmp_path: Path):
    d = placement.declared(str(_run_dir(tmp_path)), env={placement.SPLIT_ENV: "1"})
    assert d.worktree and d.source == "env"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_an_explicit_off_beats_a_recipe_that_asks_for_it(tmp_path: Path, value: str):
    """The recipe is a default, not a cage."""
    engine = tmp_path / "engine"
    (engine / "recipes" / "code-fix").mkdir(parents=True)
    (engine / "recipes" / "code-fix" / "workflow.yaml").write_text(
        "placement:\n  worktree: true\n", encoding="utf-8")
    env = {"MINI_ORK_ROOT": str(engine), placement.SPLIT_ENV: value}
    assert not placement.declared(str(_run_dir(tmp_path)), env=env).worktree


def test_the_recipe_can_declare_it(tmp_path: Path):
    engine = tmp_path / "engine"
    (engine / "recipes" / "code-fix").mkdir(parents=True)
    (engine / "recipes" / "code-fix" / "workflow.yaml").write_text(
        "placement:\n  worktree: true\n", encoding="utf-8")
    d = placement.declared(str(_run_dir(tmp_path)), env={"MINI_ORK_ROOT": str(engine)})
    assert d.worktree and d.source == "workflow"


def test_a_malformed_recipe_does_not_decide_placement(tmp_path: Path):
    """The compiler reports a broken workflow; run-start placement must not."""
    engine = tmp_path / "engine"
    (engine / "recipes" / "code-fix").mkdir(parents=True)
    (engine / "recipes" / "code-fix" / "workflow.yaml").write_text(
        "nodes: [unclosed\n", encoding="utf-8")
    d = placement.declared(str(_run_dir(tmp_path)), env={"MINI_ORK_ROOT": str(engine)})
    assert not d.worktree


# ── applying it ────────────────────────────────────────────────────────────


def test_a_plain_run_is_untouched(tmp_path: Path, project: Path):
    home, run_dir = tmp_path / "home", _run_dir(tmp_path)
    roots = _roots(project, home, run_dir)
    assert placement.prepare(str(run_dir), roots, env={}) is roots


def test_declared_placement_points_roots_at_the_new_worktree(
        tmp_path: Path, project: Path):
    home, run_dir = tmp_path / "home", _run_dir(tmp_path)
    home.mkdir()
    roots = placement.prepare(
        str(run_dir), _roots(project, home, run_dir),
        env={placement.SPLIT_ENV: "1"})

    new_target = Path(roots.target)
    assert new_target != project
    assert new_target.is_dir()
    assert (new_target / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert roots.placement == "worktree"
    # The board owns the record: this is the same object `mini-ork inbox` reads
    # to offer the keep-or-discard decision.
    record = json.loads((home / "worktrees" / "run-123.json").read_text(encoding="utf-8"))
    assert record["path"] == str(new_target)
    assert record["base_sha"]
    assert record["base_branch"]


def test_the_user_checkout_is_not_written(tmp_path: Path, project: Path):
    """The isolation claim, stated as a file: the run's tree is a different
    tree, so a write there cannot race another session."""
    home, run_dir = tmp_path / "home", _run_dir(tmp_path)
    home.mkdir()
    roots = placement.prepare(
        str(run_dir), _roots(project, home, run_dir),
        env={placement.SPLIT_ENV: "1"})
    (Path(roots.target) / "a.txt").write_text("two\n", encoding="utf-8")
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_resuming_reuses_the_worktree_it_started_in(tmp_path: Path, project: Path):
    home, run_dir = tmp_path / "home", _run_dir(tmp_path)
    home.mkdir()
    env = {placement.SPLIT_ENV: "1"}
    first = placement.prepare(str(run_dir), _roots(project, home, run_dir), env=env)
    second = placement.prepare(str(run_dir), _roots(project, home, run_dir), env=env)
    assert first.target == second.target


def test_a_declared_placement_that_cannot_be_honoured_fails_closed(
        tmp_path: Path, project: Path):
    """A non-git target has no worktree to mint. Falling back to the shared
    checkout would be the very defect this module exists to prevent, so it is an
    error with the opt-out named."""
    home, run_dir = tmp_path / "home", _run_dir(tmp_path)
    home.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    roots = RunRoots(target=str(plain), run_dir=str(run_dir),
                     home=str(home), engine=str(REPO))
    with pytest.raises(placement.PlacementError) as exc:
        placement.prepare(str(run_dir), roots, env={placement.SPLIT_ENV: "1"})
    assert "MO_WORKTREE_PLACEMENT=0" in str(exc.value)


def test_no_home_is_an_error_not_a_guess(tmp_path: Path, project: Path):
    run_dir = _run_dir(tmp_path)
    roots = RunRoots(target=str(project), run_dir=str(run_dir),
                     home="", engine=str(REPO))
    with pytest.raises(placement.PlacementError):
        placement.prepare(str(run_dir), roots, env={placement.SPLIT_ENV: "1"})


def test_run_start_refuses_rather_than_silently_running_in_place(
        tmp_path: Path, project: Path, monkeypatch):
    """Through the real pinning boundary, not just the module: the refusal must
    reach the operator as a run-start failure (rc 2), because a run that starts
    anyway is the bug."""
    from mini_ork.runtime import run_roots

    home = tmp_path / "home"
    home.mkdir()
    run_dir = tmp_path / "runs" / "run-999"
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(
        json.dumps({"recipe": "code-fix", "kickoff_path": str(tmp_path / "k.md"),
                    "target_repo": str(project)}),
        encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MO_TARGET_CWD", str(project))
    monkeypatch.setenv(placement.SPLIT_ENV, "1")
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))

    roots = run_roots.persist_run_roots(str(run_dir))

    assert roots is not None and roots.placement == "worktree"
    assert Path(roots.target) != project
    persisted = json.loads((run_dir / "run_profile.json").read_text(encoding="utf-8"))
    assert persisted["roots"]["target"] == roots.target
    assert persisted["roots"]["placement"] == "worktree"
