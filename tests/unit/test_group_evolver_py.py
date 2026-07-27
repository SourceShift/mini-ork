"""Unit tests for mini_ork.learning.group_evolver.

Covers the proposal envelope (candidate fields, count via env/arg), input
validation, seeded determinism, the mutation-type coverage, and the
novelty_score 3-dim formula (node Jaccard / tool-sequence / failure-mode
distance, weighted 0.5/0.3/0.2, clamped to [0, 1]).

7 cases:
  (a) empty_history_returns_empty_candidates
  (b) invalid_json_raises_runtime_error
  (c) n_candidates_respected_via_env_var_default_5
  (d) all_eight_mutation_types_present_after_500_samples
  (e) determinism_under_seed_for_python_port
  (f) propose_returns_well_formed_candidate_envelopes
  (g) novelty_score_formula_boundaries_and_determinism
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.learning import group_evolver as ge  # noqa: E402

ND = {"candidate_id", "parent_id", "proposed_at"}

HISTORY = [
    {"workflow_id": "wf-alpha", "nodes": {"plan": {"tools": ["t1", "t2"]}, "exec": {"tools": ["t3"]}, "verify": {"tools": ["t4"]}}, "edges": [["plan", "exec"], ["exec", "verify"]], "performance": 0.82, "failure_modes_handled": ["timeout", "oom"], "tool_sequence": ["t1", "t2", "t3", "t4"], "model_lane": "balanced", "task_class": "code_review", "version_id": "v1"},
    {"workflow_id": "wf-beta",  "nodes": {"plan": {"tools": ["t1"]},        "exec": {"tools": ["t3", "t5"]}, "verify": {"tools": ["t4"]}}, "edges": [["plan", "exec"], ["exec", "verify"]], "performance": 0.71, "failure_modes_handled": ["timeout"],       "tool_sequence": ["t1", "t3", "t5", "t4"], "model_lane": "fast",     "task_class": "code_review", "version_id": "v2"},
    {"workflow_id": "wf-gamma", "nodes": {"plan": {"tools": ["t1", "t2"]}, "exec": {"tools": ["t3"]},         "review": {"tools": ["t6"]}}, "edges": [["plan", "exec"], ["exec", "review"]], "performance": 0.65, "failure_modes_handled": ["oom", "drift"], "tool_sequence": ["t1", "t2", "t3", "t6"], "model_lane": "quality", "task_class": "code_review", "version_id": "v3"},
]


# (a) — no usable candidates for empty/non-list input.
def test_empty_history_returns_empty_candidates():
    assert ge.propose([]) == []
    assert ge.propose("[]") == []
    assert ge.propose(None) == []


# (b)
def test_invalid_json_raises_runtime_error():
    with pytest.raises(RuntimeError) as exc:
        ge.propose("{not valid json")
    assert "invalid JSON" in str(exc.value)


# (c)
def test_n_candidates_respected_via_env_var_default_5(monkeypatch):
    monkeypatch.delenv("MINI_ORK_GROUP_CANDIDATES", raising=False)
    assert len(ge.propose(deepcopy(HISTORY))) == 5
    assert len(ge.propose(deepcopy(HISTORY), n_candidates=3)) == 3
    monkeypatch.setenv("MINI_ORK_GROUP_CANDIDATES", "7")
    assert len(ge.propose(deepcopy(HISTORY))) == 7  # env wins when arg is None


# (d)
def test_all_eight_mutation_types_present_after_500_samples():
    seen: set[str] = set()
    for seed in range(5):
        seen.update(c["mutation_type"] for c in ge.propose(deepcopy(HISTORY), n_candidates=100, seed=seed))
    assert seen == set(ge.MUTATION_TYPES)


# (e) — random.seed() makes random.choice deterministic; candidate_id from
# uuid.uuid4() and proposed_at from time.time() legitimately differ.
def test_determinism_under_seed_for_python_port():
    a = ge.propose(deepcopy(HISTORY), n_candidates=10, seed=42)
    b = ge.propose(deepcopy(HISTORY), n_candidates=10, seed=42)
    assert len(a) == len(b) == 10
    # Content determinism (random.choice-controlled): same parents picked, same
    # mutation applied, same floats computed.
    assert [c["parent_id"] for c in a] == [c["parent_id"] for c in b]
    assert [c["mutation_type"] for c in a] == [c["mutation_type"] for c in b]
    assert [c["selection_score"] for c in a] == [c["selection_score"] for c in b]
    assert [c["novelty_estimated"] for c in a] == [c["novelty_estimated"] for c in b]
    # Envelope identity (uuid4 + time-controlled) intentionally NOT compared.
    # Different seed → different content (with overwhelming probability).
    c = ge.propose(deepcopy(HISTORY), n_candidates=10, seed=99)
    assert [x["mutation_type"] for x in c] != [c["mutation_type"] for c in a]


# (f) — candidate envelope shape: keys, id format, float ranges.
def test_propose_returns_well_formed_candidate_envelopes():
    cands = ge.propose(deepcopy(HISTORY), n_candidates=5)
    assert len(cands) == 5
    keys = None
    for c in cands:
        assert isinstance(c, dict)
        # every candidate has the same key set
        if keys is None:
            keys = set(c)
        else:
            assert set(c) == keys
        assert isinstance(c["mutation_applied"], dict) and "type" in c["mutation_applied"]
        assert c["mutation_type"] in ge.MUTATION_TYPES
        assert c["candidate_id"].startswith("wc-") and len(c["candidate_id"]) == 19
        assert isinstance(c["proposed_at"], int) and c["proposed_at"] > 0
        assert c["parent_id"]  # present and non-empty (parent workflow id)
        assert 0.0 <= c["selection_score"] <= 1.0
        assert 0.0 <= c["novelty_estimated"] <= 1.0


@pytest.mark.parametrize("n", [2, 3])
def test_nondefault_count_and_parent_id(n):
    cands = ge.propose(deepcopy(HISTORY), n_candidates=n)
    assert len(cands) == n
    for c in cands:
        assert "parent_id" in c, f"candidate missing parent_id key: {c}"


# (g) — novelty_score is fully deterministic (no random.choice inside).
def test_novelty_score_formula_boundaries_and_determinism():
    cases = [
        (HISTORY[0], HISTORY),
        (HISTORY[1], HISTORY),
        ({"nodes": {"only_x": {}}, "failure_modes_handled": ["x"]}, HISTORY),
        ({"nodes": {}, "failure_modes_handled": []}, HISTORY),
        (HISTORY[0], [HISTORY[0]]),
        ({"nodes": {"a": {"tools": ["t1"]}}, "failure_modes_handled": ["e1"]}, []),
    ]
    for cand, hist in cases:
        score = ge.novelty_score(cand, hist)
        assert 0.0 <= score <= 1.0
        # deterministic: same inputs → same score
        assert ge.novelty_score(cand, hist) == score

    # Boundary semantics of the 3-dim formula:
    # empty history → maximally novel
    assert ge.novelty_score(HISTORY[0], []) == 1.0
    # candidate identical to the only history item → zero novelty
    assert ge.novelty_score(HISTORY[0], [HISTORY[0]]) == 0.0
    # a fully-disjoint candidate is more novel than a history member
    disjoint = {"nodes": {"zzz_new": {"tools": ["t9"]}},
                "failure_modes_handled": ["never-seen-mode"]}
    assert (ge.novelty_score(disjoint, HISTORY)
            > ge.novelty_score(HISTORY[0], HISTORY))
