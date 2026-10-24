"""RSI Tier-0 #6: the recursive-self-improve gates must check Python, not just shell.

The bash runtime was removed in 2026-07 and nearly every patch is Python now, but
`verifiers/no-regression.py` only ran `bash -n` on changed `*.sh bin/*` files and
`verifiers/self-tests-pass.py` ran its suite without a timeout.

These tests drive the real verifier scripts as subprocesses against throwaway git
repos (no network, no credentials, no writes outside tmp_path):

    MINI_ORK_RUN_DIR=<tmp run dir>
    MINI_ORK_SELF_IMPROVE_WORKTREE=<repo>
    MINI_ORK_DB=<nonexistent path>
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NO_REGRESSION = (
    REPO_ROOT / "recipes" / "recursive-self-improve" / "verifiers" / "no-regression.py"
)
SELF_TESTS = (
    REPO_ROOT / "recipes" / "recursive-self-improve" / "verifiers" / "self-tests-pass.py"
)

EXISTING_KEYS = [
    "verifier",
    "pass",
    "evidence_path",
    "syntax_failures",
    "implementer_outcome",
    "benchmark_summary",
    "benchmark_regression",
    "benchmark_inconclusive",
]


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _make_repo(base):
    repo = base / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Gate Test")
    _git(repo, "config", "user.email", "gate@example.invalid")
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _commit(repo, rel, content):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"add {rel}")


def _clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("MINI_ORK") or k.startswith("MO_"))
    }


def _parse_result(proc):
    text = proc.stdout.decode("utf-8", "replace")
    for cand in (text, *text.splitlines()):
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and "pass" in obj:
            return obj
    raise AssertionError(
        f"no result JSON with 'pass' on stdout; rc={proc.returncode} "
        f"stdout={text!r} stderr={proc.stderr.decode('utf-8', 'replace')!r}"
    )


def _run_no_regression(repo, run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    env = _clean_env()
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MINI_ORK_SELF_IMPROVE_WORKTREE"] = str(repo)
    env["MINI_ORK_DB"] = str(run_dir / "no-such-db.sqlite3")
    proc = subprocess.run(
        [sys.executable, str(NO_REGRESSION)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    return _parse_result(proc)


# ── no-regression.py: Python syntax arm ──────────────────────────────────────


def test_untracked_python_syntax_error_fails(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "bad.py").write_text("def f(:\n", encoding="utf-8")
    result = _run_no_regression(repo, tmp_path / "run")
    assert result["pass"] is False, result
    assert "pkg/bad.py" in result["py_syntax_failures"], result


def test_valid_untracked_python_passes(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    result = _run_no_regression(repo, tmp_path / "run")
    assert result["pass"] is True, result
    assert result["py_syntax_failures"] == [], result


def test_modified_tracked_python_file_is_checked(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "pkg/m.py", "x = 1\n")
    (repo / "pkg" / "m.py").write_text("x = (\n", encoding="utf-8")
    result = _run_no_regression(repo, tmp_path / "run")
    assert result["pass"] is False, result
    assert "pkg/m.py" in result["py_syntax_failures"], result


def test_python_shebang_bin_file_skips_bash_n(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "bin/tool", '#!/usr/bin/env python3\nprint("hi")\n')
    (repo / "bin" / "tool").write_text('#!/usr/bin/env python3\nprint("ho")\n', encoding="utf-8")
    result = _run_no_regression(repo, tmp_path / "run")
    assert result["pass"] is True, result
    assert result["syntax_failures"] == [], result
    assert result["py_syntax_failures"] == [], result


def test_shell_shebang_bin_file_still_gets_bash_n(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "bin/run", "#!/bin/bash\necho ok\n")
    (repo / "bin" / "run").write_text("#!/bin/bash\nif then\n", encoding="utf-8")
    result = _run_no_regression(repo, tmp_path / "run")
    assert result["pass"] is False, result
    assert "bin/run" in result["syntax_failures"], result
    assert "bin/run" not in result["py_syntax_failures"], result


def test_sidecar_matches_stdout_pass_value(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "bad.py").write_text("def f(:\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    result = _run_no_regression(repo, run_dir)
    sidecar = run_dir / "verifier-result-no-regression.json"
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["pass"] == result["pass"]


def test_result_json_keeps_existing_keys(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "ok.py").write_text("x = 2\n", encoding="utf-8")
    result = _run_no_regression(repo, tmp_path / "run")
    assert isinstance(result["py_syntax_failures"], list), result
    assert [k for k in EXISTING_KEYS if k not in result] == [], result


def test_evidence_records_python_counts_after_shell_counts(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "bad.py").write_text("def f(:\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _run_no_regression(repo, run_dir)
    evlog = (run_dir / "verifier-no-regression.log").read_text(encoding="utf-8", errors="replace")
    assert "changed_sh_files=" in evlog, evlog
    assert "changed_py_files=1 py_syntax_failures=1" in evlog, evlog
    assert evlog.index("changed_sh_files=") < evlog.index("changed_py_files="), evlog


def test_evidence_clean_repo_counts_zero(tmp_path):
    """A repo with no changed Python files writes `changed_py_files=0 ...=0`."""
    repo = _make_repo(tmp_path / "clean")
    run_dir = tmp_path / "run"
    result = _run_no_regression(repo, run_dir)
    assert result["pass"] is True, result
    evlog = (run_dir / "verifier-no-regression.log").read_text(encoding="utf-8", errors="replace")
    assert "changed_py_files=0 py_syntax_failures=0" in evlog, evlog


# ── self-tests-pass.py: timeout + error count ────────────────────────────────


def _py_cmd(code):
    return "{} -c {}".format(shlex.quote(sys.executable), shlex.quote(code))


def _run_self_tests(repo, run_dir, test_cmd=None, timeout_s=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    env = _clean_env()
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MINI_ORK_SELF_IMPROVE_WORKTREE"] = str(repo)
    if test_cmd is not None:
        env["MINI_ORK_SELF_IMPROVE_TEST_CMD"] = test_cmd
    if timeout_s is not None:
        env["MINI_ORK_SELF_IMPROVE_TEST_TIMEOUT_S"] = timeout_s
    proc = subprocess.run(
        [sys.executable, str(SELF_TESTS)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    return _parse_result(proc)


def test_hung_suite_times_out(tmp_path):
    repo = _make_repo(tmp_path)
    run_dir = tmp_path / "run"
    result = _run_self_tests(
        repo, run_dir, test_cmd=_py_cmd("import time; time.sleep(15)"), timeout_s="2"
    )
    assert result["pass"] is False, result
    assert result["timed_out"] is True, result
    assert result["pytest_rc"] is None, result
    evlog = (run_dir / "verifier-self-tests-pass.log").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "tests timed out after 2s" in evlog, evlog


def test_bad_timeout_value_fails_closed(tmp_path):
    repo = _make_repo(tmp_path)
    result = _run_self_tests(repo, tmp_path / "run", test_cmd=_py_cmd("print('1 passed')"), timeout_s="abc")
    assert result["pass"] is False, result
    assert result["reason"] == "bad MINI_ORK_SELF_IMPROVE_TEST_TIMEOUT_S", result


def test_normal_run_gains_timed_out_false(tmp_path):
    repo = _make_repo(tmp_path)
    result = _run_self_tests(repo, tmp_path / "run", test_cmd=_py_cmd("print('1 passed')"))
    assert result["timed_out"] is False, result
    assert result["pass"] is True, result


def test_errors_counted_once(tmp_path):
    repo = _make_repo(tmp_path)
    result = _run_self_tests(
        repo, tmp_path / "run", test_cmd=_py_cmd("print('2 passed, 3 errors in 0.02s')")
    )
    assert result["tests_failed"] == 3, result
    assert result["tests_passed"] == 2, result
    assert result["tests_collected"] == 5, result
    assert result["pass"] is True, result
