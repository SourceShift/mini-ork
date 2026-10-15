"""Per-node git checkpoints for a run's worktree (kickoff hr-node-ckpt-refs).

Pure git, no DB. Three verbs:

    snapshot(worktree, run_id, node_id, *, overwrite=True) -> str | None
    list_checkpoints(worktree, run_id)                       -> list[dict]
    restore(worktree, run_id, node_id, *, home=None, force=False) -> dict

Snapshotting is invisible to the user's git state: a temp ``GIT_INDEX_FILE``
isolates the read-tree / add -A / write-tree / commit-tree / update-ref chain,
so HEAD, the real index and the working tree are never touched. The captured
tree INCLUDES untracked, non-ignored files (so a node that just wrote an
untracked artefact is snapshotted with it) and EXCLUDES anything matched by
``git``'s own ``.gitignore`` rules (so a build artefact or a fixture should
not leak in).

Ref layout: ``refs/mini-ork/ckpt/<run_id>/<node>``. The run id and the node
id are both sanitised to a git ref-component character class
``[A-Za-z0-9._-]``, then folded through the ``git check-ref-format`` rules
that the class alone does not cover (a leading ``.`` is renamed, ``..`` is
collapsed, a ``.lock`` suffix and a trailing ``.`` are renamed, an empty
string becomes ``_``). Both ``_start`` (the run's pre-begin snapshot) and
``_pre-restore-<ts>`` (the safety ref a restore leaves behind) are reserved
names — only ``_start`` carries semantic meaning; the ``_pre-restore-*`` set
is what the run directory's restore can fall back onto.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Match a single character that is NOT a valid ref-component character
# (git's ref-format allowlist; same as the kickoff).
_INVALID_REF_CHAR = re.compile(r"[^A-Za-z0-9._-]")

# Identity used by commit-tree when neither GIT_AUTHOR_* nor
# GIT_COMMITTER_* is set. Some cloud / CI nodes ship with no user.email
# and a dot-less hostname; without a committer, commit-tree fails and
# every snapshot silently returns None.
_DEFAULT_IDENTITY = {
    "GIT_AUTHOR_NAME": "mini-ork",
    "GIT_AUTHOR_EMAIL": "mini-ork@local",
    "GIT_COMMITTER_NAME": "mini-ork",
    "GIT_COMMITTER_EMAIL": "mini-ork@local",
}

# The names that the kickoff reserves for the run-local control refs.
# ``_start`` is the pre-run baseline; ``_pre-restore-*`` is the
# per-restore safety snapshot.
_RESERVED_PREFIX = "refs/mini-ork/ckpt"

# Per-call timeout for every git subprocess. 120 s matches
# ``workspaces._git`` and is long enough that an ``index.lock`` or a
# stalled FS will surface as a real error, not a hang that hides a
# later failure inside the dispatch seam.
_GIT_TIMEOUT_S = 120

# Path-relative, non-empty case where the snapshot just produces a sha —
# the kickoff does not require files outside the worktree, so an empty
# worktree (a fresh init with no commits) is the only path that gets the
# None-no-raise behaviour. Pre-`git init` directories also return None.
_NOT_GIT_MARKERS = (".git",)


# --------------------------------------------------------------------------- sanitise


def _sanitize(component: str) -> str:
    """Return a string that survives ``git check-ref-format`` for a single
    ref component. Applied to BOTH run_id and node_id; the kickoff names
    the rule for the node, but the ref is ``<run_id>/<node>`` and the
    run id is operator-supplied just as often."""
    if component is None:
        return "_"
    out = _INVALID_REF_CHAR.sub("_", str(component))
    # git check-ref-format rules the class alone doesn't catch:
    #   - cannot begin with '.'
    #   - cannot contain '..'
    #   - cannot end with '.lock'
    #   - cannot end with '.'
    #   - cannot be empty
    if out.startswith("."):
        out = "_" + out[1:]
    while ".." in out:
        out = out.replace("..", "_.")
    if out.endswith(".lock"):
        out = out[:-5] + "_lock"
    if out.endswith("."):
        out = out[:-1] + "_"
    if not out:
        out = "_"
    return out


# --------------------------------------------------------------------------- git subprocess


def _git(
    repo: str | os.PathLike,
    *args: str,
    env_extra: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """A subprocess.run with a CLEAN git env. Leaked ``GIT_DIR`` /
    ``GIT_INDEX_FILE`` / etc. (an outer hook, a pytest test) would
    redirect ``git -C <worktree>`` away from the worktree's own
    repository, so the env we hand to git is os.environ minus the
    location vars plus ``env_extra`` (and the identity defaults if the
    operator didn't set them)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                                 "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                                 "GIT_PREFIX"))}
    env.setdefault("GIT_AUTHOR_NAME", _DEFAULT_IDENTITY["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_AUTHOR_EMAIL", _DEFAULT_IDENTITY["GIT_AUTHOR_EMAIL"])
    env.setdefault("GIT_COMMITTER_NAME", _DEFAULT_IDENTITY["GIT_COMMITTER_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", _DEFAULT_IDENTITY["GIT_COMMITTER_EMAIL"])
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
        timeout=_GIT_TIMEOUT_S,
        env=env,
    )


def _git_out(repo: str | os.PathLike, *args: str) -> str | None:
    """A convenience: run git, return stdout stripped, or None on non-zero."""
    r = _git(repo, *args)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _is_git_repo(path: Path) -> bool:
    """True when ``path`` sits inside a git working tree.

    ``git rev-parse --git-dir`` succeeds in a linked worktree AND in the
    main checkout, so it is the cheapest reliable probe. A bare repo
    without a worktree is NOT a node-checkpoint candidate — a restore
    would have no on-disk files to mutate."""
    r = _git(path, "rev-parse", "--git-dir")
    if r.returncode != 0:
        return False
    # A bare repository has no worktree. ``git rev-parse`` returns
    # success on ``--git-dir`` but a bare repo cannot be snapshotted in
    # the sense the kickoff means (a restore would have nothing to
    # overwrite). Use ``--is-inside-work-tree`` to keep bare repos out.
    rb = _git(path, "rev-parse", "--is-inside-work-tree")
    return rb.returncode == 0 and rb.stdout.strip() == "true"


def _worktree_tree(repo: Path) -> str | None:
    """Tree hash of the worktree as ``git add -A`` sees it.

    Uses a temp ``GIT_INDEX_FILE`` seeded from the user's real index
    (copied byte-for-byte so the stat cache survives — a ``read-tree
    HEAD`` seed would force ``add -A`` to re-stat every tracked file
    on every node, which gets expensive on a large target repo). The
    real index and working tree are never touched. Returns None on any
    git failure (the caller logs and returns None)."""
    with tempfile.TemporaryDirectory(prefix="mo-ckpt-idx-") as td:
        idx_path = os.path.join(td, "index")
        env_extra = {"GIT_INDEX_FILE": idx_path}
        # ``--git-path index`` returns the path to the real index
        # relative to the worktree (handles linked worktrees where the
        # index lives under ``.git/worktrees/<name>/index``). A fallback
        # to ``.git/index`` covers older git versions without the flag.
        real_index = _git_out(repo, "rev-parse", "--git-path", "index")
        if real_index:
            real_index_path = os.path.join(str(repo), real_index)
        else:
            real_index_path = os.path.join(str(repo), ".git", "index")
        if os.path.isfile(real_index_path):
            # ``copy2`` preserves mtime/atime; the stat cache key git
            # uses is the file's mtime + size + dev/ino, so the seed
            # matches what the user already had.
            try:
                shutil.copy2(real_index_path, idx_path)
            except OSError:
                pass
        else:
            # Fresh init: no index yet, seed from HEAD. A repo with no
            # HEAD would fail here, and the ``snapshot()`` guard above
            # rejects that case.
            r = _git(repo, "read-tree", "HEAD", env_extra=env_extra)
            if r.returncode != 0:
                return None
        # ``add -A`` respects .gitignore and includes new untracked files.
        # ``--`` is the standard separator so a path called ``-A`` can
        # never become an option, even though that cannot happen on a
        # worktree tree capture.
        r = _git(repo, "add", "-A", "--", ".", env_extra=env_extra)
        if r.returncode != 0:
            return None
        r = _git(repo, "write-tree", env_extra=env_extra)
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None


# --------------------------------------------------------------------------- public API


def snapshot(
    worktree: str | os.PathLike,
    run_id: str,
    node_id: str,
    *,
    overwrite: bool = True,
) -> str | None:
    """Capture the worktree (tracked + untracked, non-ignored) into a git
    ref. Returns the ref's sha on success, ``None`` on any error.

    The user's HEAD, real index, and working tree are never touched —
    the capture is performed under a temporary ``GIT_INDEX_FILE`` and
    committed as a child of the current HEAD via ``commit-tree`` /
    ``update-ref``. ``overwrite=False`` keeps an existing ref (the
    ``_start`` call site uses this so a resumed run preserves its
    original snapshot)."""
    wt = Path(worktree)
    if not _is_git_repo(wt):
        _warn(f"snapshot: {wt!s} is not a git work tree")
        return None
    safe_run = _sanitize(run_id)
    safe_node = _sanitize(node_id)
    ref = f"{_RESERVED_PREFIX}/{safe_run}/{safe_node}"
    if not overwrite:
        existing = _git_out(wt, "rev-parse", "--verify", "--quiet", ref)
        if existing:
            return existing
    tree = _worktree_tree(wt)
    if not tree:
        _warn(f"snapshot: write-tree failed in {wt!s}")
        return None
    head = _git_out(wt, "rev-parse", "HEAD")
    if not head:
        # No commits yet. ``_worktree_tree`` already failed on its
        # ``read-tree HEAD`` step and returned None, so this branch is
        # unreachable in practice; the guard is kept so a future caller
        # that bypasses the temp-index path cannot end up with a
        # detached parentless checkpoint that breaks ``git log``.
        _warn(f"snapshot: no HEAD in {wt!s}; nothing to checkpoint")
        return None
    msg = f"mini-ork checkpoint {safe_run}/{safe_node}"
    sha = _git_out(wt, "commit-tree", tree, "-p", head, "-m", msg)
    if not sha:
        _warn(f"snapshot: commit-tree failed for {ref}")
        return None
    r = _git(wt, "update-ref", ref, sha)
    if r.returncode != 0:
        _warn(f"snapshot: update-ref failed for {ref}: {r.stderr.strip()}")
        return None
    return sha


def list_checkpoints(worktree: str | os.PathLike, run_id: str) -> list[dict[str, Any]]:
    """List every checkpoint for ``run_id`` ordered by creation time.

    Each item: ``{"node", "sha", "created", "files_changed"}``. The
    current tree is computed once (a single ``write-tree``) and diffed
    against each checkpoint's tree, so the call is O(N) git subprocess
    invocations rather than O(N^2)."""
    wt = Path(worktree)
    if not _is_git_repo(wt):
        return []
    safe_run = _sanitize(run_id)
    ref_prefix = f"{_RESERVED_PREFIX}/{safe_run}/"
    r = _git(
        wt,
        "for-each-ref",
        ref_prefix,
        "--sort=creatordate",
        "--format=%(refname) %(objectname) %(creatordate:unix)",
    )
    if r.returncode != 0:
        return []
    current_tree = _worktree_tree(wt) or ""
    out: list[dict[str, Any]] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        refname, sha, created = parts
        # Strip the prefix so ``node`` is just the sanitised node id.
        if refname.startswith(ref_prefix):
            node = refname[len(ref_prefix):]
        else:
            node = refname
        files_changed = 0
        if current_tree:
            ckpt_tree = _git_out(wt, "rev-parse", "--verify", f"{sha}^{{tree}}")
            if ckpt_tree:
                d = _git(wt, "diff", "--name-only", ckpt_tree, current_tree)
                if d.returncode == 0:
                    files_changed = sum(1 for ln in d.stdout.splitlines() if ln.strip())
        try:
            created_int = int(created)
        except ValueError:
            created_int = 0
        out.append({
            "node": node,
            "sha": sha,
            "created": created_int,
            "files_changed": files_changed,
        })
    return out


def _unique_safety_name(wt: Path, run_id: str) -> str:
    """Return a node id of the form ``_pre-restore-<ts>[-N]`` that is
    not yet used as a ref under this run. ``int(time.time())`` collides
    inside one second; an integer suffix disambiguates without changing
    the prefix the kickoff names."""
    base_ts = int(time.time())
    safe_run = _sanitize(run_id)
    for offset in range(0, 1000):
        # ``offset == 0`` keeps the canonical form; later attempts append
        # the offset so two restores in the same second do not collide.
        node = f"_pre-restore-{base_ts}" if offset == 0 else f"_pre-restore-{base_ts}-{offset}"
        ref = f"{_RESERVED_PREFIX}/{safe_run}/{_sanitize(node)}"
        existing = _git_out(wt, "rev-parse", "--verify", "--quiet", ref)
        if not existing:
            return node
    # Pathological clock-or-loop fallthrough — the caller will fail when
    # the safety snapshot itself fails.
    return f"_pre-restore-{base_ts}"


def _is_run_live(home: str | os.PathLike | Path, run_id: str) -> bool:
    """A run is "live" when its ``task_runs.status`` row in
    ``<home>/state.db`` is none of ``published``, ``failed``,
    ``rolled_back``, ``reviewing``. ``reviewing`` is the explicit
    exception — that is the human-pause state where a restore is the
    intended verb. A missing DB or row means "not live" so a run
    outside the database (the unit-test case) is not gated."""
    db_path = Path(home) / "state.db"
    if not db_path.is_file():
        return False
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = con.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,))
            row = cur.fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — a corrupt DB must not block restore
        return False
    if not row:
        return False
    status = str(row[0] or "").strip().lower()
    return status not in ("published", "failed", "rolled_back", "reviewing")


def _is_workspace_of_run(
    home: str | os.PathLike | None, run_id: str, worktree: Path,
) -> bool:
    """True when ``worktree`` resolves to the path the workspace
    record holds for this run. ``workspaces.load`` indexes several
    keys by name; a KeyError (a half-written record) counts as no
    match, so the operator can still proceed with ``force``."""
    if home is None:
        return False
    try:
        from mini_ork import workspaces  # local import: keep vcs package DB-free
        ws = workspaces.load(Path(home), run_id)
    except Exception:  # noqa: BLE001 — half-written record, etc.
        return False
    if ws is None:
        return False
    try:
        return Path(ws.path).resolve() == Path(worktree).resolve()
    except OSError:
        return False


def restore(
    worktree: str | os.PathLike,
    run_id: str,
    node_id: str,
    *,
    home: str | os.PathLike | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Restore the worktree to a checkpoint's tree. Always returns a dict
    with ``ok``, ``reason``, ``restored_to``, ``safety_ref`` keys.

    Refusals (no file touched, no ref created):

      * the ref does not exist → ``ok: False, reason: "unknown node"``
      * the worktree is not the run's own workspace (per
        ``workspaces.load``) and ``force`` is false → ``ok: False,
        reason: "not the run's workspace"``
      * the run is still live (its ``task_runs.status`` is not in
        ``published|failed|rolled_back|reviewing``) and ``force`` is
        false → ``ok: False, reason: "run is live"``

    On success the order is: resolve the target sha, take a safety
    snapshot (``_pre-restore-<ts>``), reset the working tree to the
    checkpoint's tree, then ``git reset -q`` so the index returns to
    HEAD and the checkpoint's untracked files come back untracked.
    HEAD is never moved."""
    wt = Path(worktree)
    if not _is_git_repo(wt):
        return {
            "ok": False, "reason": f"{wt!s} is not a git work tree",
            "restored_to": None, "safety_ref": None,
        }
    safe_run = _sanitize(run_id)
    safe_node = _sanitize(node_id)
    ref = f"{_RESERVED_PREFIX}/{safe_run}/{safe_node}"
    target_sha = _git_out(wt, "rev-parse", "--verify", ref)
    if not target_sha:
        return {
            "ok": False, "reason": f"unknown node {safe_node!r}",
            "restored_to": None, "safety_ref": None,
        }
    if not force:
        # Fail closed when the caller did not supply enough context to
        # prove this is the run's workspace. ``_is_workspace_of_run``
        # already returns False when ``home is None`` or when the
        # workspaces record is missing, so a single check covers both
        # "no home" and "no record for this run" — the kickoff names
        # the latter explicitly. A direct library caller must opt in
        # with ``force=True`` to restore without that proof.
        if not _is_workspace_of_run(home, run_id, wt):
            return {
                "ok": False, "reason": "not the run's workspace",
                "restored_to": None, "safety_ref": None,
            }
        if _is_run_live(home, run_id):
            return {
                "ok": False, "reason": "run is live",
                "restored_to": None, "safety_ref": None,
            }
    safety_name = _unique_safety_name(wt, run_id)
    safety_sha = snapshot(wt, run_id, safety_name, overwrite=False)
    if not safety_sha:
        return {
            "ok": False, "reason": "safety snapshot failed",
            "restored_to": None, "safety_ref": f"{_RESERVED_PREFIX}/{safe_run}/{_sanitize(safety_name)}",
        }
    safety_ref = f"{_RESERVED_PREFIX}/{safe_run}/{_sanitize(safety_name)}"
    # Restore: seed a temp index from the checkpoint tree, then run a
    # two-tree reset so files present now but absent from the checkpoint
    # are deleted, and files in the checkpoint are recreated.
    ckpt_tree = _git_out(wt, "rev-parse", "--verify", f"{target_sha}^{{tree}}")
    if not ckpt_tree:
        return {
            "ok": False, "reason": "checkpoint tree missing",
            "restored_to": None, "safety_ref": safety_ref,
        }
    with tempfile.TemporaryDirectory(prefix="mo-ckpt-restore-") as td:
        env_extra = {"GIT_INDEX_FILE": os.path.join(td, "index")}
        # Seed the temp index from the safety snapshot's tree so
        # ``read-tree --reset -u`` has something to remove before it
        # installs the checkpoint tree (an empty seed can leave
        # already-tracked files in place on some git versions).
        seed_tree = _git_out(wt, "rev-parse", "--verify", f"{safety_sha}^{{tree}}")
        if seed_tree:
            _git(wt, "read-tree", seed_tree, env_extra=env_extra)
        r = _git(wt, "read-tree", "--reset", "-u", ckpt_tree, env_extra=env_extra)
        if r.returncode != 0:
            return {
                "ok": False,
                "reason": f"read-tree failed: {r.stderr.strip() or 'unknown error'}",
                "restored_to": None, "safety_ref": safety_ref,
            }
    # Index reset to HEAD so the checkpoint's untracked files come back
    # untracked (not staged). The real index file is what HEAD points at
    # (it never moved), so this is just a no-op marker reset.
    r = _git(wt, "reset", "-q")
    if r.returncode != 0:
        return {
            "ok": False, "reason": f"reset failed: {r.stderr.strip() or 'unknown error'}",
            "restored_to": target_sha, "safety_ref": safety_ref,
        }
    return {
        "ok": True, "reason": "restored",
        "restored_to": target_sha, "safety_ref": safety_ref,
    }


# --------------------------------------------------------------------------- helpers


def _warn(msg: str) -> None:
    """One-line stderr write. ``flush=True`` because the call sites are
    inside the dispatch trace() seam where buffered stderr is lost."""
    print(f"[ckpt] {msg}", file=sys.stderr, flush=True)
