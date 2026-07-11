"""Parity gate: ``mini_ork.ported.mini_ork_init`` vs ``bin/mini-ork-init``.

Each test invokes the LIVE bash init script via subprocess and runs the
Python port in a separate Python subprocess against an equivalent project
tree. The bash script stays the source of truth; this file only compares
stdout, filesystem side effects, and ``state.db`` row content.
"""

from __future__ import annotations

import math
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASH_INIT = REPO_ROOT / "bin" / "mini-ork-init"
FLOAT_TOL = 1e-6


def _env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("MINI_ORK_HOME", None)
    env.pop("MINI_ORK_DB", None)
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if not env.get("PYTHONPATH")
        else str(REPO_ROOT) + os.pathsep + env["PYTHONPATH"]
    )
    if overrides:
        env.update(overrides)
    return env


def _run_bash(project_root: Path, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BASH_INIT)],
        cwd=str(project_root),
        env=_env(env_overrides),
        capture_output=True,
        text=True,
    )


def _run_py(project_root: Path, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    code = (
        "from pathlib import Path\n"
        "import sys\n"
        "from mini_ork.ported.mini_ork_init import mini_ork_init\n"
        f"sys.stdout.write(mini_ork_init(Path.cwd(), {str(REPO_ROOT)!r}))\n"
    )
    return subprocess.run(
        [os.environ.get("PYTHON", "python"), "-c", code],
        cwd=str(project_root),
        env=_env(env_overrides),
        capture_output=True,
        text=True,
    )


def _project_pair(tmp_path: Path, name: str) -> tuple[Path, Path]:
    bash_project = tmp_path / f"{name}_bash"
    py_project = tmp_path / f"{name}_py"
    bash_project.mkdir()
    py_project.mkdir()
    return bash_project, py_project


def _assert_ok(proc: subprocess.CompletedProcess, label: str) -> None:
    assert proc.returncode == 0, (
        f"{label} failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def _normalize_stdout(stdout: str, project: Path, home: Path) -> str:
    normalized = stdout.replace(str(project), "<PROJECT>")
    normalized = normalized.replace(str(home), "<HOME>")
    normalized = normalized.replace(str(REPO_ROOT), "<REPO>")
    return normalized


def _db_path(project: Path, env_overrides: dict[str, str] | None = None) -> Path:
    home = Path(env_overrides["MINI_ORK_HOME"]) if env_overrides and "MINI_ORK_HOME" in env_overrides else project / ".mini-ork"
    return home / "state.db"


def _rows(db: Path, sql: str) -> list[tuple]:
    from mini_ork.ported.db_open import mo_sqlite

    return mo_sqlite(str(db), sql)[1:]


def _table_names(db: Path) -> list[str]:
    rows = _rows(
        db,
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    return [row[0] for row in rows]


def _columns(db: Path, table: str) -> list[str]:
    return [row[1] for row in _rows(db, f"PRAGMA table_info('{table}')")]


def _is_volatile_column(column: str) -> bool:
    return column in {"ts"} or column.endswith("_at") or column.endswith("_ts")


def _ordered_table_rows(db: Path, table: str) -> list[tuple]:
    cols = _columns(db, table)
    quoted = ", ".join(f'"{col}"' for col in cols)
    order_by = ", ".join(f'"{col}"' for col in cols)
    rows = _rows(db, f'SELECT {quoted} FROM "{table}" ORDER BY {order_by}')
    normalized = [
        tuple(None if _is_volatile_column(col) and cell is not None else cell for col, cell in zip(cols, row))
        for row in rows
    ]
    return sorted(normalized, key=repr)


def _assert_cell_equal(bash_cell, py_cell, ctx: str) -> None:
    if isinstance(bash_cell, float) or isinstance(py_cell, float):
        assert math.isclose(float(bash_cell), float(py_cell), abs_tol=1e-6), ctx
    else:
        assert bash_cell == py_cell, ctx


def _assert_state_db_row_parity(bash_db: Path, py_db: Path) -> None:
    bash_tables = _table_names(bash_db)
    py_tables = _table_names(py_db)
    assert bash_tables == py_tables
    for table in bash_tables:
        bash_rows = _ordered_table_rows(bash_db, table)
        py_rows = _ordered_table_rows(py_db, table)
        assert len(bash_rows) == len(py_rows), table
        for row_idx, (bash_row, py_row) in enumerate(zip(bash_rows, py_rows)):
            assert len(bash_row) == len(py_row), f"{table}[{row_idx}]"
            for col_idx, (bash_cell, py_cell) in enumerate(zip(bash_row, py_row)):
                _assert_cell_equal(
                    bash_cell,
                    py_cell,
                    f"{table}[{row_idx}][{col_idx}] bash={bash_cell!r} py={py_cell!r}",
                )


def test_fresh_init_state_db_row_parity(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "fresh_db")

    bash = _run_bash(bash_project)  # subprocess.run live bash
    py = _run_py(py_project)

    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    _assert_state_db_row_parity(
        bash_project / ".mini-ork" / "state.db",
        py_project / ".mini-ork" / "state.db",
    )


def test_fresh_init_stdout_parity(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "fresh_stdout")

    bash = _run_bash(bash_project)  # subprocess.run live bash
    py = _run_py(py_project)

    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    assert _normalize_stdout(
        bash.stdout, bash_project, bash_project / ".mini-ork"
    ) == _normalize_stdout(py.stdout, py_project, py_project / ".mini-ork")


def test_idempotent_rerun_stdout(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "rerun")

    first_bash = _run_bash(bash_project)
    first_py = _run_py(py_project)
    bash = _run_bash(bash_project)  # subprocess.run live bash
    py = _run_py(py_project)

    _assert_ok(first_bash, "first bash")
    _assert_ok(first_py, "first python")
    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    assert "already exists" in bash.stdout
    assert "already present" in bash.stdout
    assert _normalize_stdout(
        bash.stdout, bash_project, bash_project / ".mini-ork"
    ) == _normalize_stdout(py.stdout, py_project, py_project / ".mini-ork")


def test_mini_ork_home_override(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "home_override")
    bash_home = tmp_path / "custom_bash_home"
    py_home = tmp_path / "custom_py_home"

    bash = _run_bash(bash_project, {"MINI_ORK_HOME": str(bash_home)})  # subprocess.run live bash
    py = _run_py(py_project, {"MINI_ORK_HOME": str(py_home)})

    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    assert bash_home.is_dir()
    assert py_home.is_dir()
    assert not (bash_project / ".mini-ork").exists()
    assert not (py_project / ".mini-ork").exists()
    assert _db_path(bash_project, {"MINI_ORK_HOME": str(bash_home)}).is_file()
    assert _db_path(py_project, {"MINI_ORK_HOME": str(py_home)}).is_file()


def test_existing_gitignore_preserved(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "gitignore_existing")
    user_text = "dist-local/\n.keep\n"
    (bash_project / ".gitignore").write_text(user_text, encoding="utf-8")
    (py_project / ".gitignore").write_text(user_text, encoding="utf-8")

    bash = _run_bash(bash_project)  # subprocess.run live bash
    py = _run_py(py_project)

    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    bash_lines = (bash_project / ".gitignore").read_text(encoding="utf-8").splitlines()
    py_lines = (py_project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert bash_lines[:2] == ["dist-local/", ".keep"]
    assert bash_lines == py_lines


def test_absent_gitignore_created(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "gitignore_absent")

    bash = _run_bash(bash_project)  # subprocess.run live bash
    py = _run_py(py_project)

    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    bash_lines = [line for line in (bash_project / ".gitignore").read_text(encoding="utf-8").splitlines() if line]
    py_lines = [line for line in (py_project / ".gitignore").read_text(encoding="utf-8").splitlines() if line]
    assert bash_lines == [
        ".mini-ork/state.db",
        ".mini-ork/runs/",
        ".mini-ork/INBOX/",
        ".mini-ork/secrets/",
        ".mini-ork/locks/",
    ]
    assert bash_lines == py_lines


def test_secrets_dir_chmod_700(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "chmod")

    bash = _run_bash(bash_project)  # subprocess.run live bash
    py = _run_py(py_project)

    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    assert stat.S_IMODE((bash_project / ".mini-ork" / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE((py_project / ".mini-ork" / "secrets").stat().st_mode) == 0o700


def test_task_class_seeding_count(tmp_path):
    bash_project, py_project = _project_pair(tmp_path, "task_classes")

    bash = _run_bash(bash_project)  # subprocess.run live bash
    py = _run_py(py_project)

    _assert_ok(bash, "bash")
    _assert_ok(py, "python")
    bash_count = len(list((bash_project / ".mini-ork" / "config" / "task_classes").iterdir()))
    py_count = len(list((py_project / ".mini-ork" / "config" / "task_classes").iterdir()))
    assert bash_count == py_count == 27
