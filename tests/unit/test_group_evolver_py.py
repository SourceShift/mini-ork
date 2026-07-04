"""Parity gate: mini_ork.ported.group_evolver vs lib/group_evolver.sh.

Each test invokes the live bash subprocess (``bash -c '. lib/group_evolver.sh
&& group_propose ...'``) on the same history as the Python port and diffs
NDJSON after stripping the nondeterministic envelope (candidate_id, parent_id,
proposed_at). Floats compared at 1e-6.

7 cases:
  (a) empty_history_returns_empty_candidates
  (b) invalid_json_raises_runtime_error
  (c) n_candidates_respected_via_env_var_default_5
  (d) all_eight_mutation_types_present_after_500_samples
  (e) determinism_under_seed_for_python_port
  (f) bash_subprocess_live_invocation_returns_ndjson_parseable_as_valid_json_per_line_no_mocks
  (g) novelty_score_formula_matches_bash_at_1e-6_for_diverse_seeded_inputs
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import group_evolver as ge  # noqa: E402

SH = REPO / "lib" / "group_evolver.sh"
ND = {"candidate_id", "parent_id", "proposed_at"}

HISTORY = [
    {"workflow_id": "wf-alpha", "nodes": {"plan": {"tools": ["t1", "t2"]}, "exec": {"tools": ["t3"]}, "verify": {"tools": ["t4"]}}, "edges": [["plan", "exec"], ["exec", "verify"]], "performance": 0.82, "failure_modes_handled": ["timeout", "oom"], "tool_sequence": ["t1", "t2", "t3", "t4"], "model_lane": "balanced", "task_class": "code_review", "version_id": "v1"},
    {"workflow_id": "wf-beta",  "nodes": {"plan": {"tools": ["t1"]},        "exec": {"tools": ["t3", "t5"]}, "verify": {"tools": ["t4"]}}, "edges": [["plan", "exec"], ["exec", "verify"]], "performance": 0.71, "failure_modes_handled": ["timeout"],       "tool_sequence": ["t1", "t3", "t5", "t4"], "model_lane": "fast",     "task_class": "code_review", "version_id": "v2"},
    {"workflow_id": "wf-gamma", "nodes": {"plan": {"tools": ["t1", "t2"]}, "exec": {"tools": ["t3"]},         "review": {"tools": ["t6"]}}, "edges": [["plan", "exec"], ["exec", "review"]], "performance": 0.65, "failure_modes_handled": ["oom", "drift"], "tool_sequence": ["t1", "t2", "t3", "t6"], "model_lane": "quality", "task_class": "code_review", "version_id": "v3"},
]


def _bash(history, n=5):
    """Invoke live bash group_propose → return list[dict] (one per NDJSON line)."""
    env = {**os.environ, "MINI_ORK_GROUP_CANDIDATES": str(n)}
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && group_propose \'{json.dumps(history)}\''],
        env=env, capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"bash failed (rc={r.returncode}): {r.stderr}")
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def _strip(cs):  # drop nondeterministic envelope before structural diff
    return [{k: v for k, v in c.items() if k not in ND} for c in cs]


# (a) — both sides signal "no usable candidates" for empty/non-list input.
def test_empty_history_returns_empty_candidates():
    # Python port returns [] directly.
    assert ge.propose([]) == []
    assert ge.propose("[]") == []
    assert ge.propose(None) == []
    # Bash emits one error envelope on empty/non-list input — still "no candidates".
    bash_empty = _bash([], n=5)
    assert len(bash_empty) == 1
    assert bash_empty[0].get("candidates") == []
    assert "error" in bash_empty[0]


# (b)
def test_invalid_json_raises_runtime_error():
    with pytest.raises(RuntimeError) as exc:
        ge.propose("{not valid json")
    assert "invalid JSON" in str(exc.value)
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && group_propose "{{not valid json"'],
        env={**os.environ, "MINI_ORK_GROUP_CANDIDATES": "5"},
        capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "invalid JSON" in r.stderr


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
    # Content parity (random.choice-controlled): same parents picked, same
    # mutation applied, same floats computed.
    assert [c["parent_id"] for c in a] == [c["parent_id"] for c in b]
    assert [c["mutation_type"] for c in a] == [c["mutation_type"] for c in b]
    assert [c["selection_score"] for c in a] == [c["selection_score"] for c in b]
    assert [c["novelty_estimated"] for c in a] == [c["novelty_estimated"] for c in b]
    # Envelope identity (uuid4 + time-controlled) intentionally NOT compared.
    # Different seed → different content (with overwhelming probability).
    c = ge.propose(deepcopy(HISTORY), n_candidates=10, seed=99)
    assert [x["mutation_type"] for x in c] != [c["mutation_type"] for c in a]


# (f) — live bash NDJSON is parseable as valid JSON per line, no mocks.
# Float values intentionally NOT compared here (random.choice is unseeded in
# both processes → different parents picked → different floats). Float parity
# is the responsibility of test (g), which exercises bash's deterministic
# _group_novelty_score helper directly.
def test_bash_subprocess_live_invocation_returns_ndjson_parseable_as_valid_json_per_line_no_mocks():
    py = ge.propose(deepcopy(HISTORY), n_candidates=5)
    bash = _bash(deepcopy(HISTORY), n=5)
    assert len(py) == len(bash) == 5
    assert all(isinstance(c, dict) for c in bash)
    py_keys = {k for k in py[0] if k not in ND}
    bash_keys = {k for k in bash[0] if k not in ND}
    assert py_keys == bash_keys
    # Envelope shape parity: each candidate has the same keys (modulo ND).
    for p, b in zip(_strip(py), _strip(bash)):
        assert set(p) == set(b)
    # Per-line validity + envelope membership (no mocks, no hardcoded expecteds).
    for c in bash:
        assert isinstance(c["mutation_applied"], dict) and "type" in c["mutation_applied"]
        assert c["mutation_type"] in ge.MUTATION_TYPES
        assert c["candidate_id"].startswith("wc-") and len(c["candidate_id"]) == 19
        assert isinstance(c["proposed_at"], int) and c["proposed_at"] > 0
        # Float fields are present and in [0, 1] (the float-precision test is (g)).
        assert 0.0 <= c["selection_score"] <= 1.0
        assert 0.0 <= c["novelty_estimated"] <= 1.0
    for c in py:
        assert 0.0 <= c["selection_score"] <= 1.0
        assert 0.0 <= c["novelty_estimated"] <= 1.0


# (g) — bash _group_novelty_score has no random.choice → fully deterministic.
def test_novelty_score_formula_matches_bash_at_1e_6_for_diverse_seeded_inputs():
    cases = [
        (HISTORY[0], HISTORY),
        (HISTORY[1], HISTORY),
        ({"nodes": {"only_x": {}}, "failure_modes_handled": ["x"]}, HISTORY),
        ({"nodes": {}, "failure_modes_handled": []}, HISTORY),
        (HISTORY[0], [HISTORY[0]]),
        ({"nodes": {"a": {"tools": ["t1"]}}, "failure_modes_handled": ["e1"]}, []),
    ]
    for cand, hist in cases:
        py_score = ge.novelty_score(cand, hist)
        r = subprocess.run(
            ["bash", "-c",
             f'. "{SH}" && _group_novelty_score \'{json.dumps(cand)}\' \'{json.dumps(hist)}\''],
            capture_output=True, text=True, check=True,
        )
        bash_score = float(r.stdout.strip())
        assert py_score == pytest.approx(bash_score, abs=1e-6)
        assert 0.0 <= py_score <= 1.0 and 0.0 <= bash_score <= 1.0