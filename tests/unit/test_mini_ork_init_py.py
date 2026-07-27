"""Unit tests: ``mini_ork.cli.init`` (bash parity halves removed; formerly vs ``bin/mini-ork-init``).

Each test runs the Python port in a Python subprocess against a fresh
project tree and asserts stdout, filesystem side effects, and ``state.db``
content semantically.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


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


def _run_py(project_root: Path, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    code = (
        "from pathlib import Path\n"
        "import sys\n"
        "from mini_ork.cli.init import mini_ork_init\n"
        f"sys.stdout.write(mini_ork_init(Path.cwd(), {str(REPO_ROOT)!r}))\n"
    )
    return subprocess.run(
        [os.environ.get("PYTHON", "python"), "-c", code],
        cwd=str(project_root),
        env=_env(env_overrides),
        capture_output=True,
        text=True,
    )


def _project(tmp_path: Path, name: str) -> Path:
    py_project = tmp_path / f"{name}_py"
    py_project.mkdir()
    return py_project


def _assert_ok(proc: subprocess.CompletedProcess, label: str) -> None:
    assert proc.returncode == 0, (
        f"{label} failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def _table_names(db: Path) -> list[str]:
    from mini_ork.stores.db_open import mo_sqlite

    rows = mo_sqlite(
        str(db),
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )[1:]
    return [row[0] for row in rows]


def test_fresh_init_state_db(tmp_path):
    py_project = _project(tmp_path, "fresh_db")

    py = _run_py(py_project)

    _assert_ok(py, "python")
    db = py_project / ".mini-ork" / "state.db"
    assert db.is_file()
    tables = _table_names(db)
    # full migration graph applied (48+ migrations → well over 40 tables)
    assert len(tables) > 40
    for expected in ("epics", "task_runs", "execution_traces"):
        assert expected in tables


def test_fresh_init_stdout(tmp_path):
    py_project = _project(tmp_path, "fresh_stdout")

    py = _run_py(py_project)

    _assert_ok(py, "python")
    # banner + [OK] lines mention the project tree
    assert str(py_project) in py.stdout
    assert "[OK]" in py.stdout


def test_idempotent_rerun_stdout(tmp_path):
    py_project = _project(tmp_path, "rerun")

    first_py = _run_py(py_project)
    py = _run_py(py_project)

    _assert_ok(first_py, "first python")
    _assert_ok(py, "python")
    assert "already exists" in py.stdout
    assert "already present" in py.stdout


def test_mini_ork_home_override(tmp_path):
    py_project = _project(tmp_path, "home_override")
    py_home = tmp_path / "custom_py_home"

    py = _run_py(py_project, {"MINI_ORK_HOME": str(py_home)})

    _assert_ok(py, "python")
    assert py_home.is_dir()
    assert not (py_project / ".mini-ork").exists()
    assert (py_home / "state.db").is_file()


def test_existing_gitignore_preserved(tmp_path):
    py_project = _project(tmp_path, "gitignore_existing")
    user_text = "dist-local/\n.keep\n"
    (py_project / ".gitignore").write_text(user_text, encoding="utf-8")

    py = _run_py(py_project)

    _assert_ok(py, "python")
    py_lines = (py_project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert py_lines[:2] == ["dist-local/", ".keep"]
    # generated block appended after the user's lines
    assert ".mini-ork/*" in py_lines


def test_absent_gitignore_created(tmp_path):
    py_project = _project(tmp_path, "gitignore_absent")

    py = _run_py(py_project)

    _assert_ok(py, "python")
    py_lines = [line for line in (py_project / ".gitignore").read_text(encoding="utf-8").splitlines() if line]
    assert py_lines == [
        "# mini-ork generated state (the engine pointer below is committed)",
        ".mini-ork/*",
        "!.mini-ork/engine",
        ".mini-ork/state.db",
        ".mini-ork/runs/",
        ".mini-ork/INBOX/",
        ".mini-ork/secrets/",
        ".mini-ork/locks/",
    ]


def test_secrets_dir_chmod_700(tmp_path):
    py_project = _project(tmp_path, "chmod")

    py = _run_py(py_project)

    _assert_ok(py, "python")
    assert stat.S_IMODE((py_project / ".mini-ork" / "secrets").stat().st_mode) == 0o700


def test_task_class_seeding_count(tmp_path):
    py_project = _project(tmp_path, "task_classes")

    py = _run_py(py_project)

    _assert_ok(py, "python")
    py_count = len(list((py_project / ".mini-ork" / "config" / "task_classes").iterdir()))
    # The absolute count grows as recipes are added (was 27; ~34 now), so
    # pin the sanity floor only.
    assert py_count >= 27  # at least the historical baseline seeded
