"""Tests for mini_ork.optimize.gepa — the R4a reflective prompt optimizer.

Trivial scoring task: a candidate scores 1.0 per example iff its
``instruction`` field contains ``TARGET_TOKEN``, else 0.0. Tests monkeypatch
``mini_ork.optimize.gepa.dispatch_model`` so no real model call happens.
"""

from __future__ import annotations

import json

import pytest

from mini_ork.dispatch import DispatchResult
from mini_ork.optimize import optimize


TARGET_TOKEN = "target_token"
SEED = {"instruction": "You are a helpful assistant."}
N_EXAMPLES = 10
MINIBATCH = 4


class StubAdapter:
    """Scoring: 1.0 per example iff ``TARGET_TOKEN`` appears in
    ``candidate['instruction']``. Records every ``evaluate`` call so tests
    can assert on rollout counts.
    """

    def __init__(self, full_batch=None):
        self.full_eval_count = 0
        self.iteration_count = 0
        self.full_batch = (
            list(full_batch) if full_batch is not None else list(range(N_EXAMPLES))
        )
        self.evaluate_log: list[tuple[int, bool]] = []

    def evaluate(self, batch, candidate):
        has = TARGET_TOKEN in candidate.get("instruction", "")
        self.evaluate_log.append((len(batch), has))
        scores = [1.0 if has else 0.0 for _ in batch]
        traces = [{"i": i} for i in batch]
        return scores, traces

    def make_reflective_dataset(self, candidate, eval_batch):
        return [
            {"example": ex, "score": s, "trace": t}
            for ex, s, t in eval_batch
        ]


def _improve_reflection(candidate, _dataset, _key):
    """Propose an edit that ADDS ``TARGET_TOKEN`` (improving)."""
    return {"instruction": f"{candidate.get('instruction', '')} {TARGET_TOKEN}"}


def _noop_reflection(candidate, _dataset, _key):
    """Propose a no-op edit — mutated equals parent."""
    return dict(candidate)


@pytest.fixture
def patch_dispatch(monkeypatch):
    """Patch ``mini_ork.optimize.gepa.dispatch_model`` to call a Python
    callable instead of a real model. The reflection callable receives
    ``(candidate, feedback_list, component_key)`` and must return the new
    candidate dict (the test response is wrapped in a JSON code fence so it
    exercises the parser).
    """

    def _patch(reflect_fn):
        def stub_dispatch(request):
            try:
                blob = json.loads(request.prompt)
                candidate = blob.get("candidate", {})
                component_key = blob.get("component_to_rewrite", "instruction")
                new_candidate = reflect_fn(
                    candidate, blob.get("feedback", []), component_key
                )
                response_text = "```json\n" + json.dumps(new_candidate) + "\n```"
            except Exception:
                response_text = "```json\n{}\n```"
            return DispatchResult(ok=True, rc=0, text=response_text, model="stub")

        monkeypatch.setattr(
            "mini_ork.optimize.gepa.dispatch_model", stub_dispatch
        )

    return _patch


def test_optimize_improves_seed(patch_dispatch):
    """Reflective loop must return a candidate strictly better than seed."""
    patch_dispatch(_improve_reflection)
    adapter = StubAdapter()
    best, accepted = optimize(SEED, adapter, minibatch=MINIBATCH, budget=4)
    # The improving reflection was accepted at least once.
    assert len(accepted) >= 1, f"expected ≥1 accept, got {accepted}"
    # The best candidate now contains TARGET_TOKEN; seed did not.
    assert TARGET_TOKEN in best["instruction"]
    assert TARGET_TOKEN not in SEED["instruction"]
    # At least one full eval was triggered by the accept.
    assert adapter.full_eval_count >= 1


def test_gate_rejects_non_improving_mutation(patch_dispatch):
    """Minibatch acceptance gate REJECTS no-op edits; seed is preserved."""
    patch_dispatch(_noop_reflection)
    adapter = StubAdapter()
    best, accepted = optimize(SEED, adapter, minibatch=MINIBATCH, budget=4)
    # No acceptance — every mutation equalled the parent on the minibatch,
    # so the strict `sum(new) > sum(parent)` gate failed.
    assert accepted == [], f"expected no accepts, got {accepted}"
    assert adapter.full_eval_count == 0
    # Seed was kept (Pareto front never moved off it).
    assert best == SEED
    assert TARGET_TOKEN not in best["instruction"]


def test_loop_halts_at_budget(patch_dispatch):
    """Loop runs exactly ``budget`` iterations regardless of accept/reject."""
    patch_dispatch(_improve_reflection)
    adapter = StubAdapter()
    budget = 3
    optimize(SEED, adapter, minibatch=MINIBATCH, budget=budget)
    assert adapter.iteration_count == budget


def test_rollout_economy_bounded(patch_dispatch):
    """Full-eval count stays bounded by budget even when many mutations
    are attempted. The minibatch gate skips full eval on rejected mutations
    — that's the rollout-economy claim.
    """
    patch_dispatch(_improve_reflection)
    adapter = StubAdapter()
    budget = 8
    optimize(SEED, adapter, minibatch=MINIBATCH, budget=budget)
    # All 8 iterations ran.
    assert adapter.iteration_count == budget
    # But full evals are bounded by budget (actually ≤ number of accepts).
    assert adapter.full_eval_count <= budget
    # And critically: rejects happened — otherwise rollout economy is unproven.
    # After the first accept, the mutation has TARGET_TOKEN, so subsequent
    # "improving" reflections produce candidates with the same substring →
    # equal minibatch scores → strict `>` gate rejects them.
    assert adapter.full_eval_count < adapter.iteration_count, (
        f"expected rejections to bound rollout; "
        f"got full_eval_count={adapter.full_eval_count}, "
        f"iteration_count={adapter.iteration_count}"
    )