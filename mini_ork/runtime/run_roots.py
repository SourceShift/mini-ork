"""Pinned run roots — target tree, run dir, ``MINI_ORK_HOME``, engine root.

Resolved at run start and stored in ``run_profile.json["roots"]`` so every node,
verifier and git step reads the same values. Closes the baseline-vs-
implementer tree-drift bug where ``_capture_pre_impl_baseline`` snapshotted
one tree while the implementer edited another (when the executor cwd did
not equal the resolved target).

The target-resolution ladder is byte-identical to ``_resolve_target_cwd``
in :mod:`mini_ork.cli.execute`: explicit ``MO_TARGET_CWD`` git toplevel,
then the kickoff's git toplevel, then an explicit ``MINI_ORK_TARGET_REPO``
that resolves to a non-engine git repo, then the kickoff dir, then cwd. A
kickoff staged under ``MINI_ORK_HOME`` is exempt from the kickoff rungs: it
lives in the engine checkout and declares no target. The
two helpers exist side-by-side so a parity test can cross-check them — and
``_resolve_target_cwd`` is reduced to a thin wrapper that delegates here.

The ladder fails closed: a *winning* named target — the highest-precedence
declaration among ``MO_TARGET_CWD``, ``MINI_ORK_TARGET_REPO`` and the profile's
``target_repo`` — that is not a directory raises ``RunRootsError`` instead of
falling through to the kickoff's toplevel, and a target that resolves onto the
engine checkout raises unless ``MO_ALLOW_FRAMEWORK_CWD=1`` declares a
self-edit. A spawned epic child inherits its parent's ``MINI_ORK_TARGET_REPO``
through ``_child_env``; ``MINI_ORK_PARENT_RUN_ID`` marks that inheritance, so
the value declares nothing for the child. ``persist_run_roots`` — the
run-start pinning boundary — turns the error into a rc-2 failed run.

With no ``roots`` record (legacy run dirs created before this landed),
consumers fall back to today's ``context_env("MO_TARGET_CWD") or
os.getcwd()`` resolution, so behaviour is unchanged for in-flight runs.

Pure stdlib; no import-time I/O. The shared-drive redirect is read here
only to record ``exec_cwd``; the redirect itself still happens at the
implementer handler boundary (``execute_handlers.py:749-750``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Mapping

__all__ = [
    "RunRoots",
    "RunRootsError",
    "RunRootsTargetMissing",
    "load_run_roots",
    "persist_run_roots",
    "resolve_run_roots",
]


class RunTargetMissing(RuntimeError):
    """The target recorded in ``run_profile.json`` no longer exists on disk."""


class RunRootsError(ValueError):
    """A named run target is unusable; the run must fail closed.

    Raised instead of silently dropping an explicitly named target and taking
    the kickoff's toplevel — the fallback that once pinned a run onto the
    engine checkout (run-1791930074-82118): the profile named a target worktree
    that did not exist, roots fell back to the engine's primary checkout, and
    the implementer edited the engine's fixture. A missing target is an
    operator error with a named path, never a licence to edit another tree.
    """


class RunRootsTargetMissing(RunRootsError, RunTargetMissing):
    """A *named* target is gone: fail closed, with the remediation text.

    Also a :class:`RunTargetMissing` so mid-run callers that already translate
    that remediation-shaped failure (``mini_ork/cli/execute_handlers.py``) treat
    a re-resolution onto a since-cleaned worktree the same way, instead of
    surfacing a bare ``ValueError``.
    """


@dataclass(frozen=True)
class RunRoots:
    """A run's four fixed roots, resolved once at run start.

    ``target`` is the implementer edit surface (resolved git toplevel of
    the kickoff, or the explicit ``MO_TARGET_CWD`` value when set). The
    other three are the run-identity variables the executor already
    publishes. ``exec_cwd`` is recorded only when the opt-in shared-drive
    backend is set (``MO_SHARED_DRIVE_BACKEND``); absent otherwise so the
    record stays portable for the cloud sandbox re-mapping in D4.
    """
    target: str
    run_dir: str
    home: str
    engine: str
    exec_cwd: str | None = None


def _git_toplevel(path: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
    except Exception:
        return ""
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return ""


def _git_toplevel_from_dir(kdir: str) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=kdir, capture_output=True, text=True,
        )
    except Exception:
        return ""
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return ""


def _is_spawned_child(src: Mapping[str, str]) -> bool:
    """True when this process is an epic child spawned by ``_child_env``.

    That helper copies the parent's whole environment, drops this node's run
    identity, and *sets* ``MINI_ORK_PARENT_RUN_ID`` unconditionally; it never
    sets ``MINI_ORK_TARGET_REPO``. A process carrying both therefore inherited
    the repo from its parent, and the value declares nothing about where this
    run edits. Honoring it pinned the child onto the parent's edit surface
    (run-1791785967-43108, whose Linear child wrote into the engine checkout)
    whenever the parent launched from its target worktree.
    """
    return bool((src.get("MINI_ORK_PARENT_RUN_ID", "") or "").strip())


def _repo_under_engine_guard(src: Mapping[str, str], *, engine_top: str = "") -> str:
    """git-toplevel of an explicit ``MINI_ORK_TARGET_REPO``, unless it is the engine.

    Returns ``""`` when the var is unset, is not a directory, is not a git
    repo, is inherited by a spawned epic child (see :func:`_is_spawned_child`),
    or resolves to the same tree as ``MINI_ORK_ROOT`` /
    ``MINI_ORK_ENGINE_ROOT``. The engine exclusion is load-bearing:
    ``bin/mini-ork`` defaults ``MINI_ORK_TARGET_REPO`` to the process cwd, so
    when a run is launched from the engine that default would otherwise
    re-introduce the CWT-A corruption the kickoff-dir fallback exists to
    prevent. This rung therefore only ever fires for an operand that is
    *both* a real repo, declared by this run, and not the engine.
    """
    if _is_spawned_child(src):
        return ""
    raw = (src.get("MINI_ORK_TARGET_REPO", "") or "").strip()
    if not raw or not os.path.isdir(raw):
        return ""
    top = _git_toplevel(raw)
    if not top:
        return ""
    if engine_top and os.path.realpath(top) == os.path.realpath(engine_top):
        return ""
    return top


def _path_within(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` or lives under it (realpath-compared)."""
    if not path or not root:
        return False
    try:
        resolved_path = os.path.realpath(path)
        resolved_root = os.path.realpath(root)
    except OSError:
        return False
    return resolved_path == resolved_root or resolved_path.startswith(resolved_root + os.sep)


def _read_profile(run_dir: str) -> dict:
    """The run's ``run_profile.json`` as a dict (``{}`` when absent/malformed)."""
    prof_path = os.path.join(run_dir, "run_profile.json") if run_dir else ""
    if not prof_path or not os.path.isfile(prof_path):
        return {}
    try:
        with open(prof_path) as fh:
            prof = json.load(fh)
    except (OSError, ValueError):
        return {}
    return prof if isinstance(prof, dict) else {}


def _named_targets(prof: Mapping[str, object], src: Mapping[str, str]) -> list[str]:
    """Every path this run *itself* declares as its target, in precedence order.

    ``MO_TARGET_CWD`` and the profile's ``target_repo`` are declarations of
    intent, not hints: when the winning one names a directory that is not
    there the run must refuse rather than drop it and infer another tree.

    ``MINI_ORK_TARGET_REPO`` is a declaration only when this process is not a
    spawned epic child: ``_child_env`` inherits the parent's copy but marks the
    child with ``MINI_ORK_PARENT_RUN_ID`` (see :func:`_is_spawned_child`), so
    for a child that var records the parent's surface, not the child's.
    """
    named: list[str] = []
    explicit = str(src.get("MO_TARGET_CWD", "") or "").strip()
    if explicit:
        named.append(explicit)
    if not _is_spawned_child(src):
        repo = str(src.get("MINI_ORK_TARGET_REPO", "") or "").strip()
        if repo:
            named.append(repo)
    profile_repo = str(prof.get("target_repo", "") or "").strip()
    if profile_repo:
        named.append(profile_repo)
    return named


def _missing_target_message(path: str) -> str:
    """The fail-closed reason for a dead named target, with the two ways out."""
    return (
        f"target {path} does not exist\n"
        "  The run named it as the edit surface, so roots refuse to fall back\n"
        "  to another tree (that fallback once pinned a run onto the engine\n"
        "  checkout). Re-create that directory, or resume against a fresh\n"
        "  checkout by setting MO_TARGET_CWD=<path>."
    )


def _engine_toplevel(src: Mapping[str, str]) -> tuple[str, str]:
    """``(git_toplevel, effective_toplevel)`` of the engine, computed once.

    The effective value falls back to the raw ``MINI_ORK_ROOT`` when it is not
    a git repo, so the engine-coincidence comparisons still work; the git
    toplevel is kept for rungs that may only assign a real repo. A single
    ``git`` subprocess serves both (this runs on the per-node path).
    """
    engine = (src.get("MINI_ORK_ROOT", "")
              or src.get("MINI_ORK_ENGINE_ROOT", "") or "").strip()
    if not engine:
        return "", ""
    git_top = _git_toplevel(engine)
    return git_top, git_top or engine


def _enforce_engine_guard(
    target: str, *, named: list[str], home_staged: bool, src: Mapping[str, str],
    engine_top: str,
) -> None:
    """Refuse a target that resolves onto the engine checkout without the flag.

    A run that silently edits the engine is how RB-01 corrupted the engine's
    frozen fixture. ``MO_ALLOW_FRAMEWORK_CWD=1`` is the operator's explicit
    acknowledgement of a self-edit; anything else (missing, ``"0"``, ``"true"``)
    fails closed.

    A *home-staged* kickoff declares no target (see the module docstring): with
    nothing named, the launch tree stands even when it is the engine, because
    there is no named target to contradict. Once anything is named — or the
    kickoff lives in a tree of its own — the coincidence is a self-edit and
    needs the flag. ``engine_top`` is computed once by the caller.
    """
    if not engine_top:
        return
    if (src.get("MO_ALLOW_FRAMEWORK_CWD", "") or "").strip() == "1":
        return
    if not named and home_staged:
        return
    target_top = _git_toplevel(target) if target else ""
    if target_top and os.path.realpath(target_top) == os.path.realpath(engine_top):
        raise RunRootsError(
            f"target resolves to the engine checkout {target_top}; "
            "set MO_ALLOW_FRAMEWORK_CWD=1 for a self-edit"
        )


def resolve_run_roots(run_dir: str, *, env: Mapping[str, str] | None = None) -> RunRoots:
    """Resolve the run's four roots from ``run_dir`` + current env.

    The target ladder is verbatim from ``mini_ork/cli/execute.py:_resolve_target_cwd``
    so a parity test can prove byte-identical output across the four
    precedence branches. The drive redirect is recorded separately
    (``exec_cwd``) and never re-resolves the target.

    Fails closed (``RunRootsError``) when the winning named target does not
    exist, and when the resolved target coincides with the engine checkout
    unless ``MO_ALLOW_FRAMEWORK_CWD=1`` declares a self-edit. The target
    missing error is also a ``RunTargetMissing`` (remediation text included) so
    callers that already translate that mid-run failure keep working.
    """
    src = os.environ if env is None else env
    prof = _read_profile(run_dir)
    engine_git_top, engine_top = _engine_toplevel(src)
    named = _named_targets(prof, src)
    # Declarations are checked in precedence order and only the *winning* one
    # decides: a dead lower-precedence name must not veto a live pin (a pinned
    # epic child whose parent's worktree was cleaned still has a valid
    # MO_TARGET_CWD). A dead winning declaration is the RB-01 shape and fails
    # closed instead of falling through to the kickoff's toplevel.
    if named and not os.path.isdir(named[0]):
        raise RunRootsTargetMissing(_missing_target_message(named[0]))

    explicit = (src.get("MO_TARGET_CWD", "") or "").strip()
    target = ""
    if explicit and os.path.isdir(explicit):
        target = _git_toplevel(explicit)
    home_staged = False
    if not target:
        kickoff = str(prof.get("kickoff_path", "") or "")
        if kickoff and os.path.isfile(kickoff):
            kdir = os.path.dirname(kickoff)
            # A kickoff staged under the run home lives in the engine checkout
            # (``<home>/kickoffs/`` or ``<home>/runs/``), so its toplevel says
            # where the kickoff is stored, not where the change goes. Reading it
            # as a target pinned a run launched from its target worktree onto
            # the engine, and the ground-truth gate then compared the worktree's
            # base with the engine's HEAD and failed a good change
            # (run-1792224150-80303). `MINI_ORK_ROOT` == the launch cwd is the
            # signal that the engine was started from the tree it should edit.
            home_staged = _path_within(kickoff, src.get("MINI_ORK_HOME", "") or "")
            if home_staged:
                # An explicit MINI_ORK_TARGET_REPO is a stronger signal of intent
                # than any inferred tree, but never when it resolves to the
                # engine (the CWT-A hazard).
                target = _repo_under_engine_guard(src, engine_top=engine_top)
                if not target:
                    cwd_top = _git_toplevel(os.getcwd())
                    if engine_git_top and engine_git_top == cwd_top:
                        target = engine_git_top
                if not target:
                    target = _git_toplevel_from_dir(kdir)
            else:
                target = _git_toplevel_from_dir(kdir)
                if not target:
                    # An explicit MINI_ORK_TARGET_REPO is a stronger signal of
                    # intent than the bare kickoff dir, so honor it — but ONLY
                    # once the kickoff's own repo has been ruled out (the
                    # "kickoff repo wins over cwd" guarantee above stays
                    # intact), and never when it resolves to the engine (the
                    # CWT-A hazard). When nothing qualifies, fall back to the
                    # kickoff dir for bash parity.
                    target = _repo_under_engine_guard(src, engine_top=engine_top)
                if not target:
                    # Bash parity: dirname(kickoff) on git failure — preserves
                    # the CWT-A corruption fix. Never fall through to
                    # os.getcwd().
                    target = kdir
    if not target:
        target = explicit or os.getcwd()

    _enforce_engine_guard(
        target, named=named, home_staged=home_staged, src=src,
        engine_top=engine_top,
    )

    exec_cwd: str | None = None
    backend = (src.get("MO_SHARED_DRIVE_BACKEND", "") or "").strip()
    if backend:
        # Lazy import keeps the seam side-effect-free for the default path.
        from mini_ork.runtime.run_drive import resolve_run_drive_cwd

        exec_cwd = resolve_run_drive_cwd(target, env=src)

    return RunRoots(
        target=target,
        run_dir=run_dir or "",
        home=src.get("MINI_ORK_HOME", "") or "",
        engine=src.get("MINI_ORK_ROOT", "") or "",
        exec_cwd=exec_cwd,
    )


def require_run_target(*, roots: RunRoots | None) -> str:
    """The run's target, or raise ``RunTargetMissing`` naming the missing path.

    ``load_run_roots`` deliberately trusts the persisted ``roots.target``
    without touching the filesystem — cheap, and correct for a run still in
    flight. But a run *resumed* after its worktree was cleaned then hands a
    phantom ``cwd`` to every node: the implementer spawns in a directory that
    is gone, produces nothing, and the run dies with an empty log and no clue
    why. Callers about to *use* the target call this instead, so the missing
    path is named at the point of use together with the two ways to fix it.

    A ``None`` record (legacy run dir) yields ``""``; the caller keeps its own
    lazy resolution. This only ever rejects a *recorded* target.
    """
    if roots is None:
        return ""
    target = roots.target
    if not target or os.path.isdir(target):
        return target
    raise RunTargetMissing(
        f"the target recorded for this run no longer exists: {target}\n"
        "  It was captured in run_profile.json when the run started and is gone\n"
        "  now (a worktree cleaned up after the run?). Re-create that directory,\n"
        "  or resume against a fresh checkout by setting MO_TARGET_CWD=<path>."
    )


def load_run_roots(run_dir: str, *, target_override: str = "") -> RunRoots | None:
    """Load a persisted ``roots`` record from ``run_profile.json``.

    Returns ``None`` when the profile is missing, malformed, or carries no
    ``roots`` key (legacy run dir). Consumers treat ``None`` as "use today's
    lazy resolution" so behaviour is byte-identical for in-flight runs.

    ``target_override`` re-points the edit surface when the *recorded* target no
    longer exists — the run's worktree was cleaned up after it started, and a
    resume would otherwise hand a phantom cwd to every node. Only a recorded
    target that is genuinely gone is overridden, and only by a path that exists;
    everything else in the record (``run_dir``, ``home``, ``engine``) is kept.
    With no usable override the recorded value stands, and
    ``require_run_target`` fails the run closed — the operator gets a named
    path instead of a spawn error from a directory that is not there.
    """
    if not run_dir:
        return None
    prof_path = os.path.join(run_dir, "run_profile.json")
    if not os.path.isfile(prof_path):
        return None
    try:
        with open(prof_path) as fh:
            prof = json.load(fh)
    except Exception:
        return None
    if not isinstance(prof, dict):
        return None
    raw = prof.get("roots")
    if not isinstance(raw, dict):
        return None
    target = raw.get("target", "") or ""
    exec_cwd = raw.get("exec_cwd") or None
    if target and not os.path.isdir(target) \
            and target_override and os.path.isdir(target_override):
        target = target_override
        # The recorded drive cwd was resolved off the deleted target; drop it so
        # the caller re-resolves against the replacement.
        exec_cwd = None
    if not target:
        return None
    return RunRoots(
        target=target,
        run_dir=raw.get("run_dir", "") or "",
        home=raw.get("home", "") or "",
        engine=raw.get("engine", "") or "",
        exec_cwd=exec_cwd,
    )


def _fail_run_start(run_dir: str, message: str) -> None:
    """Report a refused pin on stderr and in the run's notes (best effort).

    ``task_runs.notes`` is the durable channel the run UI and recovery read; a
    missing/unreadable DB must never mask the failure being reported, so this
    only ever warns into the void.
    """
    print(f"mini-ork: run-start refused: {message}", file=sys.stderr)
    db = (os.environ.get("MINI_ORK_DB", "") or "").strip()
    if not db:
        home = (os.environ.get("MINI_ORK_HOME", "") or "").strip()
        if home:
            db = os.path.join(home, "state.db")
    run_id = (os.environ.get("MINI_ORK_RUN_ID", "") or "").strip() \
        or os.path.basename(str(run_dir).rstrip(os.sep))
    if not db or not run_id or not os.path.isfile(db):
        return
    try:
        import sqlite3  # local: the happy path never pays for it

        con = sqlite3.connect(db, timeout=5.0)
        try:
            con.execute("PRAGMA busy_timeout = 5000")
            con.execute(
                "UPDATE task_runs SET notes = COALESCE(notes || '; ', '') || ? "
                "WHERE id = ?",
                (f"run-start refused: {message}", run_id),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        # The note is evidence, not the failure: never let writing it raise.
        return


def persist_run_roots(run_dir: str) -> RunRoots | None:
    """Idempotently write ``roots`` into ``run_profile.json``.

    Returns the persisted record — newly written OR already present. Skips
    when the profile is missing/malformed or already carries a ``roots``
    key: a resumed run keeps its original roots even if ``MO_TARGET_CWD``
    changed between cycles. Uses read-modify-write to avoid clobbering
    concurrent profile updates from the run loop.

    This is the run-start pinning boundary: a ``RunRootsError`` from
    :func:`resolve_run_roots` fails the run closed with rc 2 and the reason on
    stderr and in ``task_runs.notes``, instead of letting the run start with
    roots that fell back onto some other tree.
    """
    if not run_dir:
        return None
    prof_path = os.path.join(run_dir, "run_profile.json")
    if not os.path.isfile(prof_path):
        return None
    try:
        with open(prof_path) as fh:
            prof = json.load(fh)
    except Exception:
        return None
    if not isinstance(prof, dict):
        return None
    if "roots" in prof:
        return load_run_roots(run_dir)
    try:
        roots = resolve_run_roots(run_dir)
    except RunRootsError as exc:
        _fail_run_start(run_dir, str(exc))
        raise SystemExit(2) from exc
    prof["roots"] = asdict(roots)
    try:
        with open(prof_path, "w") as fh:
            json.dump(prof, fh, indent=2, sort_keys=True)
    except OSError:
        return None
    return roots