"""Unit tests for the per-run orchestrator's pure decision core.

Covers the 12 acceptance criteria in
``kickoffs/auto/run-orchestrator.md`` §Done When. The core is pure — no DB,
lane, or model — so every case runs against plain values. The impure adapter
(``on_node_boundary``) is exercised only for its default-off contract (never
called into here), which is the load-bearing invariant.

The test file deliberately clears ``MO_RUN_ORCHESTRATOR`` explicitly rather than
assuming a clean env: the suite-wide conftest snapshots/restores ``os.environ``
but does not scrub ``MO_*`` vars.
"""
from __future__ import annotations

from mini_ork.orchestration import runner_orchestrator as ro


# ── flag ─────────────────────────────────────────────────────────────────────
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("MO_RUN_ORCHESTRATOR", raising=False)
    assert ro.enabled() is False


def test_flag_on_truthy_variants(monkeypatch):
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("MO_RUN_ORCHESTRATOR", value)
        assert ro.enabled() is True, value
    for value in ("0", "off", ""):
        monkeypatch.setenv("MO_RUN_ORCHESTRATOR", value)
        assert ro.enabled() is False, value


# ── decisions ────────────────────────────────────────────────────────────────
def test_advance_on_success():
    action = ro.decide(node_id="impl", node_type="implementer", rc=0)
    assert action.kind == ro.ADVANCE
    assert action.reason


def test_help_on_stall():
    action = ro.decide(node_id="impl", node_type="implementer", rc=0, stall=True)
    assert action.kind == ro.HELP
    assert action.requires_llm is True


def test_repair_on_failed_with_retries_edge():
    # A retries edge is a repair channel for the node itself. A provider-limit
    # (recovery-locus) failure is *not* a harness-surface fault, so the edge is
    # promoted to the node locus and the node is repaired in place (rule 3).
    action = ro.decide(
        node_id="impl", node_type="implementer", rc=1,
        finish_reason="rate limit exceeded", attempts=1, has_retries_edge=True)
    assert action.kind == ro.REPAIR
    assert action.payload.get("check") == "retries_edge"


def test_mutate_on_harness_locus():
    # A harness-surface locus (here: a prompt fault from an output_invalid stop)
    # cannot be fixed by a node re-run, so the core proposes a harness mutation
    # (rule 4) instead of a repair.
    action = ro.decide(
        node_id="impl", node_type="implementer", rc=1,
        finish_reason="invalid json", has_retries_edge=False)
    assert action.kind == ro.MUTATE
    assert action.payload.get("locus") == "prompt"


# ── classification ───────────────────────────────────────────────────────────
def test_classify_infra_is_env_locus():
    failure_class, locus, scope = ro.classify_outcome("implementer", 1, "oom")
    assert failure_class == "infra_interrupt"
    assert locus == "env"
    assert scope != "none"


def test_classify_output_invalid_is_prompt():
    failure_class, locus, _scope = ro.classify_outcome("implementer", 1, "invalid json")
    assert failure_class == "output_invalid"
    assert locus == "prompt"


def test_classify_terminal_is_verifier():
    failure_class, locus, _scope = ro.classify_outcome("implementer", 1, "terminal")
    assert failure_class == "terminal"
    assert locus == "verifier"


# ── guard ────────────────────────────────────────────────────────────────────
def test_guard_rejects_contract_mutation():
    assert ro.guard_steer("change finish_reason") is False


def test_guard_allows_content_nudge():
    assert ro.guard_steer("consider narrowing the edit to the failing assertion") is True


# ── budget ───────────────────────────────────────────────────────────────────
def test_budget_gate_blocks_over_cap(monkeypatch):
    monkeypatch.setenv("MO_ORCH_BUDGET_USD", "1.0")
    assert ro.budget_ok(5.0) is False
    assert ro.budget_ok(0.5) is True
