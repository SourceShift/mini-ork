"""Target-tree sync via git snapshots (remote-nodes-07).

The LOCAL checkout is the truth; the remote tree is a replica synced at
dispatch boundaries (docs/architecture/remote-nodes.md, D1 + "Sync protocol").
Every diff here is computed by git, never by a model.

Pure git plumbing, no HTTP — both the control plane and the node-agent call it.

    snapshot(repo, parent=)          -> Snap    worktree as a commit (temp index;
                                                the user's index/HEAD/stash untouched)
    initial_bundle(repo, snap)       -> (path, mode)   full -> branch -> squashed ladder
    incremental_bundle(repo, new=, base=) -> path
    materialize(remote, bundle, snap, head)  replica worktree := snap.tree
    apply_delta(local, base, new)    -> head_moved      local worktree += (base -> new)

Every bundle carries the named ref ``refs/mo/sync/out`` pointing at its tip, so
a receiver fetches by ref; git refuses to fetch an unadvertised raw SHA.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.tfvars",
    "secrets.local.sh", ".mini-ork/state.db*",
)
OUT_REF = "refs/mo/sync/out"        # tip of every bundle this module writes
LOCAL_REF = "refs/mo/sync/local"    # replica side: the last tip it received
_SYNC_IDENTITY = {
    "GIT_AUTHOR_NAME": "mini-ork-sync", "GIT_AUTHOR_EMAIL": "sync@mini-ork.local",
    "GIT_COMMITTER_NAME": "mini-ork-sync", "GIT_COMMITTER_EMAIL": "sync@mini-ork.local",
}


@dataclass(frozen=True)
class Snap:
    """A worktree captured as a commit: ``tree`` is the exact worktree
    (tracked + untracked non-ignored, minus excludes); ``excluded`` lists the
    NAMES of secret-pattern files left out — never their contents."""

    commit: str
    tree: str
    excluded: tuple[str, ...] = ()


class SyncConflictError(RuntimeError):
    """The local tree changed while a remote node held the replica."""

    def __init__(self, paths: Sequence[str], message: str | None = None) -> None:
        self.paths = tuple(paths)
        super().__init__(message or (
            f"sync conflict: {len(self.paths)} path(s) diverged from base: "
            + ", ".join(self.paths[:10])))


class SyncIntegrityError(RuntimeError):
    """A sync step finished somewhere other than where it had to."""


class SyncTooLargeError(RuntimeError):
    """Even the squashed initial bundle exceeds the cap."""


# --------------------------------------------------------------------------- helpers


def _excludes() -> tuple[str, ...]:
    extra = tuple(p.strip() for p in os.environ.get("MO_REMOTE_SYNC_EXCLUDE", "").split(",")
                  if p.strip())
    return DEFAULT_EXCLUDES + extra


def _bundle_max_bytes() -> int:
    return int(float(os.environ.get("MO_REMOTE_BUNDLE_MAX_MB", "100")) * 1024 * 1024)


def _git(repo: str, *args: str, env: dict | None = None, input: bytes | None = None,
         check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, input=input,
                       env={**os.environ, **(env or {})}, timeout=timeout)
    if check and r.returncode != 0:
        raise SyncIntegrityError(
            f"git {' '.join(args[:3])} failed in {repo}: {r.stderr.decode(errors='replace').strip()}")
    return r


def _out(r: subprocess.CompletedProcess) -> str:
    return r.stdout.decode(errors="replace").strip()


def _exclude_pathspecs(patterns: Sequence[str]) -> list[str]:
    return [f":(exclude,glob)**/{p}" for p in patterns]


def _matches(path: str, patterns: Sequence[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in patterns)


def _write_worktree_tree(repo: str, parent: str, patterns: Sequence[str]) -> str:
    """The tree of the worktree as ``git add -A`` sees it, built in a TEMP index
    seeded from ``parent`` — the user's real index is never touched. Excluded
    paths keep ``parent``'s version when tracked, and are absent otherwise."""
    with tempfile.TemporaryDirectory(prefix="mo-sync-idx-") as td:
        env = {"GIT_INDEX_FILE": os.path.join(td, "index")}
        _git(repo, "read-tree", parent, env=env)
        _git(repo, "add", "-A", "--", ".", *_exclude_pathspecs(patterns), env=env)
        return _out(_git(repo, "write-tree", env=env))


def worktree_tree(repo: str, excludes: Sequence[str] | None = None) -> str:
    """Tree SHA of ``repo``'s current worktree (same rules as :func:`snapshot`)."""
    return _write_worktree_tree(repo, "HEAD", excludes or _excludes())


_current_workdir_tree = worktree_tree  # name the remote backend imports


def _commit_exists(repo: str, rev: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{rev}^{{commit}}", check=False).returncode == 0


# --------------------------------------------------------------------------- snapshot


def snapshot(repo: str, *, parent: str = "HEAD", excludes: Sequence[str] | None = None) -> Snap:
    patterns = tuple(excludes or _excludes())
    parent_sha = _out(_git(repo, "rev-parse", "--verify", f"{parent}^{{commit}}"))
    tree = _write_worktree_tree(repo, parent_sha, patterns)
    commit = _out(_git(repo, "commit-tree", tree, "-p", parent_sha, "-m", "mini-ork sync snapshot",
                       env=_SYNC_IDENTITY))
    candidates = _out(_git(repo, "ls-files", "-com", "--exclude-standard")).splitlines()
    excluded = tuple(sorted({p for p in candidates if p and _matches(p, patterns)}))
    return Snap(commit=commit, tree=tree, excluded=excluded)


# --------------------------------------------------------------------------- bundles


def _bundle_create(repo: str, *rev_args: str, timeout: int = 300) -> Path:
    fd, path = tempfile.mkstemp(prefix="mo-sync-", suffix=".bundle")
    os.close(fd)
    _git(repo, "bundle", "create", path, *rev_args, timeout=timeout)
    return Path(path)


def initial_bundle(repo: str, snap: Snap, *, max_bytes: int | None = None) -> tuple[Path, str]:
    """First upload, smallest-faithful-first ladder (the ``claude --cloud`` one):
    ``full`` (all refs + the snapshot), ``branch`` (the snapshot's ancestry),
    ``squashed`` (an orphan commit of ``snap.tree``). ``OUT_REF`` is left pointing
    at the bundle's tip — for ``squashed`` that is the orphan, not ``snap.commit``.
    """
    cap = _bundle_max_bytes() if max_bytes is None else max_bytes
    sizes: dict[str, int] = {}
    _git(repo, "update-ref", OUT_REF, snap.commit)
    for mode, revs in (("full", ("--all",)), ("branch", (OUT_REF,))):
        path = _bundle_create(repo, *revs)
        sizes[mode] = path.stat().st_size
        if sizes[mode] <= cap:
            return path, mode
        path.unlink(missing_ok=True)
    orphan = _out(_git(repo, "commit-tree", snap.tree, "-m", "mini-ork sync (squashed)",
                       env=_SYNC_IDENTITY))
    _git(repo, "update-ref", OUT_REF, orphan)
    path = _bundle_create(repo, OUT_REF)
    sizes["squashed"] = path.stat().st_size
    if sizes["squashed"] <= cap:
        return path, "squashed"
    path.unlink(missing_ok=True)
    mb = {k: round(v / 1048576, 2) for k, v in sizes.items()}
    raise SyncTooLargeError(
        f"initial bundle exceeds {round(cap / 1048576, 2)} MB in every mode (MB: {mb}); "
        f"raise MO_REMOTE_BUNDLE_MAX_MB or exclude large paths via MO_REMOTE_SYNC_EXCLUDE")


def bundle_tip(repo: str) -> str:
    """The commit the last bundle written in ``repo`` points at."""
    return _out(_git(repo, "rev-parse", OUT_REF))


def incremental_bundle(repo: str, *, new: str, base: str) -> Path | None:
    """Everything reachable from ``new`` and not from ``base`` — or None when
    there is nothing new. Snapshots are deterministic (same tree, parent and
    second give the same commit), so an unchanged tree often yields new == base,
    and git refuses to write an empty bundle."""
    if new == base or _git(repo, "merge-base", "--is-ancestor", new, base,
                           check=False).returncode == 0:
        return None
    _git(repo, "update-ref", OUT_REF, new)
    return _bundle_create(repo, OUT_REF, f"^{base}")


def fetch_bundle(repo: str, bundle: str | os.PathLike[str], ref: str) -> str:
    """Fetch a bundle's tip into ``ref``; return the tip commit."""
    _git(repo, "fetch", "-q", str(bundle), f"+{OUT_REF}:{ref}")
    return _out(_git(repo, "rev-parse", ref))


# --------------------------------------------------------------------------- replica


def materialize(remote_repo: str, bundle: str | os.PathLike[str], snap: Snap, head: str) -> str:
    """Make the replica's worktree exactly ``snap.tree`` with HEAD detached at
    ``head`` (the local HEAD) when the bundle carried it, so ``git status`` in the
    replica shows the same uncommitted changes as the local checkout."""
    remote_repo = str(remote_repo)
    if not Path(remote_repo, ".git").exists():
        Path(remote_repo).mkdir(parents=True, exist_ok=True)
        _git(remote_repo, "init", "-q")
    tip = fetch_bundle(remote_repo, bundle, LOCAL_REF)
    tip_tree = _out(_git(remote_repo, "rev-parse", f"{tip}^{{tree}}"))
    if tip_tree != snap.tree:
        raise SyncIntegrityError(f"bundle tip tree {tip_tree} != snapshot tree {snap.tree}")
    target_head = head if head and _commit_exists(remote_repo, head) else tip
    _git(remote_repo, "checkout", "-q", "--detach", "--force", target_head)
    _git(remote_repo, "read-tree", "--reset", "-u", snap.tree)   # tracked paths := snap
    _git(remote_repo, "clean", "-fdq")                           # stray untracked (not ignored)
    _git(remote_repo, "reset", "-q")                             # index := HEAD again
    got = worktree_tree(remote_repo, ())
    if got != snap.tree:
        raise SyncIntegrityError(f"materialize: worktree tree {got} != snapshot tree {snap.tree}")
    return snap.tree


# --------------------------------------------------------------------------- local


def _parent(repo: str, commit: str) -> str:
    return _out(_git(repo, "rev-parse", f"{commit}^", check=False))


def apply_delta(local_repo: str, base: Snap, new: Snap, *, ref: str | None = None,
                excludes: Sequence[str] | None = None) -> bool:
    """Apply the remote's edits (``base`` -> ``new``) to the local worktree only.

    - local already equals ``new``: a replay; nothing to do.
    - local differs from ``base``: someone edited the checkout meanwhile; raise
      :class:`SyncConflictError` (``new`` is kept at ``ref`` when given) and never
      touch the worktree.
    - otherwise ``git diff --binary base new | git apply``, then verify.

    HEAD and the index are never touched. Returns True when the remote HEAD
    moved (the agent committed) — the tree delta still arrives as uncommitted.
    """
    patterns = tuple(excludes or _excludes())
    if not _commit_exists(local_repo, new.commit):
        raise SyncIntegrityError(f"apply_delta: {new.commit} is not in {local_repo}; fetch the bundle first")
    if ref:
        _git(local_repo, "update-ref", ref, new.commit)
    current = _write_worktree_tree(local_repo, "HEAD", patterns)
    # The replica's HEAD at sync-up was base's parent (the local HEAD) — or base
    # itself for a squashed (parentless) upload. A remote snapshot taken on any
    # other HEAD means the agent committed.
    expected_remote_head = _parent(local_repo, base.commit) or base.commit
    head_moved = _parent(local_repo, new.commit) != expected_remote_head
    if current == new.tree:
        return head_moved
    if current != base.tree:
        paths = _out(_git(local_repo, "diff", "--name-only", base.tree, current)).splitlines()
        raise SyncConflictError(paths)
    diff = _git(local_repo, "diff", "--binary", base.tree, new.tree).stdout
    if diff:
        _git(local_repo, "apply", "--binary", "--whitespace=nowarn", "-", input=diff)
    after = _write_worktree_tree(local_repo, "HEAD", patterns)
    if after != new.tree:
        raise SyncIntegrityError(f"apply_delta: local tree {after} != remote tree {new.tree}")
    return head_moved


# ── replica hygiene (kickoff remote-nodes-11 §4) ─────────────────────────────
#
# After every check exec (verifier / post-run verify / step_rules git /
# mutation test-cmd) the node-agent restores the replica from the
# session's ``last_synced`` snapshot and removes untracked junk. This is
# the verb ``RemoteWorkspace.restore_replica()`` calls into.


def restore_to(repo: str, base_commit: str) -> str:
    """Reset every tracked path to ``base_commit``; leave untracked + ignored
    files alone. Returns the resulting worktree-tree hash.

    ``git read-tree -u --reset base_commit`` is the lightest reset that
    also updates the worktree (``-u``), so the on-disk files match the
    committed tree — exactly what the kickoff means by "restore from the
    snapshot tree". HEAD is not moved (we want the replica to keep
    whichever HEAD the agent left) so a follow-up ``sync_down`` can still
    reason about agent commits.
    """
    if not _commit_exists(repo, base_commit):
        raise SyncIntegrityError(f"restore_to: {base_commit} is not in {repo}")
    _git(repo, "read-tree", "-u", "--reset", base_commit)
    return _write_worktree_tree(repo, "HEAD", _excludes())


def git_clean_unignored(repo: str) -> None:
    """``git clean -fd`` (no ``-x``): remove untracked + ignored-but-listed
    files EXCEPT for the project-standard ignore list (``_excludes()``),
    so caches like ``.venv``, ``node_modules``, and ``.pytest_cache``
    survive for speed.

    The kickoff is explicit about why the ``-x`` flag is wrong here:
    an ignored cache is part of the project's runtime surface and
    rebuilding it every restore costs a sync cycle nobody paid for.
    """
    patterns = _excludes()
    pathspecs = _exclude_pathspecs(patterns)
    # ``git clean -fd -- <pathspec>...`` only removes files matching the
    # pathspecs; ``-d`` prunes empty untracked directories so a verifier
    # that created ``foo/`` and only ``foo/`` does not leave an empty
    # dir that survives into the next sync-down.
    _git(repo, "clean", "-fd", "--", *pathspecs)
