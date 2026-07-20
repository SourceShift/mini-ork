"""mini_ork/ported/mini_ork_update — Python port of bin/mini-ork-update.

Bash-side contract this port mirrors (verbatim from bin/mini-ork-update):
  - Inputs (positional argv): --dry-run, --pull, --help/-h. Unknown option → rc=2
    + 'Unknown option: X' + usage on stderr.
  - Env resolution (in priority order):
      MINI_ORK_ROOT  : framework checkout; defaults to 2 parents up from the
                       script (bin/.. → repo root). For this Python port the
                       equivalent is parents[2] of mini_ork/ported/.
      MINI_ORK_HOME  : project's .mini-ork/; defaults to <cwd>/.mini-ork.
      MINI_ORK_DB    : state.db path; defaults to <MINI_ORK_HOME>/state.db.
  - Subprocess delegation (strangler-fig — bash is single source of truth):
      * git pull sequence  → `git -C "$ROOT" …` via subprocess.run (3 calls)
      * sqlite3 schema check → bash `sqlite3 file:DB?mode=ro | grep -qx 1`
      * migration apply    → `bash db/init.sh` (this database initializer
        remains an intentional subprocess boundary)
  - Output bytes MUST match bash. The [OK]/[WARN]/[FAIL] status helpers, the
    per-line spacing ('  [OK]   ' vs '  [WARN] ' / '  [FAIL] ' — three spaces
    vs one space), the '           suggested: …' followups (11 leading spaces),
    and the trailing summary block are all part of the byte-equality surface
    that the parity test asserts.
  - Exit codes: 0 success (FAIL count == 0), 1 failure (missing home, dirty
    git checkout, db/init.sh non-zero, or summary FAIL>0), 2 unknown option.

status: alpha
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def _dq(s: str) -> str:
    """Always-wrap in double quotes, matching bash's `\"$path\"` idiom.

    Bash's drift suggested-followups unconditionally wrap path args in
    literal `"..."` (even when the path has no spaces). shlex.quote would
    only quote when needed → bytes diverge for paths-without-spaces. Mirror
    bash exactly: byte-equal at the cost of breaking on paths containing
    literal `"` characters (bash has the same hazard).
    """
    return f'"{s}"'


_USAGE = (
    "Usage: mini-ork update [--dry-run] [--pull] [--help]\n"
    "\n"
    "Apply pending mini-ork migrations to the current project's .mini-ork/state.db\n"
    "and report drift between shipped config templates and local .mini-ork/config.\n"
    "\n"
    "Options:\n"
    "  --dry-run   Print pending migrations and config drift without writing files\n"
    "  --pull      Run git pull --ff-only in MINI_ORK_ROOT before updating\n"
    "  --help, -h  Show this help\n"
)


@dataclass
class _Counters:
    passed: int = 0
    warned: int = 0
    failed: int = 0


def _resolve_root(mini_ork_root: Optional[str | Path]) -> Path:
    """Mirror bash: `MINI_ORK_ROOT=${MINI_ORK_ROOT:-$(cd $(dirname ${BASH_SOURCE[0]})/.. && pwd)}`.

    Bash default = 2 parents up from the bash script (bin/..). We mirror that
    as 2 parents up from THIS module (mini_ork/ported/..).
    """
    if mini_ork_root is not None:
        return Path(mini_ork_root)
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2]


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")
    _counters.passed += 1


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")
    _counters.warned += 1


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    _counters.failed += 1


_counters = _Counters()


def _reset_counters() -> None:
    """Reset module-level counters. Test-only helper."""
    global _counters
    _counters = _Counters()


def _project_root() -> Path:
    """Mirror bash: `PROJECT_ROOT="$(pwd)"` — current working directory at invocation time."""
    return Path(os.getcwd())


def _schema_has_migration(filename: str, db_path: Path) -> bool:
    """Mirror bash: `[ -f $DB ] || return 1; sqlite3 file:$DB?mode=ro "SELECT COUNT(*) FROM schema_migrations WHERE filename='$1';" 2>/dev/null | grep -qx 1`.

    Delegate to bash subprocess so error formatting (missing DB, sqlite3 not on
    PATH) is byte-identical between backends.
    """
    if not db_path.is_file():
        return False
    cmd = (
        f"sqlite3 \"file:{db_path}?mode=ro\" "
        f"\"SELECT COUNT(*) FROM schema_migrations WHERE filename='{filename}';\" "
        f"2>/dev/null | grep -qx \"1\""
    )
    res = subprocess.run(["bash", "-c", cmd], capture_output=True)
    return res.returncode == 0


def _list_pending_schema_files(dir_path: Path, label: str, db_path: Path) -> None:
    """Mirror bash: walk $dir for *.sql, print skip|pending per file based on schema_migrations."""
    if not dir_path.is_dir():
        return
    for f in sorted(dir_path.glob("*.sql")):
        if not f.is_file():
            continue
        base = f.name
        if _schema_has_migration(base, db_path):
            print(f"  [skip] {base} - already applied")
        else:
            print(f"  [pending {label}] {base}")


def _template_status(src: Path, dest: Path) -> str:
    """Mirror bash: cmp -s / prefix check.

    Four states (mutually exclusive):
      missing-locally : dest does not exist
      up-to-date      : cmp -s src dest → equal bytes
      behind          : dest_len < src_len AND dest == first dest_len bytes of src
      local-edited    : anything else
    """
    if not dest.is_file():
        return "missing-locally"
    src_bytes = src.read_bytes()
    dest_bytes = dest.read_bytes()
    if src_bytes == dest_bytes:
        return "up-to-date"
    dest_len = len(dest_bytes)
    if dest_len < len(src_bytes) and src_bytes[:dest_len] == dest_bytes:
        return "behind"
    return "local-edited"


def _do_pull(mini_ork_repo: Path) -> int:
    """Mirror bash --pull block. Returns 0 on success/skip, 1 on dirty-checkout fail.

    Subprocesses each git invocation directly so the git stdout/stderr bytes
    match bash's natural output. The [WARN]/[FAIL]/[OK] + 'Inspect:' lines are
    emitted by the Python helpers.
    """
    print("--- Updating framework checkout ---")
    rc = subprocess.run(
        ["git", "-C", str(mini_ork_repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    ).returncode
    if rc != 0:
        _warn(f"MINI_ORK_ROOT is not a git checkout - skipping pull: {mini_ork_repo}")
        print("")
        return 0
    porcelain = subprocess.run(
        ["git", "-C", str(mini_ork_repo), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if porcelain.stdout.strip():
        _fail("MINI_ORK_ROOT has uncommitted changes - refusing git pull --ff-only")
        print(f"         Inspect: git -C \"{mini_ork_repo}\" status --short")
        return 1
    subprocess.run(["git", "-C", str(mini_ork_repo), "pull", "--ff-only"])
    _ok("framework checkout is up to date")
    print("")
    return 0


def _print_config_drift(config_src: Path, config_dest: Path) -> None:
    """Mirror bash: walk $CONFIG_SRC recursively, print per-file status + suggested followup."""
    for src in sorted(config_src.rglob("*")):
        if not src.is_file():
            continue
        rel = str(src.relative_to(config_src))
        dest = config_dest / rel
        status = _template_status(src, dest)
        if rel.endswith(".example"):
            print(f"  [info] {rel}: {status} (example)")
        else:
            print(f"  [{status}] {rel}")
        if status == "missing-locally":
            print(f"           suggested: mkdir -p {_dq(str(dest.parent))} && cp {_dq(str(src))} {_dq(str(dest))}")
        elif status in ("behind", "local-edited"):
            print(f"           suggested: diff -u {_dq(str(dest))} {_dq(str(src))}")


def _print_task_class_drift(mini_ork_repo: Path, config_dest: Path) -> None:
    """Mirror bash: walk recipes/*/task_class.yaml, compare against config_dest/task_classes/<recipe>.yaml."""
    recipes = mini_ork_repo / "recipes"
    if not recipes.is_dir():
        return
    task_classes_dest = config_dest / "task_classes"
    print("")
    print("--- Task class drift ---")
    for src in sorted(recipes.glob("*/task_class.yaml")):
        recipe_name = src.parent.name
        rel = f"task_classes/{recipe_name}.yaml"
        dest = task_classes_dest / f"{recipe_name}.yaml"
        status = _template_status(src, dest)
        print(f"  [{status}] {rel}")
        if status == "missing-locally":
            print(f"           suggested: mkdir -p {_dq(str(task_classes_dest))} && cp {_dq(str(src))} {_dq(str(dest))}")
        elif status in ("behind", "local-edited"):
            print(f"           suggested: diff -u {_dq(str(dest))} {_dq(str(src))}")
    _ok("task class drift report complete")


def update(argv: Optional[List[str]] = None) -> int:
    """Mirror bin/mini-ork-update. Returns the process exit code.

    Args:
        argv: CLI args (defaults to sys.argv[1:]). Test entrypoint passes a
              list to keep state isolated.
    """
    if argv is None:
        argv = sys.argv[1:]

    dry_run = False
    do_pull = False
    for arg in argv:
        if arg == "--dry-run":
            dry_run = True
        elif arg == "--pull":
            do_pull = True
        elif arg in ("--help", "-h"):
            sys.stdout.write(_USAGE)
            return 0
        else:
            sys.stderr.write(f"Unknown option: {arg}\n")
            sys.stderr.write(_USAGE)
            return 2

    mini_ork_repo = _resolve_root(os.environ.get("MINI_ORK_ROOT"))
    project_root = _project_root()
    home_env = os.environ.get("MINI_ORK_HOME")
    mini_ork_home = Path(home_env) if home_env else project_root / ".mini-ork"
    db_env = os.environ.get("MINI_ORK_DB")
    mini_ork_db = Path(db_env) if db_env else mini_ork_home / "state.db"

    print("=== mini-ork update ===")
    print(f"    project: {project_root}")
    print(f"    home:    {mini_ork_home}")
    print(f"    db:      {mini_ork_db}")
    print(f"    root:    {mini_ork_repo}")
    print("")

    if not mini_ork_home.is_dir():
        _fail(f"project is not initialized: {mini_ork_home} not found")
        print("         Run: mini-ork init")
        return 1

    if do_pull:
        rc = _do_pull(mini_ork_repo)
        if rc != 0:
            return rc

    print("--- Migrations ---")
    if dry_run:
        _list_pending_schema_files(mini_ork_repo / "db" / "migrations", "migration", mini_ork_db)
        _list_pending_schema_files(mini_ork_repo / "db" / "views", "view", mini_ork_db)
        _ok("dry-run: state.db not modified")
    else:
        init_sh = mini_ork_repo / "db" / "init.sh"
        sub_env = {**os.environ, "MINI_ORK_HOME": str(mini_ork_home), "MINI_ORK_DB": str(mini_ork_db), "MINI_ORK_ROOT": str(mini_ork_repo)}
        res = subprocess.run(
            ["bash", str(init_sh)],
            env=sub_env, capture_output=True, text=True,
        )
        # Mirror bash: `bash db/init.sh` inherits stdout/stderr to the parent
        # bash, which the test sees on its captured stdout. Re-emit so the
        # Python port's stdout contains the same lines (e.g. '[apply]   0001_…',
        # '[mini-ork init] Done. Tables: 103') byte-for-byte.
        if res.stdout:
            sys.stdout.write(res.stdout)
        if res.stderr:
            sys.stderr.write(res.stderr)
        if res.returncode == 0:
            _ok("state.db migrations applied")
        else:
            _fail("db/init.sh exited non-zero")
            return 1
    print("")

    print("--- Config drift ---")
    config_src = mini_ork_repo / "config"
    config_dest = mini_ork_home / "config"
    if not config_src.is_dir():
        _warn(f"no shipped config directory found: {config_src}")
    else:
        _print_config_drift(config_src, config_dest)
        _ok("config drift report complete")

    _print_task_class_drift(mini_ork_repo, config_dest)
    print("")

    print("=== mini-ork update summary ===")
    print(f"  OK:   {_counters.passed}")
    print(f"  WARN: {_counters.warned}")
    print(f"  FAIL: {_counters.failed}")

    return 0 if _counters.failed == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    """`python -m mini_ork.ported.mini_ork_update` shim.

    Resets module counters before delegating to update() so repeated `python -m`
    invocations in tests don't accumulate state across runs.
    """
    _reset_counters()
    return update(argv)


if __name__ == "__main__":
    sys.exit(main())
