"""Unit tests for the code-fix verifier's abstention contract.

The bug (f4110a93, verification stack ON) made every non-pytest test command
(jest / npm / a bash gate) read as a red suite: the replay delta-gate abstains
for anything without "pytest", `emit_unverified()` writes `pass: false`, and the
reviewer's APPROVE rule 2 required `pass: true` — so a green-but-unverifiable
suite was rolled back. These tests pin the new `status: "unverified"` /
`suite_green` discriminants on the abstention path so an abstention is never
mistaken for a failure. The plain pass/fail paths (`emit()`) intentionally do
NOT carry `status`: an out-of-scope test (`test_suite_adequacy.py:441`) pins
`emit()`'s exact key set, so `status` lives only on the abstention payload.

The verifier auto-detects a test command via `MINI_ORK_TEST_CMD`; here we pass
a non-pytest shell command explicitly to exercise the abstention path, exactly
like a jest/npm/bash gate would.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"
REVIEWER = REPO_ROOT / "recipes" / "code-fix" / "prompts" / "reviewer.md"

GREEN_CMD = "bash -c 'exit 0'"
RED_CMD = "bash -c 'exit 1'"


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(parent: Path) -> Path:
    """Stand up a throwaway git repo. A non-pytest command needs no test files."""
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_verifier(
    repo: Path,
    tmp_path: Path,
    *,
    cmd: str,
    replay: str = "1",
    baseline: str = "1",
    run_id: str,
) -> tuple[int, dict]:
    """Invoke the verifier on `repo`. Returns (exit_code, parsed JSON envelope)."""
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_home)
    env["MINI_ORK_RUN_ID"] = run_id
    env["MO_CODEFIX_REPLAY"] = replay
    env["MO_TEST_BASELINE"] = baseline
    env["MO_SUITE_ADEQUACY"] = "0"  # these tests are about the replay abstention, not adequacy
    env["MINI_ORK_TEST_CMD"] = cmd
    # The verifier late-imports `mini_ork.certify`; that import needs the
    # worktree root on sys.path because the verifier is run as a script.
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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


# ── 1. green non-pytest → abstention, not failure ──────────────────────────
def test_green_non_pytest_emits_abstention(tmp_path):
    """A green non-pytest command must emit `status: unverified` + `suite_green:
    true`, keep `pass: false`, and exit 0 (abstention, not failure). Because
    the replay instrument does not apply to a non-pytest runner, the payload
    also carries `replay_applicable: false`."""
    repo = _make_repo(tmp_path)

    rc, out = _run_verifier(repo, tmp_path, cmd=GREEN_CMD, run_id="abstain-green")

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["status"] == "unverified", out
    assert out["suite_green"] is True, out
    assert out["replay_unverified"] is True, out
    assert out["replay_applicable"] is False, out


# ── 2. red non-pytest → fail ───────────────────────────────────────────────
def test_red_non_pytest_emits_fail(tmp_path):
    """A red non-pytest command is a real failure: `pass: false`, exit 1, and
    no `suite_green` key (that key is abstention-only)."""
    repo = _make_repo(tmp_path)

    rc, out = _run_verifier(repo, tmp_path, cmd=RED_CMD, baseline="0", run_id="fail-red")

    assert rc == 1, out
    assert out["pass"] is False, out
    assert "suite_green" not in out, out


# ── 3. replay opt-out, green → pass ────────────────────────────────────────
def test_green_opt_out_emits_pass(tmp_path):
    """With the delta-gate replay off, a green command certifies: `pass: true`."""
    repo = _make_repo(tmp_path)

    rc, out = _run_verifier(repo, tmp_path, cmd=GREEN_CMD, replay="0", run_id="optout-pass")

    assert rc == 0, out
    assert out["pass"] is True, out


# ── 4. prompt guard: reviewer.md teaches the abstention rule ────────────────
def test_reviewer_prompt_teaches_abstention_rule():
    """The reviewer prompt must name the new discriminant in the APPROVE rules
    so an abstention is never read as a red suite again."""
    text = REVIEWER.read_text(encoding="utf-8")
    assert 'status: "unverified"' in text
    assert "suite_green" in text
    # Both literals must appear inside the APPROVE block (before REQUEST_CHANGES).
    approve = text.split("### REQUEST_CHANGES")[0]
    assert 'status: "unverified"' in approve
    assert "suite_green: true" in approve
