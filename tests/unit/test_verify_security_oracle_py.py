"""Unit tests for the paired exploit-and-functional security oracle.

Hermetic: every git fixture lives in ``tmp_path``, no network, and the
git-dependent tests skip when git is unavailable.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.verify import security_oracle as so

ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "recipes" / "code-fix" / "verifiers" / "security_oracle.py"

BUG = "def add(a, b):\n    return a - b\n"
FIX = "def add(a, b):\n    return a + b\n"
EXPLOIT = "from calc import add\n\n\ndef test_exploit():\n    assert add(1, 2) == 3\n"
FUNC = "from calc import add\n\n\ndef test_functional():\n    assert add(0, 0) == 0\n"

CHECK_NAMES = {
    "exploit-fails-on-base",
    "exploit-passes-on-head",
    "functional-passes-on-base",
    "functional-passes-on-head",
}

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not available"
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    )


def build_repo(tmp_path, base_exploit=EXPLOIT, base_extra=None):
    """A buggy base commit and a fixed head commit in ``tmp_path``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "calc.py").write_text(BUG, encoding="utf-8")
    if base_exploit is not None:
        (repo / "test_exploit.py").write_text(base_exploit, encoding="utf-8")
    for name, text in (base_extra or {}).items():
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "calc.py").write_text(FIX, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo


def _checks_by_name(checks):
    return {check.name: check for check in checks}


def _verifier_env(tmp_path, repo, extra):
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir(exist_ok=True)
    env = {
        key: value for key, value in os.environ.items()
        if not (key.startswith("MO_") or key.startswith("MINI_ORK_"))
    }
    env["PYTHONPATH"] = str(ROOT)
    env["MO_TARGET_CWD"] = str(repo)
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MO_SECURITY_ORACLE_TIMEOUT"] = "120"
    env.update(extra)
    return env


def run_verifier(tmp_path, repo, extra):
    proc = subprocess.run(
        [sys.executable, str(VERIFIER)],
        cwd=str(repo), env=_verifier_env(tmp_path, repo, extra),
        capture_output=True, text=True, timeout=300,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, "no JSON envelope on stdout (stderr=%r)" % proc.stderr[-400:]
    assert len(lines) == 1, "expected single-line JSON, got %d lines" % len(lines)
    return proc.returncode, json.loads(lines[0])


# ---------------------------------------------------------- relevance


@pytest.mark.parametrize("label", ["1", "true", "TRUE", "yes", "Yes"])
def test_is_security_relevant_truthy_label(label):
    assert so.is_security_relevant([], label) is True


def test_is_security_relevant_paths():
    assert so.is_security_relevant(["src/auth/login.py"], "") is True
    assert so.is_security_relevant(["src/AUTH/login.py"], "") is True
    assert so.is_security_relevant(["src/calculator.py"], "") is False
    assert so.is_security_relevant(["src/calculator.py"], "0") is False


def test_is_security_relevant_does_not_match_a_word_prefix():
    # ``auth``/``token`` inside a longer word are not security code; matching
    # them only buys a pair of pytest runs against the wrong files.
    assert so.is_security_relevant(["docs/authors.md"], "") is False
    assert so.is_security_relevant(["src/tokenizer.py"], "") is False
    assert so.is_security_relevant(["src/credentials.py"], "") is True
    assert so.is_security_relevant(["src/authn/handler.py"], "") is True
    assert so.is_security_relevant(["src/session_store.py"], "") is True


# ---------------------------------------------------------- exploit_tests


def test_exploit_tests_env_value_wins():
    assert so.exploit_tests("/nonexistent", "a.py b.py") == ["a.py", "b.py"]
    assert so.exploit_tests("/nonexistent", "a.py\ta.py  b.py") == ["a.py", "b.py"]
    assert so.exploit_tests("/nonexistent", "") == []


def test_exploit_tests_reads_run_dir_file(tmp_path):
    (tmp_path / "exploit-tests").write_text(
        "t1.py\n\n# comment\nt2.py\nt1.py\n   \n", encoding="utf-8")
    assert so.exploit_tests(str(tmp_path), "") == ["t1.py", "t2.py"]


# ---------------------------------------------------------- evaluate


@requires_git
def test_evaluate_pass_pair(tmp_path):
    repo = build_repo(tmp_path)
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        ["test_exploit.py"], "")
    assert status == "pass"
    assert reason == ""
    by_name = _checks_by_name(checks)
    assert set(by_name) == CHECK_NAMES
    for name in ("exploit-fails-on-base", "exploit-passes-on-head"):
        assert by_name[name].passed is True and by_name[name].gating is True
    for name in ("functional-passes-on-base", "functional-passes-on-head"):
        assert by_name[name].passed is True and by_name[name].gating is False
        assert by_name[name].detail == (
            "no functional command configured "
            "(MO_SECURITY_ORACLE_FUNCTIONAL_CMD / MINI_ORK_TEST_CMD)")


@requires_git
def test_evaluate_reverted_head_is_refuted(tmp_path):
    repo = build_repo(tmp_path)
    (repo / "calc.py").write_text(BUG, encoding="utf-8")  # bug back, uncommitted
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        ["test_exploit.py"], "")
    assert status == "fail"
    by_name = _checks_by_name(checks)
    assert by_name["exploit-fails-on-base"].passed is True
    assert by_name["exploit-passes-on-head"].passed is False
    assert "exploit-passes-on-head" in reason
    assert "1 of 2" in reason


@requires_git
def test_evaluate_functional_green_only_patch_is_rejected(tmp_path):
    repo = build_repo(tmp_path)
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1", [], "")
    assert status == "fail"
    assert checks == []
    assert "exploit" in reason.lower()


@requires_git
def test_evaluate_functional_command_must_pass_on_both_trees(tmp_path):
    repo = build_repo(tmp_path)
    cmd = '"%s" -c "import sys; sys.exit(3)"' % sys.executable
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        ["test_exploit.py"], cmd)
    assert status == "fail"
    by_name = _checks_by_name(checks)
    for name in ("functional-passes-on-base", "functional-passes-on-head"):
        assert by_name[name].passed is False and by_name[name].gating is True
        assert name in reason
    assert "2 of 4" in reason


@requires_git
def test_evaluate_removes_copied_exploit_before_functional_run(tmp_path):
    repo = build_repo(tmp_path, base_exploit=None,
                      base_extra={"test_functional.py": FUNC})
    (repo / "test_exploit.py").write_text(EXPLOIT, encoding="utf-8")
    cmd = '"%s" -m pytest -q' % sys.executable
    status, checks, _ = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        ["test_exploit.py"], cmd)
    by_name = _checks_by_name(checks)
    assert by_name["functional-passes-on-base"].passed is True
    assert status == "pass"


# ---------------------------------------------------------- unmeasured


def test_evaluate_sensitive_path_without_exploit_is_vacuous(tmp_path):
    status, checks, reason = so.evaluate(
        str(tmp_path), "", ["src/auth/login.py"], "", [], "")
    assert status == "vacuous"
    assert checks == []
    assert "did not run" in reason


def test_evaluate_no_base_revision_abstains(tmp_path):
    status, checks, reason = so.evaluate(
        str(tmp_path), "", ["src/auth/login.py"], "1", ["test_exploit.py"], "")
    assert status == "abstain"
    assert checks == []
    assert reason == "no base revision to measure the exploit against"


@requires_git
def test_evaluate_missing_exploit_abstains(tmp_path):
    repo = build_repo(tmp_path)
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        ["missing.py"], "")
    assert status == "abstain"
    assert checks == []
    assert "missing.py" in reason


@requires_git
@requires_git
def test_evaluate_exploit_that_passes_on_base_is_refuted(tmp_path):
    # A green base means the exploit does not discriminate: it proves nothing
    # about the fix, so the oracle must not certify it.
    repo = build_repo(tmp_path)
    (repo / "test_exploit.py").write_text(
        "def test_always_green():\n    assert True\n", encoding="utf-8")
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        ["test_exploit.py"], "")
    assert status == "fail"
    by_name = _checks_by_name(checks)
    assert by_name["exploit-fails-on-base"].passed is False
    assert "exploit-fails-on-base" in reason


@requires_git
def test_evaluate_collection_error_on_base_abstains(tmp_path):
    # pytest exits 2 when a test module cannot even be imported. That is a
    # runner problem, not a reproduced bug: reading it as "the exploit fails on
    # base" would certify a fix on the strength of an ImportError.
    repo = build_repo(tmp_path)
    (repo / "test_exploit.py").write_text(
        "import missing_module_xyz\n\ndef test_x():\n    assert True\n",
        encoding="utf-8")
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        ["test_exploit.py"], "")
    assert status == "abstain"
    assert "did not run on the base tree" in reason
    assert [check.name for check in checks] == ["exploit-fails-on-base"]
    assert checks[0].passed is False


@requires_git
def test_evaluate_exploit_outside_the_tree_abstains(tmp_path):
    repo = build_repo(tmp_path)
    outside = tmp_path / "outside_exploit.py"
    outside.write_text(EXPLOIT, encoding="utf-8")
    status, checks, reason = so.evaluate(
        str(repo), "HEAD~1", ["src/auth/login.py"], "1",
        [str(outside)], "")
    assert status == "abstain"
    assert checks == []
    assert "outside the tree" in reason


@requires_git
def test_copy_and_restore_keeps_a_file_the_base_already_tracks(tmp_path):
    root = tmp_path / "head"
    dest = tmp_path / "base"
    root.mkdir()
    dest.mkdir()
    (root / "test_exploit.py").write_text(EXPLOIT, encoding="utf-8")
    (dest / "test_exploit.py").write_text("# base had its own copy\n",
                                          encoding="utf-8")
    copied = so._copy_exploits(root, str(dest), [Path("test_exploit.py")])
    assert (dest / "test_exploit.py").read_text(encoding="utf-8") == EXPLOIT
    so._restore_copied(copied)
    assert (dest / "test_exploit.py").read_text(encoding="utf-8") == (
        "# base had its own copy\n")


@requires_git
def test_functional_timeout_is_unmeasured_not_a_refutation(tmp_path):
    checks, abstain = so._functional_checks(
        str(tmp_path), str(tmp_path), "sleep 5", 1)
    assert abstain
    assert [check.name for check in checks] == ["functional-passes-on-base"]
    assert checks[0].passed is False


@requires_git
def test_functional_command_runs_through_a_shell(tmp_path):
    # The ordinary MINI_ORK_TEST_CMD shape is a shell command; shlex.split
    # would turn ``&&`` into an argument and read the syntax error as a
    # functional failure.
    checks, abstain = so._functional_checks(
        str(tmp_path), str(tmp_path), "true && true", 30)
    assert abstain == ""
    assert [check.passed for check in checks] == [True, True]


@requires_git
def test_evaluate_cleans_up_the_base_worktree(tmp_path):
    repo = build_repo(tmp_path)
    so.evaluate(str(repo), "HEAD~1", ["src/auth/login.py"], "1",
                ["test_exploit.py"], "")
    listing = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    entries = [line for line in listing.splitlines() if line.startswith("worktree ")]
    assert len(entries) == 1, "base worktree leaked: %r" % entries


# ---------------------------------------------------------- envelope


@requires_git
def test_envelope_pass_pair(tmp_path):
    repo = build_repo(tmp_path)
    rc, payload = run_verifier(tmp_path, repo, {
        "MO_SECURITY_FIX": "1",
        "MINI_ORK_EXPLOIT_TESTS": "test_exploit.py",
        "MINI_ORK_TOUCHED_FILES": "calc.py",
        "MO_GOAL_SCOPED_BASE": "HEAD~1",
    })
    assert rc == 0
    assert payload["verifier"] == "security_oracle"
    assert payload["verdict"] == "pass"
    assert payload["pass"] is True
    assert set(payload["checks_run"]) == CHECK_NAMES
    assert set(payload["checked_criteria"]) == CHECK_NAMES
    assert payload["failed_checks"] == []
    assert payload["findings"] == []
    assert len(payload["checks"]) == 4


@requires_git
def test_envelope_reverted_head_fails(tmp_path):
    repo = build_repo(tmp_path)
    (repo / "calc.py").write_text(BUG, encoding="utf-8")
    rc, payload = run_verifier(tmp_path, repo, {
        "MO_SECURITY_FIX": "1",
        "MINI_ORK_EXPLOIT_TESTS": "test_exploit.py",
        "MINI_ORK_TOUCHED_FILES": "calc.py",
        "MO_GOAL_SCOPED_BASE": "HEAD~1",
    })
    assert rc == 1
    assert payload["verdict"] == "fail"
    assert payload["pass"] is False
    assert payload["failed_checks"] == ["exploit-passes-on-head"]


@requires_git
def test_envelope_vacuous_exits_without_blocking(tmp_path):
    repo = build_repo(tmp_path)
    rc, payload = run_verifier(tmp_path, repo, {
        "MINI_ORK_TOUCHED_FILES": "calc.py",
    })
    assert rc == 0
    assert payload["verdict"] == "vacuous"
    # ``status: unverified`` is the executor's ABSTAIN_RC contract: a node that
    # emits it marks the run abstained and withholds the publish. A vacuous
    # verdict is the ordinary outcome of every non-security change, so it must
    # NOT carry it — only a real abstention may.
    assert "status" not in payload
    assert payload["pass"] is True
    assert payload["checks_run"] == []
    # One non-gating check, so the level gate sees an examination that found
    # nothing rather than an empty envelope it cannot read.
    assert [c["name"] for c in payload["checks"]] == ["security-oracle"]
    assert payload["checks"][0]["gating"] is False
    # The declared-skip key keeps a vacuous verdict from minting a level.
    assert payload["error_summary"]


@requires_git
def test_envelope_labelled_without_exploit_fails(tmp_path):
    repo = build_repo(tmp_path)
    rc, payload = run_verifier(tmp_path, repo, {
        "MO_SECURITY_FIX": "1",
        "MINI_ORK_TOUCHED_FILES": "calc.py",
        "MO_GOAL_SCOPED_BASE": "HEAD~1",
    })
    assert rc == 1
    assert payload["verdict"] == "fail"
    assert payload["pass"] is False
    assert payload["checks_run"] == []
    assert "exploit" in json.dumps(payload).lower()


@requires_git
def test_envelope_abstain_is_unverified_toolchain(tmp_path):
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    _git(repo, "init", "-q")  # no commits: no base revision exists
    (repo / "test_exploit.py").write_text(EXPLOIT, encoding="utf-8")
    rc, payload = run_verifier(tmp_path, repo, {
        "MO_SECURITY_FIX": "1",
        "MINI_ORK_EXPLOIT_TESTS": "test_exploit.py",
        "MINI_ORK_TOUCHED_FILES": "test_exploit.py",
    })
    assert rc == 0
    assert payload["verdict"] == "abstain"
    assert payload["status"] == "unverified"
    assert payload["failure_kind"] == "toolchain"
    assert payload["pass"] is False
    assert payload["checks_run"] == []
