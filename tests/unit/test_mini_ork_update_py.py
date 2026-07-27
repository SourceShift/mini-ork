"""Unit tests: mini_ork.cli.update (bash parity halves removed; formerly vs bin/mini-ork-update).

The Python backend runs the native Python port (``mini_ork.stores.migrate.init_db``).
Determinism strategy: per-test fake ``MINI_ORK_ROOT`` whose ``db/``,
``config/``, ``recipes/`` are COPIES (not symlinks) of the real trees.

The test strips MO_* env contamination, pins MINI_ORK_HOME / MINI_ORK_ROOT /
MINI_ORK_DB per case, and asserts rc + stdout + stderr. For DB-modules cases
(t5 dry-run, t6 apply), the test additionally seeds a fresh temp DB via
``bash db/init.sh`` and diffs ``SELECT filename FROM schema_migrations ORDER
BY filename`` before vs after the Python invocation.

Seven cases:
  t1 --help              → rc=0 + _USAGE on stdout
  t2 -h                  → rc=0 + _USAGE on stdout
  t3 --xyz (unknown)     → rc=2 + 'Unknown option: --xyz' on stderr
  t4 missing MINI_ORK_HOME → rc=1 + '[FAIL] project is not initialized' + 'Run: mini-ork init'
  t5 --dry-run with pre-seeded DB → rc=0, schema_migrations unchanged
  t6 apply mode with fresh DB     → rc=0, schema_migrations has all *.sql files
  t7 config drift (4-state + task-class) → rc=0, drift lines present
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Re-imported so we can assert on the live _USAGE constant for the help cases.
from mini_ork.cli.update import _USAGE  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


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
    # Strip MO_* operator vars that could leak into db/init.sh.
    for k in list(env):
        if k.startswith("MO_") and k not in env_extra:
            env.pop(k, None)
    env.update(env_extra)
    return env


def _py_update(
    args: list[str], env: dict, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.update", *args],
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
    (never symlinking). All file content is real (migrations work, recipes
    parse).

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
    """Create the .mini-ork/ scaffold under fake_home."""
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
    # No env vars needed — --help short-circuits before reading any.
    env = _clean_env({}, tmp_path, tmp_path, tmp_path / "state.db")
    rp = _py_update(["--help"], env)
    assert rp.returncode == 0, f"py stderr: {rp.stderr!r}"
    assert rp.stdout == _USAGE


# ──────────────────────────────────────────────────────────────────────────────
# t2 -h
# ──────────────────────────────────────────────────────────────────────────────


def test_t2_help_short_flag(tmp_path: Path):
    env = _clean_env({}, tmp_path, tmp_path, tmp_path / "state.db")
    rp = _py_update(["-h"], env)
    assert rp.returncode == 0
    assert rp.stdout == _USAGE


# ──────────────────────────────────────────────────────────────────────────────
# t3 unknown option --xyz → rc=2 + stderr message
# ──────────────────────────────────────────────────────────────────────────────


def test_t3_unknown_option_rc2(tmp_path: Path):
    env = _clean_env({}, tmp_path, tmp_path, tmp_path / "state.db")
    rp = _py_update(["--xyz"], env)
    assert rp.returncode == 2, f"py stderr: {rp.stderr!r}"
    assert "Unknown option: --xyz" in rp.stderr
    # stdout is empty in the unknown-option path (no echo headers).
    assert rp.stdout == ""


# ──────────────────────────────────────────────────────────────────────────────
# t4 missing MINI_ORK_HOME → rc=1 + [FAIL] + 'Run: mini-ork init'
# ──────────────────────────────────────────────────────────────────────────────


def test_t4_missing_home_no_init(tmp_path: Path):
    # fake_root exists (so MINI_ORK_ROOT resolves), but MINI_ORK_HOME points
    # at a path that doesn't exist — early-exit rc=1 with [FAIL] + 'Run:'.
    fake_root = _build_fake_root(tmp_path / "fr")
    nonexistent = tmp_path / "no_home"
    db = tmp_path / "state.db"
    env = _clean_env({}, fake_root, nonexistent, db)
    rp = _py_update([], env)
    assert rp.returncode == 1, f"py stdout: {rp.stdout!r}"
    assert "  [FAIL] project is not initialized" in rp.stdout
    assert "         Run: mini-ork init" in rp.stdout


# ──────────────────────────────────────────────────────────────────────────────
# t5 --dry-run leaves schema_migrations unchanged
# ──────────────────────────────────────────────────────────────────────────────


def test_t5_dry_run_does_not_modify_state_db(tmp_path: Path):
    if not shutil.which("sqlite3"):
        import pytest
        pytest.skip("sqlite3 not on PATH (required by db/init.sh)")

    fake_root = _build_fake_root(tmp_path / "fr")
    fake_home = _seed_home(tmp_path / "home")
    db = tmp_path / "state.db"
    _seed_db(db, fake_home, fake_root)

    before = _schema_migrations_filenames(db)
    assert before, "db seed produced empty schema_migrations"

    env = _clean_env({}, fake_root, fake_home, db)
    rp = _py_update(["--dry-run"], env)
    assert rp.returncode == 0, f"py stderr: {rp.stderr!r}"
    # DB state MUST be identical after --dry-run.
    after = _schema_migrations_filenames(db)
    assert before == after, (
        f"--dry-run modified schema_migrations\nbefore: {before}\nafter:  {after}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# t6 apply mode writes every *.sql
# ──────────────────────────────────────────────────────────────────────────────


def test_t6_apply_writes_all_migration_rows(tmp_path: Path):
    if not shutil.which("sqlite3"):
        import pytest
        pytest.skip("sqlite3 not on PATH")

    fake_root = _build_fake_root(tmp_path / "fr")
    fake_home = _seed_home(tmp_path / "home")
    db = tmp_path / "state.db"
    # NOTE: no _seed_db() — DB is fresh; apply mode must create + populate it.
    assert not db.exists()

    env = _clean_env({}, fake_root, fake_home, db)

    rp = _py_update([], env)
    assert rp.returncode == 0, (
        f"py apply failed rc={rp.returncode}\nstdout: {rp.stdout!r}\nstderr: {rp.stderr!r}"
    )
    py_after = _schema_migrations_filenames(db)
    expected = _expected_migration_filenames(fake_root)
    assert py_after == expected, (
        f"py apply did not write all migrations\n"
        f"missing: {set(expected) - set(py_after)}\n"
        f"extra:   {set(py_after) - set(expected)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# t7 config drift (4 states) + task-class drift
# ──────────────────────────────────────────────────────────────────────────────


def test_t7_config_drift(tmp_path: Path):
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

    # MINI_ORK_HOME is the .mini-ork dir itself (CONFIG_DEST = MINI_ORK_HOME/config).
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
    # f-example.yaml.example — NO destination → 'missing-locally (example)'
    (home_tc / "code-fix.yaml").write_text("task-dest\n")  # local-edited against recipe

    db = tmp_path / "state.db"
    env = _clean_env({}, fake_root, fake_home, db)

    rp = _py_update(["--dry-run"], env)
    assert rp.returncode == 0, f"py stderr: {rp.stderr!r}"

    # The drift invariants are present in the captured output.
    assert "  [up-to-date] a-match.yaml\n" in rp.stdout
    assert "  [behind] b-behind.yaml\n" in rp.stdout
    assert "  [local-edited] c-edited.yaml\n" in rp.stdout
    assert "  [info] f-example.yaml.example: missing-locally (example)\n" in rp.stdout
    assert "  [local-edited] task_classes/code-fix.yaml\n" in rp.stdout
    # Suggested-followup format: '           suggested: <cmd>' (11 leading spaces).
    assert "           suggested: diff -u \"" in rp.stdout
