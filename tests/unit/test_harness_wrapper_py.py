"""Unit tests: mini_ork.orchestration.harness_wrapper (bash parity halves removed; formerly vs lib/harness_wrapper.sh).

Each fixture runs the Python port against a workspace + harness + kickoff +
env and asserts the verdict JSON + rc. Tests cover the validation gate
(unknown-harness / empty harness / missing kickoff), dry-run path,
cli_absent path (one per supported harness), and git-init idempotence —
every status reachable WITHOUT invoking a real CLI (so no tokens burned).

The git-init idempotence fixture runs the port twice on the same workspace:
the first run exercises the fresh-init branch, the second the "existing
repo" branch (the realistic production code path — an operator re-invokes a
harness after the first run is done).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.orchestration import harness_wrapper as hw

# Restricted PATH: /usr/bin:/bin — the harness CLIs (claude, codex, gemini)
# live under /opt/homebrew/bin or /Users/admin/.local/bin and are therefore
# absent — this is what triggers the `cli_absent` status.
RESTRICTED_PATH = "/usr/bin:/bin"


def _run_py(
    work_dir: Path, harness: str, kickoff: Path, monkeypatch,
    env_overrides: dict | None,
) -> tuple[int, dict | None]:
    """Invoke the Python port with a controlled env. Returns (rc, verdict)."""
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(work_dir))
    if env_overrides:
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)
    rc = hw.mo_harness_wrap(harness, str(kickoff))
    verdict_path = work_dir / "harness-verdict.json"
    verdict = json.loads(verdict_path.read_text(encoding="utf-8")) \
        if verdict_path.is_file() else None
    return rc, verdict


def _bootstrap_existing_repo(workspace: Path) -> None:
    """Pre-initialize a workspace as a git repo with a dirty file so the
    git-init idempotence fixture exercises the 'existing repo' branch
    (matches production shape: re-running a harness on a workspace that's
    already under git)."""
    subprocess.run(
        ["git", "-C", str(workspace), "init", "--quiet"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "git", "-C", str(workspace),
            "-c", "user.email=harness@mini-ork.local",
            "-c", "user.name=mini-ork harness",
            "commit", "--allow-empty", "-m", "init", "--quiet",
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    (workspace / "dirty.txt").write_text("pre-existing dirty state\n", encoding="utf-8")


def _kickoff(work: Path) -> Path:
    kickoff = work / "kickoff.md"
    if not kickoff.exists():
        kickoff.write_text("stub\n", encoding="utf-8")
    return kickoff


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def test_cli_absent_claude_code(tmp_path, monkeypatch):
    """claude-code + PATH=/usr/bin:/bin → cli_absent, rc=0, exit_code=127."""
    work = tmp_path / "work"
    work.mkdir()
    rc, verdict = _run_py(work, "claude-code", _kickoff(work), monkeypatch,
                          {"PATH": RESTRICTED_PATH})
    assert rc == 0
    assert verdict is not None
    assert verdict["harness"] == "claude-code"
    assert verdict["status"] == "cli_absent"
    assert verdict["exit_code"] == 127


def test_cli_absent_codex_cli(tmp_path, monkeypatch):
    """codex-cli + PATH=/usr/bin:/bin → cli_absent, harness=codex-cli."""
    work = tmp_path / "work"
    work.mkdir()
    rc, verdict = _run_py(work, "codex-cli", _kickoff(work), monkeypatch,
                          {"PATH": RESTRICTED_PATH})
    assert rc == 0
    assert verdict is not None
    assert verdict["harness"] == "codex-cli"
    assert verdict["status"] == "cli_absent"
    assert verdict["notes"] == "no codex on PATH"


def test_cli_absent_gemini_cli(tmp_path, monkeypatch):
    """gemini-cli + PATH=/usr/bin:/bin → cli_absent, harness=gemini-cli."""
    work = tmp_path / "work"
    work.mkdir()
    rc, verdict = _run_py(work, "gemini-cli", _kickoff(work), monkeypatch,
                          {"PATH": RESTRICTED_PATH})
    assert rc == 0
    assert verdict is not None
    assert verdict["harness"] == "gemini-cli"
    assert verdict["status"] == "cli_absent"
    assert verdict["notes"] == "no gemini on PATH"


def test_unknown_harness(tmp_path, monkeypatch):
    """unknown-harness → rc=2, no verdict file written (validation gate runs
    BEFORE any side effect, so no .git/ is left behind)."""
    work = tmp_path / "work"
    work.mkdir()
    rc, verdict = _run_py(work, "unknown-harness", _kickoff(work), monkeypatch, None)
    assert rc == 2
    assert verdict is None
    # Repo must NOT be initialized — the validation gate runs before git init.
    assert subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode != 0


def test_dry_run_claude_code(tmp_path, monkeypatch):
    """MO_HARNESS_DRY_RUN=1 + claude-code → status=dry_run, rc=0, lines=0."""
    work = tmp_path / "work"
    work.mkdir()
    rc, verdict = _run_py(work, "claude-code", _kickoff(work), monkeypatch,
                          {"MO_HARNESS_DRY_RUN": "1"})
    assert rc == 0
    assert verdict is not None
    assert verdict["status"] == "dry_run"
    assert verdict["exit_code"] == 0
    assert verdict["diff_lines"] == 0
    assert verdict["notes"] == "dry-run mode"


def test_git_init_idempotence(tmp_path, monkeypatch):
    """Re-run on a workspace that is ALREADY a git repo with dirty state
    must not error and must emit the same dry-run verdict as a fresh repo."""
    work = tmp_path / "work"
    work.mkdir()
    _bootstrap_existing_repo(work)
    kickoff = _kickoff(work)
    env = {"MO_HARNESS_DRY_RUN": "1"}
    rc1, verdict1 = _run_py(work, "claude-code", kickoff, monkeypatch, env)
    # Second run on the same workspace — "existing repo" branch.
    rc2, verdict2 = _run_py(work, "claude-code", kickoff, monkeypatch, env)
    assert rc1 == rc2 == 0
    assert verdict1 == verdict2
    # Repo is still initialized after both runs (idempotence).
    assert subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    assert verdict2 is not None
    assert verdict2["diff_lines"] == 0


def test_empty_harness(tmp_path, monkeypatch):
    """Empty harness string → rc=2, no verdict file written."""
    work = tmp_path / "work"
    work.mkdir()
    rc, verdict = _run_py(work, "", _kickoff(work), monkeypatch, None)
    assert rc == 2
    assert verdict is None


def test_missing_kickoff(tmp_path, monkeypatch):
    """Missing kickoff file → rc=2, no verdict file written."""
    work = tmp_path / "work"
    work.mkdir()
    missing = work / "does_not_exist.md"
    rc, verdict = _run_py(work, "claude-code", missing, monkeypatch, None)
    assert rc == 2
    assert verdict is None
