"""Unit tests for the code-fix verifier's test-weakening guard.

Nothing stops a patch from deleting a failing test, adding a skip marker, or
making an existing test vanish — its suite then goes "green". The guard reads
``git diff`` against HEAD BEFORE running the suite and refuses with
``tests weakened: …`` on a violation.

Each test stands up a throwaway git repo, runs
``recipes/code-fix/verifiers/test.py`` against it, and asserts the JSON
verdict + exit code. The suite must not run when the guard fires; the payload
records ``post_rc: ""`` for that case.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

TEST_CMD = "python3 -m pytest -p no:cacheprovider"


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(parent: Path, files: dict[str, str]) -> Path:
    """Stand up a throwaway git repo with the given files (initial commit)."""
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_verifier(repo: Path, tmp_path: Path, *, run_id: str,
                  extra_env: dict[str, str] | None = None) -> tuple[int, dict]:
    """Invoke the verifier on ``repo``. Returns ``(exit_code, JSON envelope)``."""
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_home)
    env["MINI_ORK_RUN_ID"] = run_id
    env["MO_CODEFIX_REPLAY"] = "0"
    env["MO_TEST_BASELINE"] = "1"
    env["MO_SUITE_ADEQUACY"] = "0"
    env["MINI_ORK_TEST_CMD"] = TEST_CMD
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(VERIFIER)], cwd=repo, env=env,
        capture_output=True, text=True, timeout=120,
    )
    last = proc.stdout.strip().splitlines()
    assert last, (
        f"verifier produced no JSON: rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return proc.returncode, json.loads(last[-1])


# ── 6. deleting an existing test file → refuse, suite not run ───────────────
def test_deleting_existing_test_file_is_weakening(tmp_path):
    repo = _make_repo(tmp_path, {
        "tests/test_x.py": "def test_x():\n    assert True\n",
    })
    (repo / "tests" / "test_x.py").unlink()

    rc, out = _run_verifier(repo, tmp_path, run_id="tamper-delete")

    assert rc == 1, out
    assert out["pass"] is False, out
    assert "tests weakened" in out["error_summary"], out
    assert "test_x" in out["error_summary"], out
    assert out["post_rc"] == "", out  # suite never ran


# ── 7a. adding a pytest skip marker to an existing test → refuse ────────────
def test_adding_pytest_skip_to_existing_test_is_weakening(tmp_path):
    repo = _make_repo(tmp_path, {
        "tests/test_x.py": "def test_x():\n    assert True\n",
    })
    (repo / "tests" / "test_x.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.skip\n"
        "def test_x():\n    assert True\n"
    )

    rc, out = _run_verifier(repo, tmp_path, run_id="tamper-pytest-skip")

    assert rc == 1, out
    assert out["pass"] is False, out
    assert "tests weakened" in out["error_summary"], out


# ── 7b. adding it.skip( to an existing *.test.ts → refuse ───────────────────
def test_adding_it_skip_to_existing_ts_test_is_weakening(tmp_path):
    repo = _make_repo(tmp_path, {
        "a.test.ts": 'it("works", () => {});\n',
    })
    (repo / "a.test.ts").write_text(
        'it("works", () => {});\n'
        'it.skip("skipped", () => {});\n'
    )

    rc, out = _run_verifier(repo, tmp_path, run_id="tamper-ts-skip")

    assert rc == 1, out
    assert out["pass"] is False, out
    assert "tests weakened" in out["error_summary"], out
    assert "a.test.ts" in out["error_summary"], out


# ── 8. a NEW test file containing pytest.skip( is allowed ───────────────────
def test_new_untracked_test_with_skip_is_allowed(tmp_path):
    repo = _make_repo(tmp_path, {
        "tests/test_x.py": "def test_x():\n    assert True\n",
    })
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_y.py").write_text(
        "import pytest\n\n\n"
        "def test_y():\n"
        "    pytest.skip(\"not ready\")\n"
    )

    rc, out = _run_verifier(repo, tmp_path, run_id="tamper-new-skip-allowed")

    assert rc == 0, out
    assert out["pass"] is True, out


# ── 9. MO_ALLOW_TEST_CHANGES=1 disables the guard ───────────────────────────
def test_mo_allow_test_changes_disables_guard(tmp_path):
    repo = _make_repo(tmp_path, {
        "tests/test_x.py": "def test_x():\n    assert True\n",
    })
    (repo / "tests" / "test_x.py").unlink()

    rc, out = _run_verifier(
        repo, tmp_path, run_id="tamper-allow-changes",
        extra_env={"MO_ALLOW_TEST_CHANGES": "1"},
    )

    assert rc == 0, out
    assert "tests weakened" not in out["error_summary"], out


# ── source-level pin: the guard is wired before the suite runs ──────────────
def test_tamper_guard_runs_before_suite():
    src = VERIFIER.read_text(encoding="utf-8")
    main_start = src.index("def main():")
    tamper_call = src.index("_check_tests_tamper(", main_start)
    suite_call = src.index("run_suite(LOG_PATH", main_start)
    assert tamper_call < suite_call
    assert "MO_ALLOW_TEST_CHANGES" in src
