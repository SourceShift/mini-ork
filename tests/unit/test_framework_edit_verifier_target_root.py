"""framework-edit verifiers must archive the TARGET repo, not the engine.

Regression for run-1791733111-22332. Both verifiers resolved the repo under test
from ``MINI_ORK_ROOT`` (the ENGINE). On a worktree-first run the engine and the
target worktree are different repos; the engine had converted ``ui/`` to a git
submodule while the target still carried it as a real directory. ``git archive``
renders a submodule gitlink as an EMPTY directory, so ``ui/src/page.rs`` was
absent from the copy, the diff could not apply, and ``patched-copy-created`` /
``diff-applies-to-copy`` failed every round — the run could never pass.

Second half: framework-edit COMMITS the implementer's patch, so a re-verify after
that commit sees a HEAD that already contains the diff. The copy must be archived
from the recorded base (``pre-implementer-ref``), not HEAD, or the diff reads as
'already applied' and fails again.

The verifiers now prefer ``MO_TARGET_CWD`` and archive ``pre-implementer-ref``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIERS = REPO / "recipes" / "framework-edit" / "verifiers"

# the "after" content for ui/src/page.rs (the diff's result)
PATCHED = "// a\n// b\n"

# (verifier file, the check that proves the diff applied inside the copy)
CASES = [
    ("static-check.py", "patched-copy-created"),
    ("test.py", "diff-applies-to-copy"),
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _init(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    for rel, text in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


def _diff(target: Path, rel: str, new_text: str) -> str:
    (target / rel).write_text(new_text)
    diff = _git(target, "diff")
    _git(target, "checkout", "-q", "--", rel)
    return diff


def _name(verifier: str) -> str:
    return "static-check" if verifier == "static-check.py" else "test"


def _run_verifier(run_dir: Path, verifier: str, *, engine: Path,
                  target: Path | None = None, base_ref: str | None = None,
                  cwd: Path | None = None) -> dict[str, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    if base_ref is not None:
        (run_dir / "pre-implementer-ref").write_text(base_ref + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update(MINI_ORK_RUN_DIR=str(run_dir), MINI_ORK_ROOT=str(engine))
    if target is not None:
        env["MO_TARGET_CWD"] = str(target)
    subprocess.run([sys.executable, str(VERIFIERS / verifier)], cwd=str(cwd or target or engine),
                   env=env, capture_output=True, text=True, timeout=180)
    rows = {}
    for line in (run_dir / f"verifier-{_name(verifier)}.checks.tsv").read_text().splitlines():
        cid, _, val = line.split("\t")[:3]
        rows[cid] = val
    return rows


def _diverged(tmp_path: Path) -> tuple[Path, Path, str]:
    """engine LACKS ui/ ; target carries ui/src/page.rs ; diff touches it."""
    engine = _init(tmp_path / "engine", {"mod.py": "V = 1\n"})
    target = _init(tmp_path / "target", {"mod.py": "V = 1\n", "ui/src/page.rs": "// a\n"})
    return engine, target, _diff(target, "ui/src/page.rs", PATCHED)


def _commit_patch(target: Path, rel: str, new_text: str) -> None:
    """Apply new_text to the target and COMMIT it (framework-edit's behavior)."""
    (target / rel).write_text(new_text)
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "patch")


@pytest.mark.parametrize("verifier,cid", CASES)
def test_engine_missing_a_target_path_still_applies_via_target(tmp_path: Path,
                                                               verifier: str, cid: str) -> None:
    """The copy must come from MO_TARGET_CWD (which has ui/), not the engine."""
    engine, target, diff = _diverged(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(diff)
    rows = _run_verifier(run_dir, verifier, engine=engine, target=target)
    assert rows[cid] == "true", rows


def test_without_target_root_the_engine_copy_lacks_the_path(tmp_path: Path) -> None:
    """Teeth: with only the engine root (the old behavior) the path is missing
    and the diff cannot apply — so the target root is what makes the fix work."""
    engine, _, diff = _diverged(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(diff)
    rows = _run_verifier(run_dir, "static-check.py", engine=engine, target=None, cwd=engine)
    assert rows["patched-copy-created"] == "false", rows


@pytest.mark.parametrize("verifier,cid", CASES)
def test_copy_uses_base_ref_when_head_already_carries_the_patch(tmp_path: Path,
                                                               verifier: str, cid: str) -> None:
    """framework-edit commits the patch; the copy must still archive the base."""
    engine, target, diff = _diverged(tmp_path)
    _commit_patch(target, "ui/src/page.rs", PATCHED)  # HEAD now contains the diff
    base = _git(target, "rev-parse", "HEAD~1").strip()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(diff)
    rows = _run_verifier(run_dir, verifier, engine=engine, target=target, base_ref=base)
    assert rows[cid] == "true", rows
