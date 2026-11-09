"""Frozen-kernel boundary: the self-driving policy's INPUTS are frozen too.

``mini_ork/recovery/escalation_policy.py`` (frozen) decides whether the
self-driving loop may open a framework-edit fix epic, but it decides only from
the ``EscalationInputs`` other modules hand it — and those modules were not
frozen. An auto-opened fix epic could edit any of them and change what the
policy decides without touching the policy. This pins the eight newly frozen
inputs in BOTH the built-in floor and ``config/kernel-frozen.yaml``, keeps the
pre-existing entries, and proves the publish-time refusal now covers them.

Hermetic: no network; the publish-refusal case drives the real ``land_patch``
against a throwaway git repo with no ``config/kernel-frozen.yaml``.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from mini_ork.cli import publisher_authored_patch
from mini_ork.vcs import repo_integrity_guard

REPO = Path(__file__).resolve().parents[2]

# Kickoff: 'The eight new entries, in this order:'
NEW = [
    "mini_ork/recovery/escalation.py",
    "mini_ork/recovery/diagnose.py",
    "mini_ork/recovery/diagnoser_llm.py",
    "mini_ork/recovery/supervisor.py",
    "mini_ork/recovery/graph_patch.py",
    "mini_ork/stores/diagnosis_ledger.py",
    "mini_ork/cli/publisher_authored_patch.py",
    "mini_ork/gates/merge_risk.py",
]

# The 14 entries the floor held before this change (the ``FULL_LIST_YAML`` paths
# in tests/unit/test_frozen_kernel.py).
FLOOR_BEFORE = [
    "config/kernel-frozen.yaml",
    "recipes/framework-edit/verifiers/static-check.py",
    "mini_ork/vcs/repo_integrity_guard.py",
    "mini_ork/runtime/sandbox.py",
    "mini_ork/gates/gate_bootstrap.py",
    "mini_ork/gates/abstain_gate.py",
    "mini_ork/recovery/circuit_breaker.py",
    "mini_ork/recovery/escalation_policy.py",
    "mini_ork/dispatch/calibration.py",
    "mini_ork/recovery/auto_repair.py",
    "mini_ork/policies/engine.py",
    "mini_ork/dispatch/cost_pause.py",
    "mini_ork/stores/safety_events.py",
    "mini_ork/observability/bug_report.py",
]

GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")


# ── the floor and the file both carry the eight inputs ───────────────────────


@pytest.mark.parametrize("p", NEW)
def test_both_lists_carry_every_new_entry(p):
    assert p in repo_integrity_guard.FROZEN_PATHS_DEFAULT, (
        f"{p} missing from FROZEN_PATHS_DEFAULT")
    assert p in repo_integrity_guard.load_frozen_paths(str(REPO)), (
        f"{p} missing from config/kernel-frozen.yaml's paths")
    assert repo_integrity_guard.frozen_hits(
        [p], repo_integrity_guard.enforced_frozen_paths(str(REPO))) == [p], (
        f"{p} is not enforced for the target repo")


def test_floor_last_eight_in_kickoff_order():
    """The eight inputs stay one contiguous block in the kickoff's order.

    Pinned positionally as "the last eight" until triage/failures.py was
    appended after them (the 2026-10-29 review's freeze of escalation.py's
    import). Contiguity + order is the property the pin was for; the tail
    position was an accident of it."""
    floor = repo_integrity_guard.FROZEN_PATHS_DEFAULT
    idx = [floor.index(p) for p in NEW]
    assert idx == list(range(idx[0], idx[0] + len(NEW)))


def test_nothing_dropped_from_floor_or_file():
    floor = repo_integrity_guard.FROZEN_PATHS_DEFAULT
    file_list = repo_integrity_guard.load_frozen_paths(str(REPO))
    for p in FLOOR_BEFORE:
        assert p in floor, f"dropped from the floor: {p}"
        assert p in file_list, f"dropped from config/kernel-frozen.yaml: {p}"
    assert "recipes/code-fix/probes/fixtures/**" in file_list


def test_the_eight_are_real_files():
    for p in NEW:
        assert (REPO / p).is_file(), f"not a file in the target repo: {p}"


def test_negative_control_unfrozen_paths_pass_clean():
    assert repo_integrity_guard.frozen_hits(
        ["mini_ork/recovery/retry_hint.py", "mini_ork/recovery/planner.py"],
        repo_integrity_guard.enforced_frozen_paths(str(REPO))) == []


# ── the publish refusal now covers the policy's inputs ──────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in the throwaway repo with the index/dir env sanitized, so an
    ambient lane value can never make an assertion read a different tree than
    ``land_patch`` did."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")}
    proc = subprocess.run(["git", *GIT_IDENTITY, *args], cwd=repo, env=env,
                          capture_output=True, text=True, check=check, timeout=60)
    return proc.stdout


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_publish_refusal_covers_the_new_frozen_inputs(tmp_path, monkeypatch):
    """With NO ``config/kernel-frozen.yaml``, the built-in floor alone must refuse
    a patch to ``mini_ork/recovery/supervisor.py``; the operator token lands it."""
    # Keep safety-event writes off the operator's real DB.
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))

    repo = tmp_path / "throwaway"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "mini_ork/recovery/supervisor.py", "# base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    head0 = _git(repo, "rev-parse", "HEAD").strip()
    assert not (repo / "config" / "kernel-frozen.yaml").exists()

    _write(repo, "mini_ork/recovery/supervisor.py", "# base\nedited = True\n")
    patch = _git(repo, "diff")
    _write(repo, "mini_ork/recovery/supervisor.py", "# base\n")  # restore
    assert "edited = True" in patch

    # refusal: no override token
    monkeypatch.delenv("MO_ALLOW_FROZEN_EDIT", raising=False)
    refused = publisher_authored_patch.land_patch(str(repo), patch, "")
    assert isinstance(refused, publisher_authored_patch.Abstain), (
        f"expected an Abstain, got {refused!r}")
    assert refused.reason == "publish-frozen-path", refused.reason
    assert _git(repo, "rev-parse", "HEAD").strip() == head0, "HEAD moved on refusal"
    assert (repo / "mini_ork" / "recovery" / "supervisor.py").read_text() == "# base\n"

    # the operator's single override token lands it
    monkeypatch.setenv("MO_ALLOW_FROZEN_EDIT", "1")
    landed = publisher_authored_patch.land_patch(str(repo), patch, "")
    assert isinstance(landed, str), f"expected a sha, got {landed!r}"
    assert re.fullmatch(r"[0-9a-f]{40}", landed), landed
    assert _git(repo, "rev-parse", "HEAD").strip() == landed
    assert "edited = True" in _git(
        repo, "show", f"{landed}:mini_ork/recovery/supervisor.py")
