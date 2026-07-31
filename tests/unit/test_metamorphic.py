"""Unit tests for the Layer-2 metamorphic engine (mini_ork/learning/metamorphic.py).

The headline test is `test_catches_coverage_gap_cheat`: a patch that passes its
one extensional test but is wrong under a metamorphic relation — exactly the
failure a single verifier misses and Layer 2 exists to catch."""

import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.learning import metamorphic as mm
from mini_ork.learning import eval_judge as ej

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "code-fix" / "verifiers" / "metamorphic.py"


# ── the point of Layer 2: catch what a single test can't ─────────────────────
def test_catches_coverage_gap_cheat():
    """add((2,3))==5 passes the given test, but the function is a hardcoded
    cheat that's wrong everywhere else. Commutativity (execution-grounded)
    exposes it: add((2,3))=5 but add((3,2))=0."""
    def sneaky_add(x):
        a, b = x
        return 5 if (a, b) == (2, 3) else 0  # overfit to the one test input

    # the extensional check the cheat is tuned to pass
    assert sneaky_add((2, 3)) == 5

    res = mm.check(sneaky_add, [(2, 3)], [mm.commutativity()])
    assert not res.passed
    assert res.violations >= 1
    assert any(c["relation"] == "commutativity" for c in res.counterexamples)


def test_correct_function_passes_same_relation():
    def add(x):
        a, b = x
        return a + b

    res = mm.check(add, [(2, 3), (7, 11)], [mm.commutativity()])
    assert res.passed
    assert res.violations == 0
    assert res.checks > 0


# ── universal relations (no task spec needed) ────────────────────────────────
def test_determinism_catches_hidden_state():
    counter = {"n": 0}

    def nondeterministic(x):
        counter["n"] += 1
        return counter["n"]  # depends on call history, not input

    res = mm.check(nondeterministic, [1], [mm.determinism()], check_immutability=False)
    assert not res.passed
    assert res.per_relation["determinism"]["violations"] >= 1


def test_immutability_catches_input_mutation():
    def mutating(x):
        x.append(99)  # mutates the caller's list — a universal correctness bug
        return sum(x)

    res = mm.check(mutating, [[1, 2, 3]], [], check_immutability=True)
    assert not res.passed
    assert res.per_relation["input_immutability"]["violations"] == 1


def test_pure_function_passes_universal_checks():
    def pure(x):
        return x * 2

    res = mm.check(pure, [1, 2, 3], list(mm.UNIVERSAL_RELATIONS), check_immutability=True)
    assert res.passed
    assert res.violations == 0


# ── robustness: bad proposals don't create false violations ──────────────────
def test_bad_transform_is_skipped_not_a_violation():
    def add(x):
        a, b = x
        return a + b

    bad = mm.MetamorphicRelation("bad", lambda x: 1 / 0, lambda a, b: a == b)
    res = mm.check(add, [(1, 2)], [bad], check_immutability=False)
    assert res.passed          # no real violation
    assert res.skipped >= 1    # the exploding transform was skipped


def test_relation_that_errors_is_skipped():
    def f(x):
        return x

    boom = mm.MetamorphicRelation("boom", lambda x: x, lambda a, b: 1 / 0)
    res = mm.check(f, [1], [boom], check_immutability=False)
    assert res.passed
    assert res.skipped >= 1
    assert res.checks == 0     # the erroring check was rolled back


def test_deterministic_error_is_not_a_determinism_violation():
    def always_raises(x):
        raise ValueError("boom")

    res = mm.check(always_raises, [1], [mm.determinism()], check_immutability=False)
    assert res.passed  # raising the SAME error twice is still deterministic


# ── verifier envelope + its flow into Layer 0 ────────────────────────────────
def test_to_verifier_json_vacuous_when_nothing_ran():
    res = mm.check(lambda x: x, [1], [], check_immutability=False)
    env = res.to_verifier_json()
    assert env["verdict"] == "vacuous"          # examined nothing
    assert "pass" not in env


def test_to_verifier_json_pass_and_fail_shape():
    def add(x):
        a, b = x
        return a + b

    good = mm.check(add, [(1, 2)], [mm.commutativity()]).to_verifier_json()
    assert good["pass"] is True
    assert good["pass_fraction"] == 1.0

    def cheat(x):
        return 5 if x == (2, 3) else 0

    bad = mm.check(cheat, [(2, 3)], [mm.commutativity()]).to_verifier_json()
    assert bad["pass"] is False
    assert bad["pass_fraction"] < 1.0
    assert bad["violations"] >= 1


def test_metamorphic_result_flows_into_execution_reward():
    """A metamorphic verifier is just another verifier_*.json to Layer 0:
    a metamorphic failure drags the execution reward down; a vacuous one is
    excluded (never inflates it)."""
    def cheat(x):
        return 5 if x == (2, 3) else 0

    meta_fail = mm.check(cheat, [(2, 3)], [mm.commutativity()]).to_verifier_json()
    r, _ = ej.execution_reward({"test": {"pass": True}, "metamorphic": meta_fail})
    assert r == 0.5  # 1 test passed, metamorphic failed → 1 of 2

    meta_vacuous = mm.check(lambda x: x, [1], []).to_verifier_json()
    r2, _ = ej.execution_reward({"test": {"pass": True}, "metamorphic": meta_vacuous})
    assert r2 == 1.0  # vacuous metamorphic excluded → only the test counts


# ── end-to-end: the recipe verifier script (spec-driven) ─────────────────────
def _run_verifier(env_extra):
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(VERIFIER)],
                          capture_output=True, text=True, env=env)


def test_verifier_vacuous_without_spec():
    proc = _run_verifier({"MO_METAMORPHIC_SPEC": ""})
    assert proc.returncode == 0
    assert json.loads(proc.stdout.strip())["verdict"] == "vacuous"


def test_verifier_fails_on_a_cheat_spec(tmp_path):
    spec = tmp_path / "spec.py"
    spec.write_text(
        "from mini_ork.learning.metamorphic import commutativity\n"
        "def _cheat(x):\n"
        "    a, b = x\n"
        "    return 5 if (a, b) == (2, 3) else 0\n"
        "TARGET = _cheat\n"
        "SEED_INPUTS = [(2, 3)]\n"
        "RELATIONS = [commutativity()]\n"
    )
    proc = _run_verifier({"MO_METAMORPHIC_SPEC": str(spec)})
    assert proc.returncode == 1  # a real metamorphic violation → fail
    out = json.loads(proc.stdout.strip())
    assert out["pass"] is False
    assert out["violations"] >= 1


def test_verifier_passes_on_a_correct_spec(tmp_path):
    spec = tmp_path / "spec.py"
    spec.write_text(
        "from mini_ork.learning.metamorphic import commutativity\n"
        "def _add(x):\n"
        "    a, b = x\n"
        "    return a + b\n"
        "TARGET = _add\n"
        "SEED_INPUTS = [(2, 3), (10, 20)]\n"
        "RELATIONS = [commutativity()]\n"
    )
    proc = _run_verifier({"MO_METAMORPHIC_SPEC": str(spec)})
    assert proc.returncode == 0
    assert json.loads(proc.stdout.strip())["pass"] is True
