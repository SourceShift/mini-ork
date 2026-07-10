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

DB writes are delegated to `mini_ork.ported.recursive_orchestration` (the
port of `lib/recursive_orchestration.sh`). The Python port does NOT inline
SQLite writes; it composes the recursive_orchestration helpers so that DB
parity vs bash is enforced once at that layer's parity test.

The execute path shells out to `bin/mini-ork` subprocess (the real CLI), so
the port does not duplicate the child run engine. The `MINI_ORK_ROOT` env var
locates the CLI binary; falls back to the directory above this file.

Usage:
    from mini_ork.ported import mini_ork_spawn
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
from pathlib import Path

from mini_ork.ported import recursive_orchestration as ro

__all__ = [
    "SpawnResult",
    "parse_args",
    "compute_child_run_id",
    "resolve_depth",
    "prepare_child_workspace",
    "spawn",
    "main",
    "USAGE",
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

    paths = prepare_child_workspace(resolved_home, parent_run, child_run_id, kickoff)

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

    child_env = {
        **os.environ,
        "MINI_ORK_HOME": resolved_home,
        "MINI_ORK_DB": resolved_db,
        "MINI_ORK_RUN_ID": child_run_id,
        "MINI_ORK_PARENT_RUN_ID": parent_run,
        "MINI_ORK_ALLOW_CHILD_SPAWN": str(allow_flag),
    }
    cli = os.path.join(resolved_root, "bin", "mini-ork")
    # bash bin/mini-ork-spawn:110-129 — WITH a recipe: `run <recipe> <kickoff>`;
    # WITHOUT: `run <kickoff>`. The kickoff arg is mandatory in both; dropping it
    # (the old `run <recipe>` with no kickoff) left the child run with nothing to
    # plan → it failed and never wrote plan.json.
    if recipe:
        cli_argv = [cli, "run", recipe, paths["child_kickoff"]]
    else:
        cli_argv = [cli, "run", paths["child_kickoff"]]
    proc = subprocess.run(cli_argv, cwd=paths["child_workspace"], env=child_env,
                          capture_output=True, text=True)
    child_exit = proc.returncode

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