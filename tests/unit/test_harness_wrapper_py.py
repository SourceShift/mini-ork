"""Parity gate: mini_ork.ported.harness_wrapper vs lib/harness_wrapper.sh.

Each fixture runs the LIVE bash function (via `bash -c 'source ... && mo_harness_wrap ...'`)
and the Python port against the same workspace + harness + kickoff + env, then
asserts byte-identical verdict JSON + matching rc.  Tests cover the validation
gate (unknown-harness / empty harness / missing kickoff), dry-run path,
cli_absent path (one per supported harness), and git-init idempotence — every
status reachable WITHOUT invoking a real CLI (so no tokens burned).

A single workspace per fixture is reused across both runs (matching the
production shape: one run dir per run, multiple harness invocations) so the
diff_path field is identical.  Bash runs first; the Python run starts with
the git state bash left behind, exercising the "existing repo" branch of
git_init_if_needed — which is the realistic production code path (an operator
re-invokes a harness after the first run is done).

The kickoff contract requires `>=6 parity cases, all green`; this file ships
8 fixtures for margin against future status-enum expansion.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import harness_wrapper as hw  # noqa: E402

HARNESS_WRAPPER_SH = REPO / "lib" / "harness_wrapper.sh"

# Restricted PATH: /usr/bin:/bin on macOS includes /usr/bin/python3 (3.9) so
# bash's `python3 - <<PY` heredoc still resolves; the harness CLIs (claude,
# codex, gemini) live under /opt/homebrew/bin or /Users/admin/.local/bin and
# are therefore absent — this is what triggers the `cli_absent` status.
RESTRICTED_PATH = "/usr/bin:/bin"


def _run_bash(
    work_dir: Path, harness: str, kickoff: Path, env_overrides: dict | None,
) -> tuple[int, dict | None, bytes | None]:
    """Invoke the LIVE bash function via subprocess (mirrors production
    runtime: source the file, call the function).  Returns (rc, verdict_dict,
    raw_verdict_bytes)."""
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(work_dir)}
    if env_overrides:
        env.update(env_overrides)
    r = subprocess.run(
        [
            "bash", "-c",
            f'. "{HARNESS_WRAPPER_SH}" && mo_harness_wrap "$1" "$2"',
            "_", harness, str(kickoff),
        ],
        env=env, capture_output=True, text=True, check=False,
    )
    verdict_path = work_dir / "harness-verdict.json"
    raw = verdict_path.read_bytes() if verdict_path.is_file() else None
    verdict = json.loads(raw.decode("utf-8")) if raw else None
    return r.returncode, verdict, raw


def _run_py(
    work_dir: Path, harness: str, kickoff: Path, monkeypatch,
    env_overrides: dict | None,
) -> tuple[int, dict | None, bytes | None]:
    """Invoke the Python port with the same env as _run_bash."""
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(work_dir))
    if env_overrides:
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)
    rc = hw.mo_harness_wrap(harness, str(kickoff))
    verdict_path = work_dir / "harness-verdict.json"
    raw = verdict_path.read_bytes() if verdict_path.is_file() else None
    verdict = json.loads(raw.decode("utf-8")) if raw else None
    return rc, verdict, raw


def _assert_parity(
    bash_rc: int, bash_verdict: dict | None,
    py_rc: int, py_verdict: dict | None,
    fixture: str,
) -> None:
    """Field-by-field parity check; emits precise diff on mismatch."""
    assert bash_rc == py_rc, (
        f"[{fixture}] rc mismatch: bash={bash_rc} py={py_rc}"
    )
    if bash_verdict is None and py_verdict is None:
        return
    assert bash_verdict is not None and py_verdict is not None, (
        f"[{fixture}] verdict-presence mismatch: bash={bash_verdict!r} py={py_verdict!r}"
    )
    bk, pk = sorted(bash_verdict.keys()), sorted(py_verdict.keys())
    assert bk == pk, f"[{fixture}] key mismatch: bash={bk} py={pk}"
    for k in bash_verdict:
        b, p = bash_verdict[k], py_verdict[k]
        if isinstance(b, float) or isinstance(p, float):
            assert abs(float(b) - float(p)) < 1e-6, (
                f"[{fixture}] field {k!r}: bash={b!r} py={p!r}"
            )
        else:
            assert b == p, f"[{fixture}] field {k!r}: bash={b!r} py={p!r}"


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


def _run_pair(
    work: Path, harness: str, monkeypatch, env_overrides: dict | None,
) -> tuple[int, dict | None, bytes | None, int, dict | None, bytes | None]:
    """Bash first, then Python on the same workspace.  Returns
    (brc, bverdict, bbytes, prc, pverdict, pbytes)."""
    kickoff = work / "kickoff.md"
    if not kickoff.exists():
        kickoff.write_text("stub\n", encoding="utf-8")
    brc, bverdict, bbytes = _run_bash(work, harness, kickoff, env_overrides)
    prc, pverdict, pbytes = _run_py(work, harness, kickoff, monkeypatch, env_overrides)
    return brc, bverdict, bbytes, prc, pverdict, pbytes


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def test_cli_absent_claude_code(tmp_path, monkeypatch):
    """claude-code + PATH=/usr/bin:/bin → cli_absent, rc=0, exit_code=127."""
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": RESTRICTED_PATH}
    brc, bverdict, bbytes, prc, pverdict, pbytes = _run_pair(
        work, "claude-code", monkeypatch, env,
    )
    _assert_parity(brc, bverdict, prc, pverdict, "cli_absent_claude_code")
    assert bverdict is not None
    assert bverdict["harness"] == "claude-code"
    assert bverdict["status"] == "cli_absent"
    assert bverdict["exit_code"] == 127
    assert brc == 0
    assert bbytes == pbytes, (
        f"[cli_absent_claude_code] bytes differ:\n--- bash ---\n{bbytes!r}\n"
        f"--- py ---\n{pbytes!r}"
    )


def test_cli_absent_codex_cli(tmp_path, monkeypatch):
    """codex-cli + PATH=/usr/bin:/bin → cli_absent, harness=codex-cli."""
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": RESTRICTED_PATH}
    brc, bverdict, bbytes, prc, pverdict, pbytes = _run_pair(
        work, "codex-cli", monkeypatch, env,
    )
    _assert_parity(brc, bverdict, prc, pverdict, "cli_absent_codex_cli")
    assert bverdict is not None
    assert bverdict["harness"] == "codex-cli"
    assert bverdict["notes"] == "no codex on PATH"
    assert bbytes == pbytes


def test_cli_absent_gemini_cli(tmp_path, monkeypatch):
    """gemini-cli + PATH=/usr/bin:/bin → cli_absent, harness=gemini-cli."""
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": RESTRICTED_PATH}
    brc, bverdict, bbytes, prc, pverdict, pbytes = _run_pair(
        work, "gemini-cli", monkeypatch, env,
    )
    _assert_parity(brc, bverdict, prc, pverdict, "cli_absent_gemini_cli")
    assert bverdict is not None
    assert bverdict["harness"] == "gemini-cli"
    assert bverdict["notes"] == "no gemini on PATH"
    assert bbytes == pbytes


def test_unknown_harness(tmp_path, monkeypatch):
    """unknown-harness → rc=2, no verdict file written (validation gate runs
    BEFORE any side effect, so no .git/ is left behind)."""
    work = tmp_path / "work"
    work.mkdir()
    brc, bverdict, _b, prc, pverdict, _p = _run_pair(
        work, "unknown-harness", monkeypatch, None,
    )
    del _b, _p  # unused in this fixture
    _assert_parity(brc, bverdict, prc, pverdict, "unknown_harness")
    assert brc == 2
    assert bverdict is None and pverdict is None
    # Repo must NOT be initialized — the validation gate runs before git init.
    assert subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode != 0


def test_dry_run_claude_code(tmp_path, monkeypatch):
    """MO_HARNESS_DRY_RUN=1 + claude-code → status=dry_run, rc=0, lines=0."""
    work = tmp_path / "work"
    work.mkdir()
    env = {"MO_HARNESS_DRY_RUN": "1"}
    brc, bverdict, bbytes, prc, pverdict, pbytes = _run_pair(
        work, "claude-code", monkeypatch, env,
    )
    _assert_parity(brc, bverdict, prc, pverdict, "dry_run_claude_code")
    assert bverdict is not None
    assert bverdict["status"] == "dry_run"
    assert bverdict["exit_code"] == 0
    assert bverdict["diff_lines"] == 0
    assert bverdict["notes"] == "dry-run mode"
    assert bbytes == pbytes, (
        f"[dry_run_claude_code] bytes differ:\n--- bash ---\n{bbytes!r}\n"
        f"--- py ---\n{pbytes!r}"
    )


def test_git_init_idempotence(tmp_path, monkeypatch):
    """Re-run on a workspace that is ALREADY a git repo with dirty state
    must not error and must emit the same dry-run verdict as a fresh repo."""
    work = tmp_path / "work"
    work.mkdir()
    _bootstrap_existing_repo(work)
    env = {"MO_HARNESS_DRY_RUN": "1"}
    brc, bverdict, bbytes, prc, pverdict, pbytes = _run_pair(
        work, "claude-code", monkeypatch, env,
    )
    _assert_parity(brc, bverdict, prc, pverdict, "git_init_idempotence")
    # Repo is still initialized after both runs (idempotence).
    assert subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    assert bverdict is not None
    assert bverdict["diff_lines"] == 0
    assert bbytes == pbytes


def test_empty_harness(tmp_path, monkeypatch):
    """Empty harness string → rc=2, no verdict file written."""
    work = tmp_path / "work"
    work.mkdir()
    brc, bverdict, _b, prc, pverdict, _p = _run_pair(
        work, "", monkeypatch, None,
    )
    del _b, _p  # unused in this fixture
    _assert_parity(brc, bverdict, prc, pverdict, "empty_harness")
    assert brc == 2
    assert bverdict is None and pverdict is None


def test_missing_kickoff(tmp_path, monkeypatch):
    """Missing kickoff file → rc=2, no verdict file written."""
    work = tmp_path / "work"
    work.mkdir()
    # Note: do NOT create kickoff.md — _run_pair would create it; bypass:
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(work))
    missing = work / "does_not_exist.md"
    brc, bverdict, _b = _run_bash(work, "claude-code", missing, None)
    prc, pverdict, _p = _run_py(work, "claude-code", missing, monkeypatch, None)
    del _b, _p  # unused in this fixture
    _assert_parity(brc, bverdict, prc, pverdict, "missing_kickoff")
    assert brc == 2
    assert bverdict is None and pverdict is None


# ----------------------------------------------------------------------
# Strangler-fig contract: bash source must remain byte-identical.
# ----------------------------------------------------------------------


def test_bash_source_byte_identical():
    """lib/harness_wrapper.sh must be untouched after the port (strangler-fig)."""
    r = subprocess.run(
        ["git", "diff", "--exit-code", "lib/harness_wrapper.sh"],
        cwd=str(REPO), capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, (
        f"lib/harness_wrapper.sh drifted from the port contract:\n{r.stdout}"
    )
