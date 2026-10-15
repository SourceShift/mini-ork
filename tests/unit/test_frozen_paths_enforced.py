"""Frozen-path enforcement at the publish choke point.

An authored patch that touches a frozen path must be **refused** — not silently
filtered — before it is applied or published, and the refusal must hold on every
run: the in-place publisher's ``land_patch`` is the single choke point every
publish flows through, so a check there is origin-agnostic by construction (a
plain call carries no ``origin=auto-triage`` marker and is still refused).

The frozen list is the target's ``config/kernel-frozen.yaml`` UNIONED with the
engine's built-in floor, so a diff cannot unfreeze a path by editing or deleting
the list and a missing/unparsable file still refuses a floor path. The
operator's single override token is ``MO_ALLOW_FROZEN_EDIT=1`` (fail closed:
unset/empty/any other value refuses).

Hermetic: a throwaway git repo per test, driven through the real ``land_patch``;
patches are real ``git diff`` output, so a refusal can only come from the frozen
check, never from a broken patch. The working tree is restored after each patch
is cut, so a refusal must leave ``HEAD``, the index and the tree untouched.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from mini_ork.cli.publisher_authored_patch import Abstain, land_patch
from mini_ork.vcs import repo_integrity_guard

GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")

FIXTURE_REL = "recipes/code-fix/probes/fixtures/probe-1/tally.py"
ORDINARY_REL = "docs/note.txt"
# A FROZEN_PATHS_DEFAULT entry: the fail-closed floor must refuse it even with
# no kernel-frozen.yaml present to list it.
FLOOR_REL = "mini_ork/vcs/repo_integrity_guard.py"

FIXTURE_BASE = "TALLY_BASE = 1\n"
FIXTURE_EDITED = "TALLY_BASE = 2\n"
NOTE_BASE = "ordinary note\n"
NOTE_EDITED = "ordinary note, edited\n"
FLOOR_BASE = "# placeholder\n"
FLOOR_EDITED = "# placeholder\nedited = True\n"

# A minimal kernel-frozen document declaring only the fixtures glob.
FIXTURE_ONLY_YAML = "version: 1\npaths:\n  - recipes/code-fix/probes/fixtures/**\n"
# A document that OMITS the floor entry — the shape a diff that shrinks the list
# would produce. The union must still refuse FLOOR_REL.
GUTTED_YAML = "version: 1\npaths:\n  - config/kernel-frozen.yaml\n"


# ── harness ─────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in the throwaway repo with the index/dir env sanitized, so an
    ambient lane value can never make an assertion read a different tree than
    ``land_patch`` did."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")}
    proc = subprocess.run(["git", *GIT_IDENTITY, *args], cwd=repo, env=env,
                          capture_output=True, text=True, check=check, timeout=60)
    return proc.stdout


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_repo(tmp_path: Path, frozen_yaml: str | None = FIXTURE_ONLY_YAML) -> Path:
    """A throwaway git repo holding the files the tests' patches touch.

    ``frozen_yaml=None`` leaves ``config/kernel-frozen.yaml`` absent (the
    fail-closed case); a garbage string writes an unparsable document."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, FIXTURE_REL, FIXTURE_BASE)
    _write(repo, ORDINARY_REL, NOTE_BASE)
    _write(repo, FLOOR_REL, FLOOR_BASE)
    if frozen_yaml is not None:
        _write(repo, "config/kernel-frozen.yaml", frozen_yaml)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _patch(repo: Path, edits: dict[str, tuple[str, str]]) -> str:
    """Real ``git diff`` output applying each ``rel: (old, new)`` edit.

    The tree is restored afterwards, so the repo is clean and the patch is
    exactly what a run hands the publisher."""
    for rel, (_old, new) in edits.items():
        _write(repo, rel, new)
    diff = _git(repo, "diff", "--", *edits)
    for rel, (old, _new) in edits.items():
        _write(repo, rel, old)
    assert diff.strip(), "harness bug: generated an empty patch"
    return diff


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _commits(repo: Path) -> list[str]:
    return _git(repo, "log", "--format=%H").split()


def _staged_diff(repo: Path) -> str:
    """Real ``git diff --cached -M -C --find-copies-harder`` against HEAD.

    Staged, because that is the only diff in which git emits RENAME and
    mode-only sections — and of those two shapes at least the pure rename
    (``similarity index 100%``) carries no ``---``/``+++`` blob headers at all.
    The repo is left as it was (the mutation stays; only the patch is cut), so a
    test can assert the refusal did not touch the index or the tree either.
    """
    _git(repo, "add", "-A")
    return _git(repo, "diff", "--cached", "-M", "-C", "--find-copies-harder")


@pytest.fixture(autouse=True)
def _no_override_token(monkeypatch):
    """Start every test from the documented default (refuse)."""
    monkeypatch.delenv("MO_ALLOW_FROZEN_EDIT", raising=False)


# ── refusal at the choke point ──────────────────────────────────────────────


def test_frozen_fixture_patch_refused_before_apply(tmp_path):
    repo = _make_repo(tmp_path)
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})
    base = _head(repo)

    result = land_patch(str(repo), patch, "")

    assert isinstance(result, Abstain), result
    assert result.reason == "publish-frozen-path"
    assert FIXTURE_REL in result.detail
    assert "frozen" in result.detail.lower()
    # Refused BEFORE any git write: nothing committed, nothing staged, the
    # working tree is exactly as the harness left it.
    assert _head(repo) == base
    assert _commits(repo) == [base]
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert (repo / FIXTURE_REL).read_text() == FIXTURE_BASE


def test_frozen_and_ordinary_mixed_patch_refused_whole(tmp_path):
    repo = _make_repo(tmp_path)
    patch = _patch(repo, {
        ORDINARY_REL: (NOTE_BASE, NOTE_EDITED),
        FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED),
    })
    base = _head(repo)

    result = land_patch(str(repo), patch, "")

    assert isinstance(result, Abstain), result
    assert result.reason == "publish-frozen-path"
    assert FIXTURE_REL in result.detail
    # Refused whole, not filtered: the ordinary section must not be published
    # while the frozen one is dropped.
    assert _head(repo) == base
    assert _commits(repo) == [base]


def test_ordinary_patch_still_lands(tmp_path):
    repo = _make_repo(tmp_path)
    patch = _patch(repo, {ORDINARY_REL: (NOTE_BASE, NOTE_EDITED)})
    base = _head(repo)

    sha = land_patch(str(repo), patch, "")

    assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha), sha
    assert _head(repo) == sha
    assert sha != base
    names = _git(repo, "show", "--name-only", "--format=", sha).splitlines()
    assert ORDINARY_REL in names


# ── patch shapes with no blob headers (rename / mode-only) ──────────────────


def test_rename_away_from_a_frozen_path_refused_on_the_old_side(tmp_path):
    """A rename is a write to the path it leaves, not only the one it writes.

    A pure rename section (``similarity index 100%``) has no ``---``/``+++``
    lines: reading only the blob headers made the section name no path at all,
    so renaming a fixture away — deleting the measurement floor while keeping
    its bytes — read as "touches nothing frozen".
    """
    repo = _make_repo(tmp_path)
    moved = "recipes/code-fix/probes/fixtures/probe-1/tally_renamed.py"
    (repo / FIXTURE_REL).rename(repo / moved)
    patch = _staged_diff(repo)
    assert f"rename from {FIXTURE_REL}" in patch, "harness: not a rename section"
    assert f"--- a/{FIXTURE_REL}" not in patch, "harness: rename has blob headers"
    base, index_before = _head(repo), _git(repo, "status", "--porcelain")

    result = land_patch(str(repo), patch, "")

    assert isinstance(result, Abstain), result
    assert result.reason == "publish-frozen-path"
    assert FIXTURE_REL in result.detail, result.detail
    assert _head(repo) == base
    assert _commits(repo) == [base]
    # The refusal wrote nothing: the staged rename and the tree are untouched.
    assert _git(repo, "status", "--porcelain") == index_before
    assert (repo / moved).read_text() == FIXTURE_BASE


def test_mode_only_change_on_a_frozen_path_refused(tmp_path):
    """A mode-only section has neither blob headers nor blob content."""
    repo = _make_repo(tmp_path)
    (repo / FIXTURE_REL).chmod(0o755)
    patch = _staged_diff(repo)
    assert "new mode 100755" in patch, "harness: not a mode-only section"
    base = _head(repo)

    result = land_patch(str(repo), patch, "")

    assert isinstance(result, Abstain), result
    assert result.reason == "publish-frozen-path"
    assert FIXTURE_REL in result.detail
    assert _head(repo) == base


def test_rename_of_an_ordinary_file_still_lands(tmp_path):
    """The wider path scan must not turn an ordinary rename into a refusal."""
    repo = _make_repo(tmp_path)
    moved = "docs/note-renamed.txt"
    (repo / ORDINARY_REL).rename(repo / moved)
    patch = _staged_diff(repo)
    assert f"rename from {ORDINARY_REL}" in patch, "harness: not a rename section"

    sha = land_patch(str(repo), patch, "")

    assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha), sha
    names = _git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    assert moved in names and ORDINARY_REL not in names


# ── override token (fail closed by default) ─────────────────────────────────


def test_override_token_allows_frozen_edit(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})
    base = _head(repo)

    monkeypatch.setenv("MO_ALLOW_FROZEN_EDIT", "1")
    sha = land_patch(str(repo), patch, "")

    assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha), sha
    assert _head(repo) == sha
    assert sha != base
    assert _git(repo, "show", f"{sha}:{FIXTURE_REL}") == FIXTURE_EDITED


def test_override_token_only_accepts_the_value_one(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})
    base = _head(repo)

    for value in ("true", "yes", "on", "0", ""):
        monkeypatch.setenv("MO_ALLOW_FROZEN_EDIT", value)
        result = land_patch(str(repo), patch, "")
        assert isinstance(result, Abstain), (value, result)
        assert result.reason == "publish-frozen-path"

    assert _head(repo) == base


def test_override_token_is_documented_and_default_refuse():
    doc = repo_integrity_guard.__doc__ or ""
    assert "MO_ALLOW_FROZEN_EDIT" in doc, "the override token must be documented"
    assert "frozen" in doc.lower()
    # Documented default is refuse — no token in the environment here (the
    # autouse fixture scrubs it).
    assert repo_integrity_guard.frozen_edit_allowed() is False


# ── fail-closed fallback (missing / unparsable / gutted file) ───────────────


def test_missing_kernel_frozen_yaml_fallback_still_refuses_floor_path(tmp_path):
    repo = _make_repo(tmp_path, frozen_yaml=None)
    assert not (repo / "config" / "kernel-frozen.yaml").exists()
    assert FLOOR_REL in repo_integrity_guard.FROZEN_PATHS_DEFAULT
    patch = _patch(repo, {FLOOR_REL: (FLOOR_BASE, FLOOR_EDITED)})
    base = _head(repo)

    result = land_patch(str(repo), patch, "")

    assert isinstance(result, Abstain), result
    assert result.reason == "publish-frozen-path"
    assert FLOOR_REL in result.detail
    assert "frozen" in result.detail.lower()
    assert _head(repo) == base


def test_unparsable_kernel_frozen_yaml_fallback_still_refuses_floor_path(tmp_path):
    repo = _make_repo(tmp_path, frozen_yaml="::: definitely not: [a yaml mapping\n")
    patch = _patch(repo, {FLOOR_REL: (FLOOR_BASE, FLOOR_EDITED)})
    base = _head(repo)

    result = land_patch(str(repo), patch, "")

    assert isinstance(result, Abstain), result
    assert result.reason == "publish-frozen-path"
    assert _head(repo) == base


def test_gutted_yaml_fallback_still_refuses_a_dropped_floor_entry(tmp_path):
    # The file parses but no longer names the floor path: the union must still
    # refuse it, so a diff cannot unfreeze a path by shrinking the list.
    repo = _make_repo(tmp_path, frozen_yaml=GUTTED_YAML)
    patch = _patch(repo, {FLOOR_REL: (FLOOR_BASE, FLOOR_EDITED)})
    base = _head(repo)

    result = land_patch(str(repo), patch, "")

    assert isinstance(result, Abstain), result
    assert result.reason == "publish-frozen-path"
    assert FLOOR_REL in result.detail
    assert _head(repo) == base


# ── the union helper ────────────────────────────────────────────────────────


def test_enforced_paths_is_the_union_of_file_and_floor(tmp_path):
    root = tmp_path / "cfg"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kernel-frozen.yaml").write_text("paths:\n  - a/b.py\n")

    got = repo_integrity_guard.enforced_frozen_paths(str(root))

    assert got[:1] == ["a/b.py"]
    assert got.count("a/b.py") == 1
    assert set(repo_integrity_guard.FROZEN_PATHS_DEFAULT) <= set(got)


def test_enforced_paths_missing_file_is_the_floor(tmp_path):
    got = repo_integrity_guard.enforced_frozen_paths(str(tmp_path / "gone"))
    assert set(got) == set(repo_integrity_guard.FROZEN_PATHS_DEFAULT)
