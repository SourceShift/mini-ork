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

Third: the target vendors its crate as a submodule too, so archiving the right
repo is still not enough — ``git archive`` writes a gitlink as an EMPTY
directory, and the copy loses the vendored tree the target really has. Both
verifiers now materialise each submodule from the target's own checkout. The
fixture below used to build ``ui/`` with ``mkdir``, which is exactly why the
suite stayed green over the hole; ``_add_submodule`` replaces that.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
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


def _add_submodule(target: Path, name: str, crate: Path) -> None:
    """Register ``crate`` as a REAL submodule of ``target`` at ``name``.

    A path created with ``mkdir`` models the world before the vendoring move and
    cannot fail the way production does: ``git archive`` copies a directory, so a
    fixture built from one never shows the hole a gitlink leaves."""
    _git(target, "-c", "protocol.file.allow=always", "submodule", "add", str(crate), name)
    _git(target, "commit", "-q", "-m", f"vendor {name}")


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


def _vendored(tmp_path: Path) -> tuple[Path, Path, str]:
    """The target vendors ``ui/`` as a REAL submodule — what production looks like
    now. The diff touches a superproject file, because git cuts no diff across a
    gitlink, so "the diff applied" can no longer be the proof that the copy is
    faithful: only the vendored tree can show that."""
    crate = _init(tmp_path / "crate", {"Cargo.toml": "[package]\n", "src/page.rs": "// a\n"})
    target = _init(tmp_path / "target", {"mod.py": "V = 1\n"})
    _add_submodule(target, "ui", crate)
    engine = _init(tmp_path / "engine", {"mod.py": "V = 1\n"})
    return engine, target, _diff(target, "mod.py", "V = 2\n")


def _copied(run_dir: Path, verifier: str, rel: str) -> Path:
    return run_dir / f"verifier-{_name(verifier)}-work" / "repo" / rel


@pytest.mark.parametrize("verifier,cid", CASES)
def test_copy_carries_a_vendored_submodule(tmp_path: Path, verifier: str,
                                           cid: str) -> None:
    """``git archive`` leaves a gitlink as an EMPTY directory, so the copy must
    rebuild the vendored tree or paths the target really has read as absent."""
    engine, target, diff = _vendored(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(diff)
    rows = _run_verifier(run_dir, verifier, engine=engine, target=target)
    assert rows[cid] == "true", rows
    assert _copied(run_dir, verifier, "ui/src/page.rs").read_text() == "// a\n", \
        "the vendored submodule is missing from the copy"


def test_archive_alone_is_what_loses_the_submodule(tmp_path: Path) -> None:
    """Teeth: the raw archive the verifier starts from has no ``ui/src/page.rs``
    at all, so a copy that has one can only have materialised the submodule."""
    _, target, _ = _vendored(tmp_path)
    archive = subprocess.run(["git", "-C", str(target), "archive", "HEAD"],
                             check=True, capture_output=True).stdout
    names = tarfile.open(fileobj=io.BytesIO(archive)).getnames()
    assert "ui/src/page.rs" not in names, names
