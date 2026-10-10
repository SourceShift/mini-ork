"""Unit tests for the code-fix verifier's red-base per-test decision.

The old ``BASE_RC != 0 → emit(True, "pre-existing failure")`` branch certified
any patch whose baseline was also red — including a patch that broke MORE
tests in an already-red suite. These tests pin the replacement: when post is
red AND base is red, run both sides with per-test outcomes and fail on any
regression, otherwise abstain (``red_base_unverified``) rather than pass.

Each test stands up a throwaway git repo, runs
``recipes/code-fix/verifiers/test.py`` against it with ``MO_TEST_BASELINE=1``
and ``MO_CODEFIX_REPLAY=0`` (so the red-base branch is what is exercised), and
asserts the JSON verdict + exit code.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

# `-p no:cacheprovider` keeps `.pytest_cache/` out of the candidate tree.
# We deliberately do NOT pass `-q`: the red-base parser needs the per-test
# `-v` lines, and pytest resolves conflicting verbosity by the LAST flag.
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
    env["MO_CODEFIX_REPLAY"] = "0"  # red-base branch, not the green replay
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


# ── 1. base `a` failing + `b` passing; candidate breaks `b` → regression ────
def test_red_base_regression_fails(tmp_path):
    repo = _make_repo(tmp_path, {
        "mod.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n",
        "test_a.py": "from mod import a\n\n\ndef test_a():\n    assert a() == 999\n",
        "test_b.py": "from mod import b\n\n\ndef test_b():\n    assert b() == 2\n",
    })
    # Candidate breaks `b` by a CODE change (the test file is untouched) while
    # `a` stays failing, so both sides are red and the red-base branch runs.
    (repo / "mod.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 999\n"
    )

    rc, out = _run_verifier(repo, tmp_path, run_id="redbase-regression")

    assert rc == 1, out
    assert out["pass"] is False, out
    assert "regression on a red base" in out["error_summary"], out
    assert "test_b" in out["error_summary"], out


# ── 2. base `a`+`c` failing; candidate fixes `a` only → abstain ─────────────
def test_red_base_improvement_abstains(tmp_path):
    repo = _make_repo(tmp_path, {
        "mod.py": "def a():\n    return 1\n\n\ndef c():\n    return 3\n",
        "test_a.py": "from mod import a\n\n\ndef test_a():\n    assert a() == 2\n",
        "test_c.py": "from mod import c\n\n\ndef test_c():\n    assert c() == 4\n",
    })
    # Candidate fixes `a` only; `c` still fails, so post stays red.
    (repo / "mod.py").write_text(
        "def a():\n    return 2\n\n\ndef c():\n    return 3\n"
    )

    rc, out = _run_verifier(repo, tmp_path, run_id="redbase-improve")

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["status"] == "unverified", out
    assert out["red_base_unverified"] is True, out
    assert out["suite_green"] is False, out


def test_red_base_improvement_certifies_with_knob(tmp_path):
    repo = _make_repo(tmp_path, {
        "mod.py": "def a():\n    return 1\n\n\ndef c():\n    return 3\n",
        "test_a.py": "from mod import a\n\n\ndef test_a():\n    assert a() == 2\n",
        "test_c.py": "from mod import c\n\n\ndef test_c():\n    assert c() == 4\n",
    })
    (repo / "mod.py").write_text(
        "def a():\n    return 2\n\n\ndef c():\n    return 3\n"
    )

    rc, out = _run_verifier(
        repo, tmp_path, run_id="redbase-improve-certify",
        extra_env={"MO_TEST_CERTIFY_ON_IMPROVEMENT": "1"},
    )

    assert rc == 0, out
    assert out["pass"] is True, out


# ── 3. equal outcomes on both sides → abstain, never pass ───────────────────
def test_red_base_equal_outcomes_abstain(tmp_path):
    repo = _make_repo(tmp_path, {
        "mod.py": "def a():\n    return 1\n",
        "test_a.py": "from mod import a\n\n\ndef test_a():\n    assert a() == 999\n",
    })
    # No working-tree change: the same failure on both sides.

    rc, out = _run_verifier(repo, tmp_path, run_id="redbase-equal")

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["status"] == "unverified", out
    assert out["red_base_unverified"] is True, out


# ── 4. MO_TEST_LEGACY_RED_BASE=1 restores the old blanket pass ──────────────
def test_red_base_legacy_escape_hatch_passes(tmp_path):
    repo = _make_repo(tmp_path, {
        "mod.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n",
        "test_a.py": "from mod import a\n\n\ndef test_a():\n    assert a() == 999\n",
        "test_b.py": "from mod import b\n\n\ndef test_b():\n    assert b() == 2\n",
    })
    # Same regression shape as test 1; the escape hatch must still blanket-pass.
    (repo / "mod.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 999\n"
    )

    rc, out = _run_verifier(
        repo, tmp_path, run_id="redbase-legacy",
        extra_env={"MO_TEST_LEGACY_RED_BASE": "1"},
    )

    assert rc == 0, out
    assert out["pass"] is True, out


# ── 5. an UNRUNNABLE baseline is never a red baseline ───────────────────────
#
# `BASE_RC != 0` cannot tell "the suite ran and was already red" from "the
# suite never started". The reported live failures were exactly that: post rc=1
# with base rc=127 (no node_modules in the base worktree) and rc=77 on both
# sides (jest-guard refused). Zero tests executed, and the run was certified.

RED_MOD = "def a():\n    return 1\n"
RED_TEST = "from mod import a\n\n\ndef test_a():\n    assert a() == 999\n"


def test_unrunnable_baseline_is_not_a_red_baseline(tmp_path):
    """The candidate's runner is missing in the base worktree → rc 127.

    The runner is untracked and NOT a test file, so the overlay does not carry
    it to the base — the same shape as a base worktree without node_modules.
    """
    repo = _make_repo(tmp_path, {"mod.py": RED_MOD, "test_a.py": RED_TEST})
    (repo / "runner.py").write_text(
        "import subprocess, sys\n"
        "sys.exit(subprocess.call([sys.executable, '-m', 'pytest',\n"
        "                          '-p', 'no:cacheprovider', *sys.argv[1:]]))\n"
    )

    rc, out = _run_verifier(
        repo, tmp_path, run_id="redbase-unrunnable",
        extra_env={"MINI_ORK_TEST_CMD": "python3 ./runner.py"},
    )

    # An un-attributable baseline ABSTAINS (exit 0, status unverified) — it must
    # not `emit()` (exit 1), which the executor's revise loop reads as a fixable
    # gate failure and answers with a full implementer round spent on an
    # environment collision the patch cannot fix.
    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["status"] == "unverified", out
    assert out["base_unrunnable"] is True, out
    assert "did not execute tests" in out["error_summary"], out


def test_missing_test_binary_abstains_rather_than_fails(tmp_path):
    """Both sides rc 127 — the live jest-without-node_modules shape.

    A missing runner is NOT a red baseline: it abstains (exit 0) so the gate
    neither certifies nor burns a revise round on an environment collision.
    """
    repo = _make_repo(tmp_path, {"mod.py": RED_MOD, "test_a.py": RED_TEST})

    rc, out = _run_verifier(
        repo, tmp_path, run_id="redbase-missing-bin",
        extra_env={"MINI_ORK_TEST_CMD": "./node_modules/.bin/jest --ci"},
    )

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["base_unrunnable"] is True, out
    assert "did not execute tests" in out["error_summary"], out


def test_legacy_hatch_cannot_pass_an_unrunnable_baseline(tmp_path):
    """MO_TEST_LEGACY_RED_BASE is checked AFTER the runnability classification.

    The hatch cannot blanket-PASS a baseline that never ran; the runnability
    classification abstains first (exit 0, `base_unrunnable`), never `pass:true`.
    """
    repo = _make_repo(tmp_path, {"mod.py": RED_MOD, "test_a.py": RED_TEST})

    rc, out = _run_verifier(
        repo, tmp_path, run_id="redbase-legacy-unrunnable",
        extra_env={
            "MINI_ORK_TEST_CMD": "./node_modules/.bin/jest --ci",
            "MO_TEST_LEGACY_RED_BASE": "1",
        },
    )

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["base_unrunnable"] is True, out


# ── 6. env scrub: MO_CANARY does not leak into the child suite ──────────────
def test_child_env_scrubs_mo_canary(tmp_path):
    repo = _make_repo(tmp_path, {
        "mod.py": "def add(a, b):\n    return a + b\n",
        "test_canary.py": (
            "import os\n\n\n"
            "def test_canary():\n"
            "    assert \"MO_CANARY\" not in os.environ\n"
        ),
    })

    rc, out = _run_verifier(
        repo, tmp_path, run_id="env-scrub",
        extra_env={"MO_CANARY": "1"},
    )

    assert rc == 0, out
    assert out["pass"] is True, out


# ── 6. source-level pins: scrub seam + red-base knobs are wired ─────────────
def test_codefix_verifier_source_wires_scrub_and_red_base():
    src = VERIFIER.read_text(encoding="utf-8")
    assert "from mini_ork.verify.test_env import scrubbed_test_env" in src
    assert "def scrubbed_test_env(environ=None):" in src  # local fallback
    assert "def _child_env" in src
    assert "env=_child_env()" in src  # post run + baseline run call sites
    assert 'k.startswith("MO_")' in src  # MO_* dropped on top of the scrub
    assert "red_base_unverified" in src
    assert "MO_TEST_CERTIFY_ON_IMPROVEMENT" in src
    assert "MO_TEST_LEGACY_RED_BASE" in src
