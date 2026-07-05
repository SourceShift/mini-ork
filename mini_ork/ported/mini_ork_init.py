"""Pure-logic port of ``bin/mini-ork-init``.

Faithful Python port of the ``.mini-ork/`` scaffolder: creates the
home directory tree, seeds ``config/task_classes/`` from every recipe's
``task_class.yaml``, copies ``agents.yaml`` and ``scope-patterns.yaml.example``
into ``config/``, runs ``db/init.sh`` to populate ``state.db``, and
appends the canonical ``.gitignore`` entries to the project root's
``.gitignore``. Idempotent on re-run.

Strangler-fig co-existence: ``bin/mini-ork-init`` is byte-identical
before and after this module exists. ``tests/unit/test_mini_ork_init_py.py``
is the parity gate that proves the port produces byte-identical stdout
and filesystem state (including ``state.db`` row content within 1e-6
float tolerance) against the live bash subprocess (no mocking).

Public API::

    from mini_ork.ported.mini_ork_init import mini_ork_init

    stdout = mini_ork_init(project_root, mini_ork_repo,
                           mini_ork_home=None)  # -> str
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path

_DEFAULT_AGENTS_YAML = """\
# mini-ork agent configuration
# Edit to customise model routing, iteration caps, and lane parallelism.

defaults:
  max_iters: 3          # max worker+review cycles per epic before escalation
  max_lanes: 4          # max parallel lanes (epics running simultaneously)

models:
  decomposer:   claude-opus-4       # parses kickoff, seeds epics
  worker:       claude-sonnet-4-5   # default implementation model
  reviewer:     claude-opus-4       # adversarial diff reviewer
  healer:       claude-sonnet-4-5   # self-heal on BDD failure
  hunter:       glm-4               # cheap parallel scan (bug-hunt mode)

# Per-complexity overrides:
complexity:
  low:    { worker: claude-sonnet-4-5 }
  medium: { worker: claude-sonnet-4-5 }
  high:   { worker: claude-opus-4     }
"""

_DEFAULT_SCOPE_PATTERNS_YAML = """\
# Scope pattern configuration — copy to scope-patterns.yaml and edit.
#
# Patterns listed under 'deny' are checked against each epic's proposed
# file writes.  If an epic touches a denied path, the lane is rejected
# before the worker runs.
#
# Use glob syntax (fnmatch).  Leading '!' negates a pattern.

deny:
  - "*.env"
  - "*.env.*"
  - ".mini-ork/**"
  - "node_modules/**"
  - "dist/**"
  - "*.db"

# Per-epic file-touch limits (0 = unlimited):
limits:
  max_files_per_epic: 20
"""

_GITIGNORE_ENTRIES = (
    ".mini-ork/state.db",
    ".mini-ork/runs/",
    ".mini-ork/INBOX/",
    ".mini-ork/secrets/",
    ".mini-ork/locks/",
)

_HOME_SUBDIRS = (
    "kickoffs",
    "INBOX",
    "runs",
    "locks",
    "secrets",
    "config",
)


def _ok(buf: StringIO, msg: str) -> None:
    """Mirror bash ``_ok`` (line 16). Note ``[OK]`` has THREE trailing spaces."""
    buf.write(f"  [OK]   {msg}\n")


def _warn(buf: StringIO, msg: str) -> None:
    """Mirror bash ``_warn`` (line 17). Note ``[WARN]`` has ONE trailing space."""
    buf.write(f"  [WARN] {msg}\n")


def _make_dirs_idempotent(buf: StringIO, paths: list[Path]) -> None:
    """Mirror bash lines 28-43: per-dir ``created X/`` vs ``X/ already exists``."""
    for path in paths:
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            _ok(buf, f"created {path.name}/")
        else:
            _ok(buf, f"{path.name}/ already exists")


def _chmod_700_safe(buf: StringIO, path: Path) -> None:
    """Mirror bash line 47: chmod 700 + warn on failure.

    Bash hardcodes the message so the port does too — the literal name
    ``secrets/`` is the canonical contract surface, not a derived one.
    """
    try:
        path.chmod(0o700)
    except OSError:
        _warn(buf, "chmod 700 secrets/ failed (filesystem doesn't support unix perms?)")


def _seed_task_classes(buf: StringIO, recipes_dir: Path, dest_dir: Path) -> None:
    """Mirror bash lines 62-74: copy each recipe's task_class.yaml into dest_dir.

    Skips recipes without a ``task_class.yaml`` (mirrors bash's
    ``[ -d ] || continue`` and ``[ -f ] || continue``). Sorted iteration
    matches bash's glob ordering so the stdout line order is stable.
    Idempotent: never overwrites an existing dest.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for recipe_dir in sorted(recipes_dir.iterdir()):
        if not recipe_dir.is_dir():
            continue
        recipe_name = recipe_dir.name
        src_yaml = recipe_dir / "task_class.yaml"
        if not src_yaml.is_file():
            continue
        dest_yaml = dest_dir / f"{recipe_name}.yaml"
        if not dest_yaml.is_file():
            shutil.copy2(str(src_yaml), str(dest_yaml))
            _ok(buf, f"task_class seeded: {recipe_name}.yaml")
        else:
            _ok(buf, f"task_class {recipe_name}.yaml already present")


def _copy_or_default(
    buf: StringIO,
    src: Path,
    dest: Path,
    default_text: str,
    msgs: dict[str, str],
) -> None:
    """Mirror bash lines 77-146 — the two near-identical copy-or-default blocks.

    Three states, three exact messages:
      - src present, dest absent  → copy + ``msgs["copied"]``
      - src present, dest present → no-op + ``msgs["already_present"]``
      - src absent,  dest absent  → write default + ``msgs["default_created"]``
      - src absent,  dest present → silent no-op (mirrors bash)
    """
    if src.is_file():
        if not dest.is_file():
            shutil.copy2(str(src), str(dest))
            _ok(buf, msgs["copied"])
        else:
            _ok(buf, msgs["already_present"])
    elif not dest.is_file():
        dest.write_text(default_text, encoding="utf-8")
        _ok(buf, msgs["default_created"])


def _run_db_init(buf: StringIO, db_init_path: Path, env: dict) -> None:
    """Mirror bash lines 154-165: invoke ``db/init.sh`` via subprocess.

    Hard-exits (SystemExit(1)) on non-zero — the load-bearing safety
    gate that prevents partial ``state.db`` migrations. On missing
    ``db/init.sh`` emits a WARN and returns (does NOT raise).
    """
    if db_init_path.is_file():
        proc = subprocess.run(
            ["bash", str(db_init_path)],
            env=env,
            cwd=str(db_init_path.parent.parent),
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            _ok(buf, "state.db initialised via db/init.sh")
        else:
            sys.stderr.write(
                "  [FAIL] db/init.sh exited non-zero — state.db was not initialized\n"
                "         Refusing to continue with an incomplete or unsafe mini-ork home.\n"
            )
            raise SystemExit(1)
    else:
        _warn(buf, "db/init.sh not found — state.db not created (run after other agents land db/)")


def _update_gitignore(buf: StringIO, gitignore_path: Path, entries: tuple[str, ...]) -> None:
    """Mirror bash lines 171-194: append entries via ``grep -qxF`` semantics.

    Whole-line exact match (after stripping newline) keeps re-runs from
    duplicating entries. Pre-existing user content is never touched.
    """
    if not gitignore_path.is_file():
        gitignore_path.touch()
        _ok(buf, "created .gitignore")

    existing = [line for line in gitignore_path.read_text(encoding="utf-8").splitlines()]
    for entry in entries:
        if entry in existing:
            _ok(buf, f".gitignore: {entry} (already present)")
        else:
            with gitignore_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n{entry}\n")
            _ok(buf, f".gitignore: added {entry}")


def mini_ork_init(
    project_root: str | Path,
    mini_ork_repo: str | Path,
    mini_ork_home: str | Path | None = None,
) -> str:
    """Scaffold ``.mini-ork/`` under ``project_root`` mirroring ``bin/mini-ork-init``.

    Returns the full stdout (banner, [OK]/[WARN] lines, summary, next-steps
    block) as a single string so the parity test can byte-compare against
    the live bash subprocess. Side-effect order is locked to match the
    bash script — reordering any step breaks the parity gate.

    Args:
        project_root:    Where the .mini-ork/ tree (or override below) lives.
        mini_ork_repo:   mini-ork repo root (provides recipes/, db/init.sh,
                          examples/ for the next-steps block). Bash derives
                          this from ``$BASH_SOURCE``; Python has no
                          equivalent so it must be passed.
        mini_ork_home:   Optional override for the ``.mini-ork/`` dir
                          location. Defaults to ``os.environ['MINI_ORK_HOME']``
                          if set, else ``{project_root}/.mini-ork``.
    """
    # Do NOT resolve() project_root — bash uses ``$(pwd)`` which preserves
    # the un-resolved cwd spelling (e.g. /tmp vs /private/tmp on macOS).
    # Resolving would diverge from bash's stdout byte-for-byte.
    project_root = Path(project_root)
    mini_ork_repo = Path(mini_ork_repo).resolve()

    if mini_ork_home is None:
        mini_ork_home = Path(
            os.environ.get("MINI_ORK_HOME", str(project_root / ".mini-ork"))
        ).resolve()
    else:
        mini_ork_home = Path(mini_ork_home).resolve()

    buf = StringIO()
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_ork_home)
    env["MINI_ORK_DB"] = str(mini_ork_home / "state.db")

    buf.write("=== mini-ork init ===\n")
    buf.write(f"    project: {project_root}\n")
    buf.write(f"    home:    {mini_ork_home}\n")
    buf.write("\n")

    # ── 1. Directory structure ────────────────────────────────────────────────
    buf.write("--- Creating .mini-ork/ structure ---\n")
    paths = [mini_ork_home] + [mini_ork_home / sub for sub in _HOME_SUBDIRS]
    _make_dirs_idempotent(buf, paths)
    _chmod_700_safe(buf, mini_ork_home / "secrets")
    buf.write("\n")

    # ── 2. Default config files ──────────────────────────────────────────────
    buf.write("--- Copying default config ---\n")
    config_src = mini_ork_repo / "config"
    config_dest = mini_ork_home / "config"

    recipes_dir = mini_ork_repo / "recipes"
    _seed_task_classes(buf, recipes_dir, config_dest / "task_classes")

    _copy_or_default(
        buf,
        src=config_src / "agents.yaml",
        dest=config_dest / "agents.yaml",
        default_text=_DEFAULT_AGENTS_YAML,
        msgs={
            "copied": "agents.yaml copied to config/",
            "already_present": "agents.yaml already present — not overwritten",
            "default_created": "agents.yaml created (default)",
        },
    )

    _copy_or_default(
        buf,
        src=config_src / "scope-patterns.yaml.example",
        dest=config_dest / "scope-patterns.yaml.example",
        default_text=_DEFAULT_SCOPE_PATTERNS_YAML,
        msgs={
            "copied": "scope-patterns.yaml.example copied to config/",
            "already_present": "scope-patterns.yaml.example already present",
            "default_created": "scope-patterns.yaml.example created",
        },
    )
    buf.write("\n")

    # ── 3. state.db ──────────────────────────────────────────────────────────
    buf.write("--- Initialising state.db ---\n")
    _run_db_init(buf, mini_ork_repo / "db" / "init.sh", env)
    buf.write("\n")

    # ── 4. .gitignore ────────────────────────────────────────────────────────
    buf.write("--- Updating .gitignore ---\n")
    _update_gitignore(buf, project_root / ".gitignore", _GITIGNORE_ENTRIES)
    buf.write("\n")

    # ── Summary + next steps ─────────────────────────────────────────────────
    buf.write(f"=== mini-ork ready in {project_root} ===\n")
    buf.write("\n")
    buf.write("Next steps:\n")
    buf.write(f"  1. Review and edit   {mini_ork_home}/config/agents.yaml\n")
    buf.write("     (model lane assignments + budget caps)\n")
    buf.write("\n")
    buf.write("  2. (Optional) seed extra task_classes:\n")
    buf.write(f"     ls {mini_ork_home}/config/task_classes/\n")
    buf.write("\n")
    buf.write("  3. Write your first kickoff:\n")
    buf.write(f"     cp {mini_ork_repo}/examples/01-hello-world/kickoff.md ./kickoff.md\n")
    buf.write("     # edit kickoff.md for your project\n")
    buf.write("\n")
    buf.write("  4. Run via the universal task loop:\n")
    buf.write("     mini-ork run code-fix ./kickoff.md\n")
    buf.write("\n")
    buf.write("  5. Inspect state:\n")
    buf.write(
        "     sqlite3 .mini-ork/state.db "
        "'SELECT id,task_class,status,verdict FROM task_runs;'\n"
    )
    buf.write("\n")

    return buf.getvalue()
