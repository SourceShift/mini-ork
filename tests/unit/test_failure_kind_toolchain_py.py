"""Contract guard for the ``failure_kind="toolchain"`` sweep.

``mini_ork/verify/levels.py`` turns a red verdict carrying
``failure_kind="toolchain"`` into an ABSTENTION (UNVERIFIED) instead of a
refutation, because a tool that never launched has not judged the code. That
makes the field dangerous in both directions:

* **Omitted** when the tool genuinely could not start — a missing ``pnpm`` /
  ``psql`` / ``stryker`` / failing ``git`` is charged to the implementation and
  burns an implementer round on a defect that does not exist.
* **Emitted** when a real failure exists — a broken toolchain silently excuses a
  regression, which is the failure mode the whole contract exists to prevent.

This module pins both halves for the recipe verifiers that spawn an external
toolchain. The sweep is deliberately explicit: a new verifier must be added
here, and the behavioural controls below prove the abstinence is conditional
rather than unconditional.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]

# Verifiers that spawn a tool the host may simply not have installed. Broad
# surface on purpose — a repo-wide "every verifier" test would force an
# abstention branch onto the ~30 that shell out to a local harness script,
# where "command not found" is not a realistic state.
SWEPT_VERIFIERS = [
    "recipes/bdd-first-delivery/verifiers/playwright_runner.py",
    "recipes/epic-runner/verifiers/researcher-verifier.py",
    "recipes/framework-edit/verifiers/static-check.py",
    "recipes/framework-edit/verifiers/spec-tests.py",
    "recipes/recursive-self-improve/verifiers/self-tests-pass.py",
    "recipes/recursive-validate-impl/verifiers/tier2-scoped-unit.py",
    "recipes/recursive-validate-impl/verifiers/tier3-property-mutation.py",
    "recipes/self-migrate/verifiers/feature-acceptance.py",
    "recipes/self-migrate/verifiers/pre-retirement-parity.py",
]

PLAYWRIGHT = REPO_ROOT / "recipes" / "bdd-first-delivery" / "verifiers" / "playwright_runner.py"

# A binary that cannot exist: the spawn raises FileNotFoundError, which the
# runner records as rc 127 — the canonical "the tool never ran" state.
MISSING_TOOL = "mini-ork-no-such-binary-xyz"


def _last_json(text: str) -> dict:
    """The last non-empty stdout line as JSON — the verifier's envelope."""
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "verifier printed no payload"
    return json.loads(lines[-1])


@pytest.mark.parametrize("rel", SWEPT_VERIFIERS)
def test_verifier_can_declare_a_toolchain_abstention(rel: str) -> None:
    """Every swept verifier can still write ``failure_kind="toolchain"``.

    A static check, not a re-implementation of the verifier: it catches the
    regression that matters (the branch deleted in a refactor) without pinning
    the mechanism. Most verifiers route through ``classify_spawn``; spec-tests
    knows the answer structurally (no rc and no timeout means the spawn never
    ran) and writes the literal, which is just as honest.
    """
    src = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert '"failure_kind"' in src or "failure_kind=" in src, f"{rel} never emits failure_kind"
    assert "classify_spawn" in src or '"toolchain"' in src, (
        f"{rel} emits failure_kind but can never attribute it to a toolchain"
    )


def _run_playwright(tmp_path: Path, command: str) -> dict:
    """Drive the BDD verifier against a throwaway worktree holding one spec."""
    workdir = tmp_path / "wt"
    (workdir / "e2e").mkdir(parents=True)
    (workdir / "e2e" / "EP-1_smoke.spec.ts").write_text("// spec\n", encoding="utf-8")
    env = {
        **os.environ,
        "SUB_EPIC_ID": "EP-1",
        "WORKDIR": str(workdir),
        "MINI_ORK_PLAYWRIGHT_CMD": command,
        "MINI_ORK_BDD_TIMEOUT_SEC": "60",
    }
    # A verdict dir would add --reporter=json and a log file; the abstention is
    # decided before either matters, and leaving it unset keeps the run hermetic.
    env.pop("MINI_ORK_VERDICT_DIR", None)
    proc = subprocess.run(
        [sys.executable, str(PLAYWRIGHT)],
        env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    return _last_json(proc.stdout)


def test_missing_playwright_binary_abstains(tmp_path: Path) -> None:
    """rc 127 = the runner never started: abstain, do not refute the patch."""
    verdict = _run_playwright(tmp_path, MISSING_TOOL)
    assert verdict["pass"] is False
    assert verdict["exit_code"] == 127
    assert verdict["failure_kind"] == "toolchain"


def test_real_playwright_failure_still_refutes(tmp_path: Path) -> None:
    """A tool that RAN and rejected the spec is a verdict, not an abstention.

    This is the half that keeps the contract honest: if the abstention fired
    whenever ``pass`` was false, every failing spec would be excused.
    """
    verdict = _run_playwright(tmp_path, "false")
    assert verdict["pass"] is False
    assert verdict["exit_code"] == 1
    assert "failure_kind" not in verdict


def test_epic_verifier_lib_accepts_an_opt_in_failure_kind() -> None:
    """``emit_verifier_json`` stays schema-compatible without the new argument."""
    lib_src = (REPO_ROOT / "recipes" / "epic-runner" / "verifiers" / "_lib.py").read_text(
        encoding="utf-8"
    )
    assert "def emit_verifier_json(log, name, artifact_ref, failure_kind=None)" in lib_src
    assert 'payload["failure_kind"] = failure_kind' in lib_src
