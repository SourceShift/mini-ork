"""Pinned run roots — target tree, run dir, ``MINI_ORK_HOME``, engine root.

Resolved at run start and stored in ``run_profile.json["roots"]`` so every node,
verifier and git step reads the same values. Closes the baseline-vs-
implementer tree-drift bug where ``_capture_pre_impl_baseline`` snapshotted
one tree while the implementer edited another (when the executor cwd did
not equal the resolved target).

The target-resolution ladder is byte-identical to ``_resolve_target_cwd``
in :mod:`mini_ork.cli.execute`: explicit ``MO_TARGET_CWD`` git toplevel,
then the kickoff's git toplevel, then the kickoff dir, then cwd. The two
helpers exist side-by-side so a parity test can cross-check them — and
``_resolve_target_cwd`` is reduced to a thin wrapper that delegates here.

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
from dataclasses import asdict, dataclass
from typing import Mapping

__all__ = [
    "RunRoots",
    "load_run_roots",
    "persist_run_roots",
    "resolve_run_roots",
]


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


def resolve_run_roots(run_dir: str, *, env: Mapping[str, str] | None = None) -> RunRoots:
    """Resolve the run's four roots from ``run_dir`` + current env.

    The target ladder is verbatim from ``mini_ork/cli/execute.py:_resolve_target_cwd``
    so a parity test can prove byte-identical output across the four
    precedence branches. The drive redirect is recorded separately
    (``exec_cwd``) and never re-resolves the target.
    """
    src = os.environ if env is None else env

    explicit = src.get("MO_TARGET_CWD", "") or ""
    target = ""
    if explicit and os.path.isdir(explicit):
        target = _git_toplevel(explicit)
    if not target:
        kickoff = ""
        prof_path = os.path.join(run_dir, "run_profile.json") if run_dir else ""
        if prof_path and os.path.isfile(prof_path):
            try:
                kickoff = json.load(open(prof_path)).get("kickoff_path", "") or ""
            except Exception:
                kickoff = ""
        if kickoff and os.path.isfile(kickoff):
            kdir = os.path.dirname(kickoff)
            target = _git_toplevel_from_dir(kdir)
            if not target:
                # Bash parity: dirname(kickoff) on git failure — preserves the
                # CWT-A corruption fix. Never fall through to os.getcwd().
                target = kdir
    if not target:
        target = explicit or os.getcwd()

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


def load_run_roots(run_dir: str) -> RunRoots | None:
    """Load a persisted ``roots`` record from ``run_profile.json``.

    Returns ``None`` when the profile is missing, malformed, or carries no
    ``roots`` key (legacy run dir). Consumers treat ``None`` as "use today's
    lazy resolution" so behaviour is byte-identical for in-flight runs.
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
    if not target:
        return None
    return RunRoots(
        target=target,
        run_dir=raw.get("run_dir", "") or "",
        home=raw.get("home", "") or "",
        engine=raw.get("engine", "") or "",
        exec_cwd=(raw.get("exec_cwd") or None),
    )


def persist_run_roots(run_dir: str) -> RunRoots | None:
    """Idempotently write ``roots`` into ``run_profile.json``.

    Returns the persisted record — newly written OR already present. Skips
    when the profile is missing/malformed or already carries a ``roots``
    key: a resumed run keeps its original roots even if ``MO_TARGET_CWD``
    changed between cycles. Uses read-modify-write to avoid clobbering
    concurrent profile updates from the run loop.
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
    roots = resolve_run_roots(run_dir)
    prof["roots"] = asdict(roots)
    try:
        with open(prof_path, "w") as fh:
            json.dump(prof, fh, indent=2, sort_keys=True)
    except OSError:
        return None
    return roots