"""Tests for safe metamorphic auto-proposal (Layer 2 auto-wiring).

The core safety property: an LLM proposal can only ever select relations from
the vetted library by name — arbitrary/garbage names are dropped and nothing
model-authored is executed. Plus an end-to-end run of the verifier on a
data-only JSON spec (no Python module), catching a cheat."""

import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.learning import metamorphic as mm
from mini_ork.learning import metamorphic_proposer as mp

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "code-fix" / "verifiers" / "metamorphic.py"


# ── the safety boundary: only whitelisted relation NAMES survive ─────────────
def test_parse_proposal_drops_unknown_relations():
    text = ('{"relations": ["commutativity", "arbitrary_predicate", "__import__", '
            '"rm -rf /"], "seed_inputs": [[2, 3]]}')
    prop = mp.parse_proposal(text)
    assert prop["relations"] == ["commutativity"]  # unknown/garbage dropped
    assert prop["seed_inputs"] == [[2, 3]]


def test_parse_proposal_keeps_only_json_seed_inputs():
    text = '{"relations": ["determinism"], "seed_inputs": [1, "a", [1, 2], {"k": 1}]}'
    prop = mp.parse_proposal(text)
    assert prop["seed_inputs"] == [1, "a", [1, 2], {"k": 1}]


def test_parse_proposal_handles_garbage():
    assert mp.parse_proposal("not json") == {"relations": [], "seed_inputs": []}
    assert mp.parse_proposal("") == {"relations": [], "seed_inputs": []}
    assert mp.parse_proposal('{"relations": "notalist"}') == {"relations": [], "seed_inputs": []}


def test_parse_proposal_from_fenced_json_with_prose():
    text = 'Sure:\n```json\n{"relations": ["order_invariance"], "seed_inputs": [[3, 1, 2]]}\n```'
    prop = mp.parse_proposal(text)
    assert prop["relations"] == ["order_invariance"]


# ── the library + resolver ───────────────────────────────────────────────────
def test_resolve_relations_only_known_names():
    rels = mm.resolve_relations(["commutativity", "determinism", "bogus"])
    assert [r.name for r in rels] == ["commutativity", "determinism"]
    assert all(isinstance(r, mm.MetamorphicRelation) for r in rels)


def test_library_catalog_lists_every_relation():
    names = {c["name"] for c in mm.library_catalog()}
    assert names == set(mm.RELATION_LIBRARY)
    assert "commutativity" in names


def test_build_proposer_prompt_lists_catalog_and_function():
    prompt = mp.build_proposer_prompt("add", "def add(x):\n    a,b=x\n    return a+b")
    assert "add" in prompt
    for name in mm.RELATION_LIBRARY:
        assert name in prompt
    assert '"relations"' in prompt  # asks for the strict JSON envelope


def test_to_spec_shape():
    prop = {"relations": ["commutativity"], "seed_inputs": [[2, 3]]}
    spec = mp.to_spec("patched.py", "add", prop)
    assert spec["target"] == {"module": "patched.py", "function": "add"}
    assert spec["relations"] == ["commutativity"]
    assert spec["seed_inputs"] == [[2, 3]]


# ── end-to-end: verifier runs a DATA-ONLY json spec (no module code) ─────────
def _run_verifier(env_extra):
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(VERIFIER)],
                          capture_output=True, text=True, env=env)


def test_verifier_json_spec_catches_cheat(tmp_path):
    target = tmp_path / "patched.py"
    target.write_text("def add(x):\n    a, b = x\n    return 5 if (a, b) == (2, 3) else 0\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "target": {"module": str(target), "function": "add"},
        "seed_inputs": [[2, 3]],
        "relations": ["commutativity"],  # a NAME — resolved from the whitelist
    }))
    proc = _run_verifier({"MO_METAMORPHIC_SPEC_JSON": str(spec)})
    assert proc.returncode == 1
    out = json.loads(proc.stdout.strip())
    assert out["pass"] is False
    assert out["violations"] >= 1


def test_verifier_json_spec_ignores_unknown_relation_names(tmp_path):
    target = tmp_path / "patched.py"
    target.write_text("def add(x):\n    a, b = x\n    return a + b\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "target": {"module": str(target), "function": "add"},
        "seed_inputs": [[2, 3]],
        "relations": ["definitely_not_a_relation"],  # dropped by resolve_relations
    }))
    proc = _run_verifier({"MO_METAMORPHIC_SPEC_JSON": str(spec)})
    assert proc.returncode == 0  # unknown names are dropped, never executed
    out = json.loads(proc.stdout.strip())
    # no violation and no crash: the garbage relation name simply never runs;
    # only the universal immutability check does (add is pure → holds).
    assert out.get("pass") is not False
