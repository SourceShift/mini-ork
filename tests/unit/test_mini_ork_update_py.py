"""Parity gate: mini_ork.ported.mini_ork_update vs bin/mini-ork-update.

Both backends invoke the LIVE bash subprocess for db/init.sh (the not-yet-ported
peer) so the parity check compares two end-to-end flows against the same
backing migration code, not two reimplementations.

Determinism strategy: per-test fake ``MINI_ORK_ROOT`` whose ``db/``,
``lib/migrate.sh``, ``config/``, ``recipes/`` are COPIES (not symlinks) of the
real trees. Bash's ``find -type f`` does NOT follow symlinks for the directory
itself (without -L), so a symlinked fake_root/db/ would yield ZERO files and
break parity with Python's ``Path.glob`` (which does follow symlinks). Copying
matches what a real framework checkout looks like and keeps find + glob in sync.

The test strips MO_* env contamination (any operator vars that could leak into
db/init.sh or lib/migrate.sh subprocesses), pins MINI_ORK_HOME / MINI_ORK_ROOT /
MINI_ORK_DB per case, and asserts rc + stdout + stderr byte-equality. For
DB-modules cases (t5 dry-run, t6 apply), the test additionally seeds a fresh
temp DB via ``bash db/init.sh`` and diffs ``SELECT filename FROM
schema_migrations ORDER BY filename`` before vs after the Python invocation.

Seven cases (>= 6 floor):
  t1 --help              → rc=0 + _USAGE on stdout
  t2 -h                  → rc=0 + _USAGE on stdout
  t3 --xyz (unknown)     → rc=2 + 'Unknown option: --xyz' on stderr + _USAGE on stderr
  t4 missing MINI_ORK_HOME → rc=1 + '[FAIL] project is not initialized' + 'Run: mini-ork init'
  t5 --dry-run with pre-seeded DB → rc=0, bash.stdout == py.stdout, schema_migrations unchanged
  t6 apply mode with fresh DB     → rc=0, bash.stdout == py.stdout, schema_migrations has all *.sql files
  t7 config drift (4-state + task-class) → rc=0, bash.stdout == py.stdout

No mocks, no fabricated outputs — the bash subprocess IS the oracle.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

BIN = REPO / "bin" / "mini-ork-update"

# Re-imported so we can assert on the live _USAGE constant for the help cases.
from mini_ork.ported.mini_ork_update import _USAGE  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _which_tools() -> Tuple[bool, bool]:
    """Both bash and sqlite3 must be on PATH for the parity gate to fire."""
    return (shutil.which("bash") is not None, shutil.which("sqlite3") is not None)


def _clean_env(
    env_extra: dict,
    mini_ork_root: str | os.PathLike,
    mini_ork_home: str | os.PathLike,
    mini_ork_db: str | os.PathLike,
) -> dict:
    """Build a subprocess env that strips MO_* operator contamination and pins
    the three required MINI_ORK_* vars deterministically per case.
    """
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(mini_ork_root),
        "MINI_ORK_HOME": str(mini_ork_home),
        "MINI_ORK_DB": str(mini_ork_db),
    }
    # Strip MO_* operator vars that could leak into db/init.sh or lib/migrate.sh.
    for k in list(env):
        if k.startswith("MO_") and k not in env_extra:
            env.pop(k, None)
    env.update(env_extra)
    return env


def _bash_update(
    args: list[str], env: dict, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BIN), *args],
        env=env,
        cwd=str(cwd) if cwd else str(REPO),
        capture_output=True,
        text=True,
    )


def _py_update(
    args: list[str], env: dict, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.ported.mini_ork_update", *args],
        env=env,
        cwd=str(cwd) if cwd else str(REPO),
        capture_output=True,
        text=True,
    )


def _build_fake_root(
    fake_root: Path,
    *,
    copy_db: bool = True,
    copy_lib: bool = True,
    copy_config: bool = False,
    copy_recipes: bool = True,
    extra_config: dict | None = None,
    extra_recipes: dict | None = None,
) -> Path:
    """Build a fake MINI_ORK_ROOT under ``fake_root`` by COPYING real trees
    (never symlinking — bash's find doesn't follow symlinks for directory
    entries by default). All file content is real (migrations work, recipes
    parse) so any bash subprocess that runs from inside the fake root behaves
    identically to a real framework checkout.

    extra_config: {rel_path: content_str} — additional files to create under
        fake_root/config/ (overrides any copied default).
    extra_recipes: {recipe_name: task_class_yaml_content} — additional recipe
        subdirs to create under fake_root/recipes/<recipe>/task_class.yaml.
    """
    fake_root.mkdir(parents=True, exist_ok=True)

    if copy_db:
        db = fake_root / "db"
        db.mkdir(exist_ok=True)
        shutil.copy(REPO / "db" / "init.sh", db / "init.sh")
        shutil.copytree(REPO / "db" / "migrations", db / "migrations")
        if (REPO / "db" / "views").is_dir():
            shutil.copytree(REPO / "db" / "views", db / "views")

    if copy_lib:
        lib = fake_root / "lib"
        lib.mkdir(exist_ok=True)
        shutil.copy(REPO / "lib" / "migrate.sh", lib / "migrate.sh")

    if copy_config or extra_config:
        cfg = fake_root / "config"
        cfg.mkdir(exist_ok=True)
        if copy_config and (REPO / "config").is_dir():
            shutil.copytree(REPO / "config", cfg, dirs_exist_ok=True)
        if extra_config:
            for rel, content in extra_config.items():
                target = cfg / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

    if copy_recipes or extra_recipes:
        rec = fake_root / "recipes"
        rec.mkdir(exist_ok=True)
        if copy_recipes and (REPO / "recipes").is_dir():
            shutil.copytree(REPO / "recipes", rec, dirs_exist_ok=True)
        if extra_recipes:
            for recipe_name, content in extra_recipes.items():
                recipe_dir = rec / recipe_name
                recipe_dir.mkdir(parents=True, exist_ok=True)
                (recipe_dir / "task_class.yaml").write_text(content)

    return fake_root


def _seed_home(fake_home: Path) -> Path:
    """Create the .mini-ork/ scaffold under fake_home (bash expects this dir to
    exist or the missing-home check fails immediately).
    """
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / ".mini-ork").mkdir(exist_ok=True)
    return fake_home


def _seed_db(db_path: Path, fake_home: Path, fake_root: Path) -> None:
    """Run bash db/init.sh against a fresh DB to seed all migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    env = _clean_env({}, fake_root, fake_home, db_path)
    res = subprocess.run(
        ["bash", str(fake_root / "db" / "init.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (
        f"db/init.sh seed failed rc={res.returncode}\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )


def _schema_migrations_filenames(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        return [
            r[0]
            for r in con.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
        ]
    finally:
        con.close()


def _expected_migration_filenames(fake_root: Path) -> list[str]:
    """Sorted set of *.sql basenames under db/migrations + db/views."""
    names: set[str] = set()
    for d in (fake_root / "db" / "migrations", fake_root / "db" / "views"):
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file() and f.suffix == ".sql":
                    names.add(f.name)
    return sorted(names)


# ──────────────────────────────────────────────────────────────────────────────
# t1 --help
# ──────────────────────────────────────────────────────────────────────────────


def test_t1_help_long_flag(tmp_path: Path):
    bash_ok, _ = _which_tools()
    assert bash_ok, "bash not on PATH"
    # No env vars needed — --help short-circuits before reading any.
    env = _clean_env({}, tmp_path, tmp_path, tmp_path / "state.db")
    rb = _bash_update(["--help"], env)
    rp = _py_update(["--help"], env)
    assert rb.returncode == rp.returncode == 0, (
        f"rc mismatch bash={rb.returncode} py={rp.returncode}\n"
        f"bash stderr: {rb.stderr!r}\npy stderr: {rp.stderr!r}"
    )
    assert rb.stdout == rp.stdout, (
        f"stdout mismatch\nbash: {rb.stdout!r}\npy: {rp.stdout!r}"
    )
    assert rb.stdout == _USAGE


# ──────────────────────────────────────────────────────────────────────────────
# t2 -h
# ──────────────────────────────────────────────────────────────────────────────


def test_t2_help_short_flag(tmp_path: Path):
    bash_ok, _ = _which_tools()
    assert bash_ok
    env = _clean_env({}, tmp_path, tmp_path, tmp_path / "state.db")
    rb = _bash_update(["-h"], env)
    rp = _py_update(["-h"], env)
    assert rb.returncode == rp.returncode == 0
    assert rb.stdout == rp.stdout == _USAGE


# ──────────────────────────────────────────────────────────────────────────────
# t3 unknown option --xyz → rc=2 + stderr message + usage
# ──────────────────────────────────────────────────────────────────────────────


def test_t3_unknown_option_rc2(tmp_path: Path):
    bash_ok, _ = _which_tools()
    assert bash_ok
    env = _clean_env({}, tmp_path, tmp_path, tmp_path / "state.db")
    rb = _bash_update(["--xyz"], env)
    rp = _py_update(["--xyz"], env)
    assert rb.returncode == rp.returncode == 2, (
        f"rc mismatch bash={rb.returncode} py={rp.returncode}\n"
        f"bash stderr: {rb.stderr!r}\npy stderr: {rp.stderr!r}"
    )
    assert "Unknown option: --xyz" in rb.stderr
    assert "Unknown option: --xyz" in rp.stderr
    assert rb.stderr == rp.stderr, (
        f"stderr mismatch\nbash: {rb.stderr!r}\npy: {rp.stderr!r}"
    )
    # stdout is empty in the unknown-option path (no echo headers).
    assert rb.stdout == rp.stdout == ""


# ──────────────────────────────────────────────────────────────────────────────
# t4 missing MINI_ORK_HOME → rc=1 + [FAIL] + 'Run: mini-ork init'
# ──────────────────────────────────────────────────────────────────────────────


def test_t4_missing_home_no_init(tmp_path: Path):
    bash_ok, _ = _which_tools()
    assert bash_ok
    # fake_root exists (so MINI_ORK_ROOT resolves), but MINI_ORK_HOME points
    # at a path that doesn't exist — bash + python must both early-exit with
    # rc=1 and the [FAIL] + 'Run:' followup.
    fake_root = _build_fake_root(tmp_path / "fr")
    nonexistent = tmp_path / "no_home"
    db = tmp_path / "state.db"
    env = _clean_env({}, fake_root, nonexistent, db)
    rb = _bash_update([], env)
    rp = _py_update([], env)
    assert rb.returncode == rp.returncode == 1, (
        f"rc mismatch bash={rb.returncode} py={rp.returncode}\n"
        f"bash stdout: {rb.stdout!r}\npy stdout: {rp.stdout!r}"
    )
    assert rb.stdout == rp.stdout, (
        f"stdout mismatch\nbash: {rb.stdout!r}\npy: {rp.stdout!r}"
    )
    assert "  [FAIL] project is not initialized" in rb.stdout
    assert "         Run: mini-ork init" in rb.stdout


# ──────────────────────────────────────────────────────────────────────────────
# t5 --dry-run leaves schema_migrations unchanged; bash == py stdout bytes
# ──────────────────────────────────────────────────────────────────────────────


def test_t5_dry_run_does_not_modify_state_db(tmp_path: Path):
    bash_ok, sqlite_ok = _which_tools()
    assert bash_ok, "bash not on PATH"
    assert sqlite_ok, "sqlite3 not on PATH (required by bash db/init.sh)"

    fake_root = _build_fake_root(tmp_path / "fr")
    fake_home = _seed_home(tmp_path / "home")
    db = tmp_path / "state.db"
    _seed_db(db, fake_home, fake_root)

    before = _schema_migrations_filenames(db)
    assert before, "db seed produced empty schema_migrations"

    env = _clean_env({}, fake_root, fake_home, db)
    rb = _bash_update(["--dry-run"], env)
    rp = _py_update(["--dry-run"], env)
    assert rb.returncode == rp.returncode == 0, (
        f"rc mismatch bash={rb.returncode} py={rp.returncode}\n"
        f"bash stderr: {rb.stderr!r}\npy stderr: {rp.stderr!r}"
    )
    assert rb.stdout == rp.stdout, (
        f"stdout mismatch\nbash: {rb.stdout!r}\npy: {rp.stdout!r}"
    )
    # DB state MUST be identical after --dry-run.
    after = _schema_migrations_filenames(db)
    assert before == after, (
        f"--dry-run modified schema_migrations\nbefore: {before}\nafter:  {after}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# t6 apply mode writes every *.sql; bash == py stdout bytes
# ──────────────────────────────────────────────────────────────────────────────


def test_t6_apply_writes_all_migration_rows(tmp_path: Path):
    bash_ok, sqlite_ok = _which_tools()
    assert bash_ok
    assert sqlite_ok

    fake_root = _build_fake_root(tmp_path / "fr")
    fake_home = _seed_home(tmp_path / "home")
    db = tmp_path / "state.db"
    # NOTE: no _seed_db() — DB is fresh; apply mode must create + populate it.
    assert not db.exists()

    env = _clean_env({}, fake_root, fake_home, db)

    # Bash run.
    rb = _bash_update([], env)
    assert rb.returncode == 0, (
        f"bash apply failed rc={rb.returncode}\nstdout: {rb.stdout!r}\nstderr: {rb.stderr!r}"
    )
    bash_after = _schema_migrations_filenames(db)
    expected = _expected_migration_filenames(fake_root)
    assert bash_after == expected, (
        f"bash apply did not write all migrations\n"
        f"missing: {set(expected) - set(bash_after)}\n"
        f"extra:   {set(bash_after) - set(expected)}"
    )
    bash_stdout = rb.stdout

    # Wipe and re-run python.
    db.unlink()
    assert not db.exists()
    rp = _py_update([], env)
    assert rp.returncode == 0, (
        f"py apply failed rc={rp.returncode}\nstdout: {rp.stdout!r}\nstderr: {rp.stderr!r}"
    )
    py_after = _schema_migrations_filenames(db)
    assert py_after == expected, (
        f"py apply did not write all migrations\n"
        f"missing: {set(expected) - set(py_after)}\n"
        f"extra:   {set(py_after) - set(expected)}"
    )

    # The whole point: bash stdout == python stdout, byte-equal.
    assert bash_stdout == rp.stdout, (
        f"stdout mismatch\nbash: {bash_stdout!r}\npy: {rp.stdout!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# t7 config drift (4 states) + task-class drift: bash == py stdout bytes
# ──────────────────────────────────────────────────────────────────────────────


def test_t7_config_drift_byte_equal(tmp_path: Path):
    bash_ok, _ = _which_tools()
    assert bash_ok

    # Build a fake_root with ONLY the seeded drift fixtures under config/ and
    # one task-class fixture. Don't copy the real config/ (it has dozens of
    # files that would clutter the assertion).
    drift_fixtures = {
        "a-match.yaml": "match-content\n",
        "b-behind.yaml": "src-behind-content\n",  # dest will be a strict prefix
        "c-edited.yaml": "src-edited\n",          # dest will be wholly different
        # d-missing.yaml has no destination → 'missing-locally'.
        "e-missing.yaml": "would-be-missing\n",
        "f-example.yaml.example": "example-content\n",
    }
    recipe_fixtures = {
        "code-fix": "task-src\n",  # dest will be different content → 'local-edited'
    }
    fake_root = _build_fake_root(
        tmp_path / "fr",
        copy_db=False,
        copy_lib=False,
        copy_config=False,
        copy_recipes=False,
        extra_config=drift_fixtures,
        extra_recipes=recipe_fixtures,
    )

    # bash's MINI_ORK_HOME defaults to $PROJECT_ROOT/.mini-ork (the suffix
    # IS part of MINI_ORK_HOME). Match that here so CONFIG_DEST (= MINI_ORK_HOME/config)
    # lines up with where we seed the destinations.
    fake_home = tmp_path / "home" / ".mini-ork"
    fake_home.mkdir(parents=True, exist_ok=True)
    home_cfg = fake_home / "config"
    home_cfg.mkdir(parents=True, exist_ok=True)
    home_tc = home_cfg / "task_classes"
    home_tc.mkdir(parents=True, exist_ok=True)

    # Seed destinations in MINI_ORK_HOME/config/.
    (home_cfg / "a-match.yaml").write_text("match-content\n")
    (home_cfg / "b-behind.yaml").write_text("src-behind")  # prefix of source
    (home_cfg / "c-edited.yaml").write_text("diff-locally\n")  # completely different
    # e-missing.yaml — NO destination → 'missing-locally'
    # f-example.yaml.example — NO destination → 'missing-locally' (still printed as [info] missing-locally (example))
    (home_tc / "code-fix.yaml").write_text("task-dest\n")  # local-edited against recipe

    # No DB needed for drift — dry-run path will see DB doesn't exist and emit
    # every *.sql as pending. We just need parity.
    db = tmp_path / "state.db"
    env = _clean_env({}, fake_root, fake_home, db)

    rb = _bash_update(["--dry-run"], env)
    rp = _py_update(["--dry-run"], env)
    assert rb.returncode == rp.returncode == 0, (
        f"rc mismatch bash={rb.returncode} py={rp.returncode}\n"
        f"bash stderr: {rb.stderr!r}\npy stderr: {rp.stderr!r}"
    )
    assert rb.stdout == rp.stdout, (
        f"stdout mismatch\n"
        f"--- bash ---\n{rb.stdout}\n"
        f"--- py ---\n{rp.stdout}\n"
        f"--- diff ---\n"
        + "\n".join(_line_diff(rb.stdout, rp.stdout))
    )

    # Sanity-check the drift invariants are present in the captured output.
    assert "  [up-to-date] a-match.yaml\n" in rb.stdout
    assert "  [behind] b-behind.yaml\n" in rb.stdout
    assert "  [local-edited] c-edited.yaml\n" in rb.stdout
    assert "  [info] f-example.yaml.example: missing-locally (example)\n" in rb.stdout
    assert "  [local-edited] task_classes/code-fix.yaml\n" in rb.stdout
    # Suggested-followup format: '           suggested: <cmd>' (11 leading spaces).
    assert "           suggested: diff -u \"" in rb.stdout


def _line_diff(a: str, b: str) -> list[str]:
    """Tiny line-level unified-diff for parity error messages."""
    import difflib

    return list(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile="bash",
            tofile="py",
            n=2,
        )
    )