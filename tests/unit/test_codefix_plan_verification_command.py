"""The test gate must run the command the run was dispatched with.

``run_profile.json``'s ``verification_command`` is written by the profiler and is
frequently empty: the profiler *defers* whenever the kickoff declares its own
proof of success (``gates/profile_gate.declares_verification_command``). The
command the run is actually dispatched with then survives only in the plan's
``verifier_contract.checks``. A revive that cannot see it falls through to
auto-detect and substitutes the repo's default ``test`` script — measured on the
researcher home, a scoped 653/653 compose suite became the whole repo's
``pnpm run test`` at 762 failing, and because the run's own script also exported
``JEST_GUARD_SOFT=1`` the delta base then refused under load (rc=77).

These tests pin the plan as a source ranked below the profile and above
auto-detect, and pin the rule that tells a test check from the typecheck and grep
checks that share ``verifier_contract.checks``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

TEST_CHECK = "bash /home/kickoffs/cjd-cc-sa1-test.sh"
TYPECHECK_CHECK = "bash .husky/_typecheck-touched.sh"
GREP_CHECK = 'grep -rln "Referral Engine" src/pages/compose/companion/'


@pytest.fixture(scope="module")
def verifier():
    """Load the verifier as a module (it is a script, guarded by __main__)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("mo_codefix_verifier_plan_cmd", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_plan(run_dir: Path, checks: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"verifier_contract": {"checks": checks}}), encoding="utf-8"
    )


def _write_profile(run_dir: Path, command) -> None:
    (run_dir / "run_profile.json").write_text(
        json.dumps({"verification_command": command}), encoding="utf-8"
    )


# ── the selection rule ─────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    TEST_CHECK,                                   # the real researcher case
    "pytest -q tests/unit/test_widget.py",
    "npm run test",
    "pnpm exec jest --config tests/jest.config.js src/pages/compose",
    "cargo test --lib",
    "go test ./...",
])
def test_a_test_invocation_is_recognised(verifier, cmd: str) -> None:
    assert verifier._looks_like_test_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    TYPECHECK_CHECK,
    "npx tsc --noEmit -p tsconfig.json",
    "eslint src/",
    GREP_CHECK,
    'grep -rn "sampleStage.test.tsx" src/',       # a grep that names a test file
    '[ "$(grep -rn "<StyleGuideEditor" src/ | wc -l)" = "1" ]',
    '! grep -rn "lw-pm-adv-style-guide" src/',
    "echo done",
    "",
])
def test_a_non_test_invocation_is_rejected(verifier, cmd: str) -> None:
    assert verifier._looks_like_test_command(cmd) is False


# ── the plan as a source ───────────────────────────────────────────────────


def test_the_test_check_is_picked_from_a_mixed_check_list(verifier, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _write_plan(run_dir, [
        {"id": "ac1-jest", "command": TEST_CHECK},
        {"id": "ac2-typecheck", "command": TYPECHECK_CHECK},
        {"id": "ac3-no-fixtures", "command": GREP_CHECK},
    ])
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    assert verifier._plan_verification_command() == TEST_CHECK


def test_a_checks_list_of_only_inspections_yields_nothing(verifier, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _write_plan(run_dir, [
        {"id": "ac1-typecheck", "command": TYPECHECK_CHECK},
        {"id": "ac2-no-fixtures", "command": GREP_CHECK},
    ])
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    assert verifier._plan_verification_command() == ""


@pytest.mark.parametrize("payload", [
    "{not json",                                      # malformed
    '{"verifier_contract": null}',                    # no contract
    '{"verifier_contract": {"checks": "nope"}}',       # checks not a list
    '{"verifier_contract": {"checks": [null, 7]}}',    # entries not dicts
    '{"verifier_contract": {"checks": [{"command": 3}]}}',
    "[]",                                             # not an object
])
def test_a_malformed_plan_never_raises(verifier, tmp_path, monkeypatch, payload: str):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(payload, encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    assert verifier._plan_verification_command() == ""


def test_a_missing_plan_never_raises(verifier, tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "nowhere"))
    assert verifier._plan_verification_command() == ""


def test_the_home_runs_dir_is_the_fallback_when_the_env_is_unset(
    verifier, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    _write_plan(home / "runs" / "run-1", [{"command": TEST_CHECK}])
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setattr(verifier, "MINI_ORK_HOME", str(home))
    monkeypatch.setattr(verifier, "MINI_ORK_RUN_ID", "run-1")

    assert verifier._plan_verification_command() == TEST_CHECK


# ── the ordering in detect_test_cmd ────────────────────────────────────────


def test_the_profile_outranks_the_plan(verifier, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _write_plan(run_dir, [{"command": TEST_CHECK}])
    _write_profile(run_dir, "pytest -q declared-by-profile")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setattr(verifier, "MINI_ORK_HOME", str(tmp_path / "none"))
    monkeypatch.delenv("MINI_ORK_TEST_CMD", raising=False)

    assert verifier.detect_test_cmd() == "pytest -q declared-by-profile"


def test_the_plan_outranks_auto_detect(verifier, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _write_plan(run_dir, [{"command": TEST_CHECK}])
    _write_profile(run_dir, [])
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run")
    monkeypatch.setattr(verifier, "MINI_ORK_HOME", str(tmp_path / "none"))
    monkeypatch.delenv("MINI_ORK_TEST_CMD", raising=False)

    assert verifier.detect_test_cmd() == TEST_CHECK


def test_the_explicit_env_override_still_wins(verifier, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _write_plan(run_dir, [{"command": TEST_CHECK}])
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_TEST_CMD", "echo explicit")

    assert verifier.detect_test_cmd() == "echo explicit"


def test_the_plan_read_is_wired_between_the_profile_and_auto_detect():
    """Source-shape guard: the call must sit where the ordering says it does.

    ``detect_test_cmd`` runs at import, so a misplaced call is only visible in a
    revive — the exact situation that produced the bug.
    """
    src = VERIFIER.read_text(encoding="utf-8")
    profile_at = src.index("profile_cmd = _profile_verification_command()")
    plan_at = src.index("plan_cmd = _plan_verification_command()")
    auto_at = src.index("npm / pnpm / yarn / bun")
    assert profile_at < plan_at < auto_at
