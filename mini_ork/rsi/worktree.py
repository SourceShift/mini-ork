"""The git surface of a cycle — claim, create, apply, merge, quarantine, revert.

This module owns every place a cycle touches git, and it owns them as a small
number of named operations rather than as shell strings scattered through
``cycle``. Four rules are load-bearing:

* **It never evaluates a gate.** By the time anything here merges, ``cycle`` has
  already turned eleven greens into a decision. ``worktree`` is told to merge; it
  does not decide whether to.
* **Only ``land_patch`` writes the tree.** The child's diff is applied through
  ``publisher_authored_patch.land_patch`` — which refuses frozen paths *before*
  any git write, uses a private index and a compare-and-swap on the branch ref.
  Shelling out to ``git apply`` here would skip all three, so this module does not
  contain a ``git apply`` or a ``git add -A`` at all.
* **The merge is the script's job.** ``scripts/mini_ork_worktree.py merge`` owns
  the OSS guard, the green gate, the mkdir lock and the bounded rebase→push
  retry. Re-implementing any of that here would create a second, weaker merge
  path whose bugs would only appear under concurrency.
* **Nothing is ever destroyed to recover.** A quarantine keeps the worktree, the
  branch and the claims. A revert is a *new commit* merged through the same path.
  ``reset --hard`` appears nowhere in this file.

The one subtle outcome is :attr:`MergeResult.diverged`. The script pushes
``HEAD:main`` inside its own lock, so by the time it returns 0 the commit **is on
``origin/main``**; what can still fail is the *local* checkout's fast-forward.
That is a stale laptop, not a bad change, and it must not be reported as a
rejection — the ledger would then quarantine a commit that already shipped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mini_ork.cli.publisher_authored_patch import Abstain, land_patch

__all__ = [
    "MERGE_STATUSES",
    "MergeResult",
    "Worktree",
    "WorktreeConfig",
    "WorktreeError",
    "apply_patch",
    "assert_worktree_root",
    "claim_and_create",
    "clean",
    "merge",
    "revert",
    "safe_slug",
    "subprocess_runner",
    "write_quarantine",
]

#: ``merged``   — pushed, and the local checkout fast-forwarded.
#: ``diverged`` — pushed, but the local checkout could not fast-forward. Still landed.
#: ``rejected`` — never reached ``origin/main``.
MERGE_STATUSES: tuple[str, ...] = ("merged", "diverged", "rejected")

Runner = Callable[..., "tuple[int, str, str]"]


class WorktreeError(RuntimeError):
    """A git-level failure with no decision attached — the cycle records ``error``."""


def subprocess_runner(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: int | None = None,
) -> tuple[int, str, str]:
    """Default runner: argv only, never a shell, output captured as text."""
    proc = subprocess.run(  # noqa: S603 - argv is built here, never interpolated
        list(argv),
        cwd=cwd,
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# --------------------------------------------------------------------------
# Slugs. Mirrored from scripts/mini_ork_worktree.py:sanitize_slug for the same
# reason select.paths_overlap is mirrored: importing scripts/ into the package
# is the wrong layering, and a test pins the two to agreement.
# --------------------------------------------------------------------------

_SLUG_BAD = re.compile(r"[^A-Za-z0-9._-]")


def safe_slug(slug: str) -> str:
    """The script's slug rule, plus a refusal of anything that walks a path.

    The script's own rule strips separators, so ``a/b`` becomes ``a-b``. That is
    safe by construction, but this function *refuses* ``.``/``..`` instead of
    rewriting them: a slug is used to build ``WORKTREES_DIR/<slug>`` on our side
    before the script ever sees it, and silently rewriting a path component is
    how two different slugs end up sharing a directory.
    """
    text = str(slug or "")
    if not text:
        raise WorktreeError("slug is empty")
    if text in (".", "..") or ".." in text.split("/"):
        raise WorktreeError(f"slug may not walk a path: {slug!r}")
    text = _SLUG_BAD.sub("-", text)
    if text.startswith("-"):
        text = text[1:]
    if text.endswith("-"):
        text = text[:-1]
    if not text:
        raise WorktreeError(f"slug has no alphanumeric character: {slug!r}")
    return text


@dataclass(frozen=True, slots=True)
class WorktreeConfig:
    """Where a cycle's branch lives and how it is allowed to land.

    ``test_cmd`` is required. ``merge_worktree``'s default green gate is the full
    suite (~an hour here), so a merge that forgot to scope it would either hang
    the loop or be run with ``--no-verify`` by a frustrated operator. Refusing a
    blank command here makes "merge without a gate" unrepresentable.
    """

    slug: str
    files: tuple[str, ...]
    worktrees_dir: Path
    primary: Path
    test_cmd: str
    branch: str = ""
    require_remote: bool = True
    clean_after_merge: bool = True
    timeout_s: int | None = None

    def __post_init__(self) -> None:
        if not self.files:
            raise ValueError("a cycle with no claimed files has no scope to merge")
        if not str(self.test_cmd).strip():
            raise ValueError(
                "test_cmd is required: an unscoped merge would run the full suite")
        if not str(self.worktrees_dir).strip() or not str(self.primary).strip():
            raise ValueError("worktrees_dir and primary are required")
        # Validate the slug without rewriting it: a caller that asked for one
        # worktree must not silently get a different path. A slug that needs
        # rewriting is a config error, so it surfaces as ValueError here rather
        # than as WorktreeError from the guard primitive.
        try:
            safe = safe_slug(self.slug)
        except WorktreeError as exc:
            raise ValueError(f"unusable slug {self.slug!r}: {exc}") from exc
        if safe != self.slug:
            raise ValueError(f"slug is not already safe: {self.slug!r}")

    @property
    def branch_name(self) -> str:
        return self.branch or f"wt/{self.slug}"

    @property
    def path(self) -> Path:
        """Mirrors ``WORKTREES_DIR/<sanitized slug>`` in the script."""
        return Path(self.worktrees_dir) / self.slug

    @property
    def script(self) -> Path:
        return Path(self.primary) / "scripts" / "mini_ork_worktree.py"


@dataclass(frozen=True, slots=True)
class Worktree:
    slug: str
    path: Path
    branch: str


@dataclass(frozen=True, slots=True)
class MergeResult:
    status: str
    sha: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in MERGE_STATUSES:
            raise ValueError(f"unknown merge status {self.status!r}")

    @property
    def landed(self) -> bool:
        """Did the commit reach ``origin/main``? True for ``diverged`` too."""
        return self.status in ("merged", "diverged")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _tail(text: str, lines: int = 12) -> str:
    rows = [r for r in (text or "").strip().splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def _run_script_at(script: Path, primary: Path, args: Sequence[str], *,
                   runner: Runner, env: Mapping[str, str] | None = None,
                   timeout_s: int | None = None) -> tuple[int, str, str]:
    if not script.is_file():
        raise WorktreeError(f"worktree script not found: {script}")
    return runner([sys.executable, str(script), *args],
                  cwd=str(primary),
                  env={**os.environ, **(env or {})},
                  timeout_s=timeout_s)


def _run_script(cfg: WorktreeConfig, args: Sequence[str], *,
                runner: Runner, env: Mapping[str, str] | None = None,
                ) -> tuple[int, str, str]:
    return _run_script_at(cfg.script, cfg.primary, args, runner=runner,
                          env=env, timeout_s=cfg.timeout_s)


def _git(cfg: WorktreeConfig, args: Sequence[str], *,
         runner: Runner, cwd: Path | None = None) -> tuple[int, str, str]:
    return runner(["git", "-C", str(cwd or cfg.primary), *args],
                  cwd=None, env=None, timeout_s=cfg.timeout_s)


def assert_worktree_root(wt: Worktree, *, runner: Runner) -> Path:
    """Prove a command is about to run *inside* the worktree, not the engine tree.

    Comparing the two paths naively fails on macOS, where ``/var`` and
    ``/private/var`` name the same directory and ``git`` reports one or the
    other. ``realpath`` on both sides is what makes this check about identity
    rather than about spelling.
    """
    rc, out, err = runner(["git", "-C", str(wt.path), "rev-parse", "--show-toplevel"],
                          cwd=None, env=None, timeout_s=None)
    top = out.strip()
    if rc != 0 or not top:
        raise WorktreeError(f"cannot resolve the worktree root of {wt.path}: {_tail(err)}")
    if os.path.realpath(top) != os.path.realpath(str(wt.path)):
        raise WorktreeError(
            f"command would run outside the worktree: toplevel={top!r} "
            f"worktree={str(wt.path)!r}")
    return Path(top)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def claim_and_create(cfg: WorktreeConfig, *, runner: Runner = subprocess_runner) -> Worktree:
    """Create the cycle's worktree through the script, claiming its files.

    The script sets ``ALLOW_WORKTREE_BRANCH_CREATE`` itself and writes the CAID
    registry, so this must not call ``mini_ork.workspaces.create`` — that omits
    the env and is rejected by the ``reference-transaction`` guard.
    """
    argv = ["create", cfg.slug]
    for path in cfg.files:
        argv.extend(("--owns", path))
    rc, out, err = _run_script(cfg, argv, runner=runner)
    if rc != 0:
        raise WorktreeError(
            f"worktree create failed for {cfg.slug!r}: {_tail(err) or _tail(out)}")
    wt = Worktree(slug=cfg.slug, path=cfg.path, branch=cfg.branch_name)
    assert_worktree_root(wt, runner=runner)
    return wt


def apply_patch(wt: Worktree, patch_text: str, *,
                message: str = "",
                runner: Runner | None = None) -> str | Abstain:
    """Land the child's diff on the worktree's branch via ``land_patch``.

    ``runner`` is accepted for symmetry with the rest of the module and ignored:
    ``land_patch`` owns its own git calls, and letting a test substitute them
    would mean testing a merge path that does not exist in production.
    """
    del runner
    return land_patch(str(wt.path), patch_text, "", message=message)


def merge(cfg: WorktreeConfig, *,
          runner: Runner = subprocess_runner) -> MergeResult:
    """Merge the cycle's branch to ``origin/main``, then refresh the primary.

    Order matters: the script pushes inside its lock (so a peer pushing during
    the green gate cannot lose the race), and only then is the local primary
    fast-forwarded. A ``--ff-only`` refusal means the local primary diverged —
    the change **landed**, so the result is ``diverged``, never ``rejected``.
    """
    if cfg.require_remote:
        rc, out, err = _git(cfg, ["remote", "get-url", "origin"], runner=runner)
        if rc != 0 or not out.strip():
            return MergeResult("rejected", detail=(
                "merge.require_remote is true and the repository has no 'origin': "
                + (_tail(err) or "git remote get-url origin returned nothing")))

    rc, out, err = _run_script(
        cfg, ["merge", cfg.slug], runner=runner,
        env={"MINI_ORK_TEST_CMD": cfg.test_cmd},
    )
    if rc != 0:
        return MergeResult("rejected", detail=_tail(err) or _tail(out))

    rc, sha, sha_err = _git(cfg, ["rev-parse", "HEAD"], runner=runner, cwd=cfg.path)
    sha = sha.strip()
    if rc != 0 or not sha:
        # The push succeeded (rc == 0 above) but we cannot name the commit. That
        # is worse than a rejection: quarantine, because the ledger must not
        # record "merged" with no sha to revert.
        return MergeResult("rejected", detail=(
            "merge reported success but HEAD could not be resolved: " + _tail(sha_err)))

    rc, _, ferr = _git(cfg, ["fetch", "origin", "main"], runner=runner)
    if rc != 0:
        return MergeResult("diverged", sha=sha, detail=(
            "merged and pushed, but the local primary could not fetch origin/main: "
            + _tail(ferr)))
    rc, _, merr = _git(cfg, ["merge", "--ff-only", "origin/main"], runner=runner)
    if rc != 0:
        return MergeResult("diverged", sha=sha, detail=(
            "merged and pushed, but the local primary could not fast-forward: "
            + _tail(merr)
            + " — reconcile by hand; never 'git reset --hard' main"))

    if cfg.clean_after_merge:
        try:
            clean(cfg, runner=runner)
        except WorktreeError:
            # The merge landed. A failed teardown is operator housekeeping, not a
            # reason to misreport the outcome; `rsi-cycle doctor` surfaces it.
            pass
    return MergeResult("merged", sha=sha)


def clean(cfg: WorktreeConfig, *, runner: Runner = subprocess_runner) -> None:
    """Remove the worktree and release its claims. Fails on a dirty tree."""
    rc, out, err = _run_script(cfg, ["clean", cfg.slug], runner=runner)
    if rc != 0:
        raise WorktreeError(
            f"worktree clean failed for {cfg.slug!r}: {_tail(err) or _tail(out)}")


def release(primary: Path, slug: str, *, runner: Runner = subprocess_runner,
            timeout_s: int | None = None) -> None:
    """Drop a cycle's file claims, keeping its worktree and branch.

    The claims — not the branch — are what a later cycle collides with: while
    they are live, the next attempt at the same gap rejects with
    ``worktree_claim_conflict`` after paying for SUGGEST. So this is the verb an
    operator means by "release": keep the diff for review, hand the files back
    to the loop.

    It takes the primary and a slug rather than a ``WorktreeConfig`` on purpose.
    That config *requires* a merge scope, and a release has no scope: a cycle
    quarantined before its candidate recorded any files still has claims to
    drop, and demanding a scope here would turn a cleanup into a ValueError.
    """
    script = Path(primary) / "scripts" / "mini_ork_worktree.py"
    rc, out, err = _run_script_at(script, Path(primary), ["release", slug],
                                  runner=runner, timeout_s=timeout_s)
    if rc != 0:
        raise WorktreeError(
            f"worktree release failed for {slug!r}: {_tail(err) or _tail(out)}")


def write_quarantine(record: Mapping[str, Any], *,
                     record_dir: Path, cycle_id: str) -> Path:
    """Persist why a cycle did not merge. Written atomically, never inside the tree.

    The record lives in the primary's ``.mini-ork/rsi/quarantine/`` rather than
    the worktree: a file written into the worktree would make it dirty, and a
    dirty worktree cannot be merged or cleaned later without deleting evidence
    the operator may still want.
    """
    target = Path(record_dir) / f"{safe_slug(cycle_id)}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(dict(record), indent=2, sort_keys=True, default=str) + "\n"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, target)
    return target


def revert(cfg: WorktreeConfig, merged_sha: str, *,
           runner: Runner = subprocess_runner) -> MergeResult:
    """Undo a merged cycle with a *new revert commit*, gated like any other merge.

    A conflicted revert is a rejection that leaves its worktree in place: the
    conflict markers are the operator's only map of what changed since, and
    deleting them to tidy up would throw that away.
    """
    if not merged_sha.strip():
        return MergeResult("rejected", detail="revert needs the sha that was merged")
    slug = safe_slug(f"revert-{cfg.slug}")[:64]
    rev = WorktreeConfig(
        slug=slug,
        files=cfg.files,
        worktrees_dir=cfg.worktrees_dir,
        primary=cfg.primary,
        test_cmd=cfg.test_cmd,
        require_remote=cfg.require_remote,
        clean_after_merge=cfg.clean_after_merge,
        timeout_s=cfg.timeout_s,
    )
    wt = claim_and_create(rev, runner=runner)
    rc, out, err = _git(cfg, ["revert", "--no-edit", merged_sha],
                        runner=runner, cwd=wt.path)
    if rc != 0:
        return MergeResult("rejected", detail=(
            f"revert of {merged_sha} conflicted in {wt.path}; the worktree is kept "
            "so the conflict can be resolved by hand: " + (_tail(err) or _tail(out))))
    return merge(rev, runner=runner)
