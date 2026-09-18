"""Python port of bin/mini-ork-spawn — bounded child mini-ork orchestrator.

Faithful port of the bash CLI wrapper that approves + (optionally) executes a
bounded child mini-ork. Co-existence model (strangler-fig): bash
`bin/mini-ork-spawn` remains the authoritative source. This module mirrors
its observable surfaces exactly:

    spawn_id=…
    parent_run_id=…
    child_run_id=…
    child_workspace=…
    child_kickoff=…
    depth=N
    allow_child_spawn=0|1
    spawn_status=approved|completed|failed

Plus identical DB rows in `run_spawns`, `run_events`, and `task_runs` (floats
1e-6 on `authority_level`; epochs within 1s tolerance; `spawn_id`/`event_id`
stem-equal because the random hex12 suffix is generated per port).

DB writes are delegated to `mini_ork.orchestration.recursive` (the
port of `lib/recursive_orchestration.sh`). The Python port does NOT inline
SQLite writes; it composes the recursive_orchestration helpers so that DB
parity vs bash is enforced once at that layer's parity test.

The execute path shells out to `bin/mini-ork` subprocess (the real CLI), so
the port does not duplicate the child run engine. The `MINI_ORK_ROOT` env var
locates the CLI binary; falls back to the directory above this file.

Isolation (S1 of docs/architecture/cloud-swarm.md): with `MO_SANDBOX_BACKEND`
unset the child launch is a host `subprocess.run` — byte-for-byte today's
behavior. With a backend set the child is launched through the `Workspace`
axis (`up → spawn → down`), so the same call places the child in a docker
container, a microVM, or (S4) a remote sandbox without the caller knowing.
Two consequences of crossing that boundary: the child's tree rides the shared
drive, so its `MINI_ORK_HOME`/kickoff/cwd are handed over as their in-sandbox
mount paths (a path escaping the drive is a loud error, not a silent miss),
and its env crosses as the `container_env` allowlist rather than the whole
host environment.

Usage:
    from mini_ork.cli import spawn as mini_ork_spawn
    rc = mini_ork_spawn.main(["--parent-run", "p1", "--kickoff", "/tmp/k.md",
                              "--child-run", "c1", "--no-execute"])
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from mini_ork.orchestration import recursive as ro

__all__ = [
    "SpawnResult",
    "parse_args",
    "compute_child_run_id",
    "resolve_depth",
    "prepare_child_workspace",
    "spawn",
    "main",
    "USAGE",
    "ENV_SANDBOX_BACKEND",
    "ENV_SANDBOX_CHILD_TIMEOUT",
    "ENV_SANDBOX_CLI",
    "ENV_SHARED_DRIVE_ROOT",
]


# Mirrors the usage block in bin/mini-ork-spawn lines 8-26 (verbatim).
USAGE = """\
Usage: mini-ork spawn --parent-run <run-id> --kickoff <child.md> [options]

Options:
  --recipe <name>              Force child recipe; omit to use markdown dispatcher.
  --child-run <id>             Stable child run id (default: child-<ts>-<pid>).
  --depth <n>                  Child depth from root (default: infer parent depth + 1).
  --authority <0.0..0.9>       Child authority level (default: 0.3).
  --allow-child-spawn          Permit this child to spawn descendants.
  --no-execute                 Record approved spawn without running child mini-ork.
  --help                       Show this help.

Environment policy:
  MINI_ORK_RECURSIVE_MAX_DEPTH        default 2
  MINI_ORK_RECURSIVE_MAX_CHILDREN     default 4
  MINI_ORK_RECURSIVE_MAX_DESCENDANTS  default 16
  MINI_ORK_RECURSIVE_MAX_PARALLEL     default 4
"""


# S1 spawn isolation knobs (docs/architecture/cloud-swarm.md).
ENV_SANDBOX_BACKEND = "MO_SANDBOX_BACKEND"
ENV_SANDBOX_CHILD_TIMEOUT = "MO_SANDBOX_CHILD_TIMEOUT"
ENV_SANDBOX_CLI = "MO_SANDBOX_MINI_ORK_CLI"
ENV_SHARED_DRIVE_ROOT = "MO_SHARED_DRIVE_ROOT"

# Backends that spawn the child in-process on THIS machine, with no Workspace
# at all: the unset default and its explicit "host" alias. Every other name
# goes through the Workspace axis — `local` onto host paths, a real backend
# into a sandbox (where the drive and the path remapping below apply).
_NATIVE_BACKENDS = ("", "host")

# Today's host `subprocess.run` has no timeout, so the routed transport's
# default budget is "unbounded in practice" (7 days) rather than a new cap the
# opt-in path would impose. The transport still needs a float.
_DEFAULT_CHILD_TIMEOUT = 604800.0

# In a sandbox the child CLI is whatever the image puts on PATH (the documented
# image expectation in cloud-swarm.md S1); on the host it is the repo's own
# `bin/mini-ork`.
_DEFAULT_SANDBOX_CLI = "mini-ork"


# Mirrors bin/mini-ork-spawn:5 (MINI_ORK_ROOT resolution).
def _resolve_root(explicit: str | None = None) -> str:
    """Return the mini-ork repo root: explicit arg > $MINI_ORK_ROOT > repo dir."""
    if explicit:
        return explicit
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return env_root
    return str(Path(__file__).resolve().parents[2])


# Mirrors bin/mini-ork-spawn:58-60 (HOME + DB resolution).
def _resolve_paths(home: str | None = None, db: str | None = None) -> tuple[str, str]:
    resolved_home = home or os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    resolved_db = db or os.environ.get("MINI_ORK_DB") or os.path.join(resolved_home, "state.db")
    return resolved_home, resolved_db


# Mirrors bin/mini-ork-spawn:38-52 (the bash while-case parser).
def parse_args(argv: list[str]) -> dict:
    """Parse argv into a dict mirroring the bash getopts loop.

    Mirrors bash exactly:
      * `--help` / `-h` → SystemExit(0) after printing USAGE to stdout.
      * `--flag` without a value → SystemExit(2) with stderr
        ``<flag> requires a value``.
      * Unknown ``-*`` token → SystemExit(2) with stderr ``Unknown flag: …``
        + USAGE to stderr.
      * Positional ``foo`` → SystemExit(2) with stderr
        ``Unexpected argument: …`` + USAGE to stderr.
      * After the loop, missing `--parent-run` or `--kickoff` → SystemExit(2)
        with stderr ``<flag> is required``.

    Raises:
        SystemExit: with code 0 (help) or 2 (any parse error) and stderr
            message matching bash's phrasing exactly.
    """
    flags_with_value = {
        "--parent-run": "parent_run",
        "--kickoff": "kickoff",
        "--recipe": "recipe",
        "--child-run": "child_run",
        "--depth": "depth",
        "--authority": "authority",
    }
    out: dict = {
        "parent_run": "",
        "kickoff": "",
        "recipe": "",
        "child_run": "",
        "depth": "",
        "authority": None,
        "allow_child_spawn": 0,
        "no_execute": 0,
    }

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--help", "-h"):
            print(USAGE, end="")
            raise SystemExit(0)
        if tok in flags_with_value:
            if i + 1 >= len(argv):
                _die(f"{tok} requires a value")
            out[flags_with_value[tok]] = argv[i + 1]
            i += 2
            continue
        if tok == "--allow-child-spawn":
            out["allow_child_spawn"] = 1
            i += 1
            continue
        if tok == "--no-execute":
            out["no_execute"] = 1
            i += 1
            continue
        if tok.startswith("-"):
            _die(f"Unknown flag: {tok}")
        _die(f"Unexpected argument: {tok}")

    if not out["parent_run"]:
        _die("--parent-run is required")
    if not out["kickoff"]:
        _die("--kickoff is required")

    return out


def _die(msg: str) -> None:
    """Emit ``msg`` + USAGE to stderr and SystemExit(2) — mirrors bash
    `echo "..." >&2; usage >&2; exit 2` at bin/mini-ork-spawn:49-50."""
    sys.stderr.write(f"{msg}\n")
    sys.stderr.write(USAGE)
    raise SystemExit(2)


# Mirrors bin/mini-ork-spawn:66-68 (default child_run_id format).
def compute_child_run_id(explicit: str | None = None, *, ts: int | None = None,
                         pid: int | None = None) -> str:
    """Generate the default child run id `child-<ts>-<pid>`.

    Args:
        explicit: When set, returned verbatim (mirrors `--child-run` override).
        ts: Override for `int(time.time())` — used by tests for determinism.
        pid: Override for `os.getpid()` — used by tests for determinism.

    Returns:
        The child run id. Default format mirrors bash
        `child-$(date +%s)-$$`.
    """
    if explicit:
        return explicit
    sec = int(time.time()) if ts is None else int(ts)
    proc_pid = os.getpid() if pid is None else int(pid)
    return f"child-{sec}-{proc_pid}"


# Mirrors bin/mini-ork-spawn:70-80 (inline python depth inference).
def resolve_depth(parent_run_id: str, db_path: str) -> int:
    """Infer the child depth from the parent's depth (parent+1) or default 1.

    Mirrors the bash heredoc:
        SELECT depth FROM run_spawns WHERE child_run_id=?
    If the parent was itself a child (has a `run_spawns` row keyed by its
    id as `child_run_id`), the child depth is `parent.depth + 1`; otherwise
    the child is depth 1.

    Raises:
        FileNotFoundError: if db_path does not exist (mirrors bash `[ -f
            "$MINI_ORK_DB" ] || exit 2` guard upstream).
    """
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"state.db not found: {db_path}")
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT depth FROM run_spawns WHERE child_run_id=?",
            (parent_run_id,),
        ).fetchone()
    finally:
        con.close()
    return (row[0] + 1) if row else 1


# Mirrors bin/mini-ork-spawn:82-86 (workspace prep).
def prepare_child_workspace(
    home: str,
    parent_run: str,
    child_run: str,
    kickoff_src: str,
) -> dict:
    """Mirror the bash mkdir+cp sequence at bin/mini-ork-spawn:82-86.

    Creates `$MINI_ORK_HOME/runs/$PARENT_RUN/children/$CHILD_RUN/{worktree,artifacts}`,
    copies the kickoff to `$CHILD_BASE/kickoff.md`.

    Returns:
        Dict with keys `child_base`, `child_workspace`, `child_kickoff`,
        `child_artifacts` (all absolute paths).
    """
    if not os.path.isfile(kickoff_src):
        raise FileNotFoundError(f"kickoff not found: {kickoff_src}")
    child_base = os.path.join(home, "runs", parent_run, "children", child_run)
    child_workspace = os.path.join(child_base, "worktree")
    child_artifacts = os.path.join(child_base, "artifacts")
    child_kickoff = os.path.join(child_base, "kickoff.md")
    os.makedirs(child_workspace, exist_ok=True)
    os.makedirs(child_artifacts, exist_ok=True)
    shutil.copyfile(kickoff_src, child_kickoff)
    return {
        "child_base": child_base,
        "child_workspace": child_workspace,
        "child_kickoff": child_kickoff,
        "child_artifacts": child_artifacts,
    }


def _sandbox_backend(env: Mapping[str, str] | None = None) -> str:
    """The spawn-isolation backend; ``""`` (unset) means the host path."""
    src = os.environ if env is None else env
    return (src.get(ENV_SANDBOX_BACKEND) or "").strip()


def _child_timeout(env: Mapping[str, str] | None = None) -> float:
    """Wall-clock budget for a routed child spawn (see `_DEFAULT_CHILD_TIMEOUT`)."""
    src = os.environ if env is None else env
    raw = (src.get(ENV_SANDBOX_CHILD_TIMEOUT) or "").strip()
    return float(raw) if raw else _DEFAULT_CHILD_TIMEOUT


def _host_to_container(path: str, *, drive_root: str, mount_path: str) -> str:
    """Map a host path under ``drive_root`` to its in-sandbox equivalent.

    Only what the drive exports is reachable from a sandbox, so a path that
    escapes the drive root is a hard configuration error: raising here beats
    launching a child whose kickoff or cwd silently does not exist in there.
    """
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(drive_root))
    if rel == os.curdir:
        return mount_path
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError(
            f"{path!r} is outside the shared drive root {drive_root!r}, so an "
            f"isolated child could not reach it; point MINI_ORK_HOME or "
            f"MO_SHARED_DRIVE_ROOT at a directory containing the run tree"
        )
    return os.path.join(mount_path, rel)


class _ChildTransport(NamedTuple):
    """Where the child CLI runs, and how to reach the run tree from there."""

    workspace: Any  # Workspace | None — None keeps today's host subprocess.run
    isolated: bool  # a real sandbox: mount-path view + env allowlist
    drive_root: str  # host dir the run tree lives on (bind-mounted)
    mount_path: str  # in-sandbox path of drive_root ("" on the host)


def _resolve_child_transport(resolved_home: str) -> _ChildTransport:
    """Resolve how to launch the child: the host, or a ``Workspace``.

    ``MO_SANDBOX_BACKEND`` unset (or the ``host`` alias) keeps today's host
    ``subprocess.run`` — no Workspace at all. ``local`` routes through the
    Workspace axis while keeping host paths and the full host env (observable
    parity). Any other name is a sandbox: the run tree moves onto the drive and
    the child sees the mount's view of it.

    Resolution happens before any DB or disk side effect, so an unknown or
    unbuildable backend fails loud with nothing half-recorded.
    """
    backend = _sandbox_backend()
    if backend in _NATIVE_BACKENDS:
        return _ChildTransport(None, False, resolved_home, "")
    # Lazy: the default host path never imports the runtime/backend stack.
    from mini_ork.runtime.agent_workspace import MOUNT_PATH, resolve_spawn_workspace

    if backend == "local":
        workspace = resolve_spawn_workspace("local", cwd=resolved_home)
        return _ChildTransport(workspace, False, resolved_home, MOUNT_PATH)
    drive_root = (os.environ.get(ENV_SHARED_DRIVE_ROOT) or "").strip() or resolved_home
    workspace = resolve_spawn_workspace(backend, cwd=drive_root)
    return _ChildTransport(workspace, True, drive_root, MOUNT_PATH)


class _ChildOutcome:
    """Proc-like shim over ``Workspace.spawn``'s bare ``(rc, stdout, stderr)``.

    ``_persist_child_log`` reads ``.returncode``/``.stdout``/``.stderr`` off a
    ``CompletedProcess``; this keeps that logger at ONE signature no matter
    which transport produced the result.
    """

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _child_argv(cli: str, recipe: str, kickoff: str) -> list[str]:
    """``run <recipe> <kickoff>`` with a recipe, ``run <kickoff>`` without.

    Mirrors bin/mini-ork-spawn:110-129. The kickoff arg is mandatory in both:
    dropping it (the old ``run <recipe>`` with no kickoff) left the child with
    nothing to plan → it failed and never wrote plan.json.
    """
    if recipe:
        return [cli, "run", recipe, kickoff]
    return [cli, "run", kickoff]


def _launch_child(
    workspace: Any,
    *,
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: str,
    timeout: float,
) -> _ChildOutcome:
    """Run the child CLI as a one-shot ``Workspace.spawn`` (up → spawn → down).

    Mirrors ``mini_ork.dispatch.core._spawn_in_workspace``: the spawn is
    one-shot, so the sandbox is cattle — provisioned, used once, torn down. The
    wait stays blocking here because this caller owns the child's lifetime until
    S2 replaces the join with event supervision.
    """
    workspace.up()
    try:
        rc, stdout, stderr = workspace.spawn(
            list(argv), stdin="", timeout=timeout, env=env, cwd=cwd
        )
    finally:
        workspace.down()
    return _ChildOutcome(rc, stdout, stderr)


class SpawnResult:
    """Result of a `spawn()` invocation — mirrors bash stdout + DB writes.

    Attributes:
        lines: stdout lines in the order bash emits them.
        exit_code: 0 on success, 2 on validation failure, child exit code otherwise.
        spawn_id: Generated spawn id from mo_recursive_approve_spawn.
        child_exit_code: Set when the execute step ran; None for --no-execute.
    """

    def __init__(self, lines: list[str], exit_code: int, spawn_id: str = "",
                 child_exit_code: int | None = None) -> None:
        self.lines = lines
        self.exit_code = exit_code
        self.spawn_id = spawn_id
        self.child_exit_code = child_exit_code


def _persist_child_log(home: str, child_run_id: str,
                       cli_argv: list[str], proc: Any) -> str | None:
    """Write the child subprocess's captured stdout/stderr into its run dir.

    ``spawn()`` runs the child with ``capture_output=True``; without persisting
    the result, that output is discarded and a non-zero child (e.g. the
    goal-loop sweep's code-fix child) leaves NO trail explaining WHY it failed —
    the outer driver only sees ``exit_code != 0`` with nothing to self-diagnose.
    Best-effort by contract: a logging failure must never mask the child's real
    exit code, so OSErrors are swallowed and ``None`` is returned.
    """
    try:
        child_run_dir = os.path.join(home, "runs", child_run_id)
        os.makedirs(child_run_dir, exist_ok=True)
        log_path = os.path.join(child_run_dir, "spawn-child.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(cli_argv)}\n")
            fh.write(f"exit_code={getattr(proc, 'returncode', '')}\n\n")
            fh.write("=== stdout ===\n")
            fh.write(getattr(proc, "stdout", None) or "")
            fh.write("\n=== stderr ===\n")
            fh.write(getattr(proc, "stderr", None) or "")
        return log_path
    except OSError:
        return None


# Mirrors bin/mini-ork-spawn:88-141 (the orchestrate-run-execute-mark flow).
def spawn(
    parent_run: str,
    kickoff: str,
    *,
    child_run: str = "",
    depth: int | str | None = None,
    authority: float | str | None = None,
    allow_child_spawn: int | bool = 0,
    no_execute: int | bool = 0,
    recipe: str = "",
    home: str | None = None,
    db: str | None = None,
    root: str | None = None,
    ts: int | None = None,
    pid: int | None = None,
) -> SpawnResult:
    """Mirror bash `bin/mini-ork-spawn` end-to-end.

    Returns a SpawnResult whose `lines` and DB side-effects match bash.
    """
    if not parent_run:
        raise ValueError("--parent-run is required")
    if not kickoff:
        raise ValueError("--kickoff is required")
    if not os.path.isfile(kickoff):
        raise ValueError(f"kickoff not found: {kickoff}")

    resolved_root = _resolve_root(root)
    resolved_home, resolved_db = _resolve_paths(home=home, db=db)

    if not os.path.isfile(resolved_db):
        raise ValueError(f"state.db not found: run mini-ork init first ({resolved_db})")

    # Default authority mirrors bash line 34: ${MINI_ORK_CHILD_AUTHORITY:-0.3}.
    if authority is None or authority == "":
        authority = float(os.environ.get("MINI_ORK_CHILD_AUTHORITY", "0.3"))

    child_run_id = compute_child_run_id(child_run or None, ts=ts, pid=pid)

    if depth is None or depth == "":
        depth_int = resolve_depth(parent_run, resolved_db)
    else:
        depth_int = int(depth)

    allow_flag = 1 if int(allow_child_spawn) else 0

    transport = _resolve_child_transport(resolved_home)
    paths = prepare_child_workspace(
        transport.drive_root, parent_run, child_run_id, kickoff
    )

    if transport.isolated:
        # A sandbox sees the *drive*, not the host tree, so the child's home,
        # kickoff and cwd are handed over as their in-sandbox equivalents.
        # Validated here, before the approve below: a path the drive cannot
        # export must fail with nothing recorded, not as a half-made spawn.
        def _in_sandbox(path: str) -> str:
            return _host_to_container(
                path, drive_root=transport.drive_root, mount_path=transport.mount_path
            )

        child_home = _in_sandbox(resolved_home)
        child_db = _in_sandbox(resolved_db)
        child_cwd = _in_sandbox(paths["child_workspace"])
        kickoff_arg = _in_sandbox(paths["child_kickoff"])
    else:
        child_home, child_db = resolved_home, resolved_db
        child_cwd = paths["child_workspace"]
        kickoff_arg = paths["child_kickoff"]

    spawn_id = ro.mo_recursive_approve_spawn(
        parent_run_id=parent_run,
        child_run_id=child_run_id,
        recipe=recipe,
        kickoff_path=paths["child_kickoff"],
        child_workspace=paths["child_workspace"],
        depth=depth_int,
        authority_level=authority,
        allow_child_spawn=allow_flag,
    )

    lines: list[str] = [
        f"spawn_id={spawn_id}",
        f"parent_run_id={parent_run}",
        f"child_run_id={child_run_id}",
        f"child_workspace={paths['child_workspace']}",
        f"child_kickoff={paths['child_kickoff']}",
        f"depth={depth_int}",
        f"allow_child_spawn={allow_flag}",
    ]

    if int(no_execute):
        lines.append("spawn_status=approved")
        return SpawnResult(lines=lines, exit_code=0, spawn_id=spawn_id)

    ro.mo_recursive_mark_spawn(child_run_id, "running")
    ro.mo_recursive_emit_event(
        child_run_id, parent_run, "child.started",
        json.dumps({"workspace": paths["child_workspace"]}),
    )

    overrides = {
        "MINI_ORK_HOME": child_home,
        "MINI_ORK_DB": child_db,
        "MINI_ORK_RUN_ID": child_run_id,
        "MINI_ORK_PARENT_RUN_ID": parent_run,
        "MINI_ORK_ALLOW_CHILD_SPAWN": str(allow_flag),
    }
    if transport.isolated:
        # The AMBIENT host env crosses only via the allowlist — never
        # `{**os.environ}`: a dropped key fails loud (the child cannot auth),
        # where a leaked host secret would fail silent (cloud-swarm.md P5). The
        # run contract in `overrides` is injected deliberately and always rides
        # (it is `MINI_ORK_*`, which the `MO_*` allowlist would not carry).
        from mini_ork.runtime.backends._workspace_env import container_env

        child_env = {**container_env(os.environ), **overrides}
        cli = (os.environ.get(ENV_SANDBOX_CLI) or "").strip() or _DEFAULT_SANDBOX_CLI
    else:
        child_env = {**os.environ, **overrides}
        cli = os.path.join(resolved_root, "bin", "mini-ork")
    cli_argv = _child_argv(cli, recipe, kickoff_arg)

    try:
        if transport.workspace is None:
            proc: Any = subprocess.run(cli_argv, cwd=child_cwd, env=child_env,
                                       capture_output=True, text=True)
        else:
            proc = _launch_child(transport.workspace, argv=cli_argv, env=child_env,
                                 cwd=child_cwd, timeout=_child_timeout())
    except Exception:
        # A dead backend must not wedge the spawn row in "running" (the
        # recursive caps count live spawns): record the failure, then re-raise
        # so the misconfiguration stays loud.
        ro.mo_recursive_mark_spawn(child_run_id, "failed")
        ro.mo_recursive_emit_event(
            child_run_id, parent_run, "child.failed",
            json.dumps({"error": "launch_failed"}),
        )
        raise
    child_exit = proc.returncode
    _persist_child_log(resolved_home, child_run_id, cli_argv, proc)

    if child_exit == 0:
        ro.mo_recursive_mark_spawn(child_run_id, "completed")
        ro.mo_recursive_emit_event(
            child_run_id, parent_run, "child.completed",
            json.dumps({"exit_code": 0}),
        )
        lines.append("spawn_status=completed")
    else:
        ro.mo_recursive_mark_spawn(child_run_id, "failed")
        ro.mo_recursive_emit_event(
            child_run_id, parent_run, "child.failed",
            json.dumps({"exit_code": child_exit}),
        )
        lines.append("spawn_status=failed")

    return SpawnResult(lines=lines, exit_code=child_exit, spawn_id=spawn_id,
                       child_exit_code=child_exit)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — prints the same lines bash prints, exits with the same code.

    On `--help`: prints USAGE to stdout, exits 0.
    On validation errors: prints to stderr, exits 2.
    On success: prints result lines to stdout, exits 0 (or child's exit code).
    """
    if argv is None:
        argv = sys.argv[1:]

    try:
        opts = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    try:
        result = spawn(
            parent_run=opts["parent_run"],
            kickoff=opts["kickoff"],
            child_run=opts["child_run"],
            depth=opts["depth"],
            authority=opts["authority"],
            allow_child_spawn=opts["allow_child_spawn"],
            no_execute=opts["no_execute"],
            recipe=opts["recipe"],
        )
    except (ValueError, FileNotFoundError) as exc:
        msg = str(exc)
        sys.stderr.write(f"{msg}\n")
        # bash's exit-2 phrasing: missing kickoff file or state.db both emit
        # usage to stderr and exit 2.
        if "not found" in msg or "is required" in msg:
            sys.stderr.write(USAGE)
            return 2
        return 1

    for line in result.lines:
        print(line)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())