"""Unit tests for ``mini_ork/gates/suite_strengthen.py`` (SWE-ABS, shadow).

Hermetic: plain temp trees, a fake lane dispatch, and real single-file pytest
runs. No network and no run-local state beyond ``tmp_path``.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from mini_ork.gates import suite_adequacy
from mini_ork.gates import suite_strengthen

CALC = (
    "def clamp(x, lo, hi):\n"
    "    if x < lo:\n"
    "        return lo\n"
    "    if x > hi:\n"
    "        return hi\n"
    "    return x\n"
)

# base/calc.py: line 5 (`        return hi`) becomes `        return lo`, so the
# base clamps above-range values to lo.
BASE_CALC = CALC.replace("        return hi\n", "        return lo\n")

BLOCK_PRESERVE = "from calc import clamp\ndef test_inside():\n    assert clamp(5, 0, 10) == 5\n"
BLOCK_TARGET = "from calc import clamp\ndef test_above():\n    assert clamp(11, 0, 10) == 10\n"
BLOCK_NOKILL = "from calc import clamp\ndef test_exists():\n    assert clamp is not None\n"
BLOCK_RED = "from calc import clamp\ndef test_wrong():\n    assert clamp(11, 0, 10) == 11\n"
BLOCK_HELPER = "def helper():\n    return 1\n"

BLOCKS = [BLOCK_PRESERVE, BLOCK_TARGET, BLOCK_NOKILL, BLOCK_RED, BLOCK_HELPER]
SURVIVOR_IDS = ["M03", "M04", "M05", "M06", "M07", "M08", "M09"]

RESULT_KEYS = {
    "mode", "status", "reason", "targeted", "unmatched", "tests", "admitted",
    "newly_killed", "score_before", "score_after", "verdict_after",
    "would_rescue",
}
TEST_KEYS = {
    "name", "admissible", "why", "on_candidate", "on_base", "kills", "kind",
}


def _write(root, text):
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def trees(tmp_path):
    return _write(tmp_path / "cand", CALC), _write(tmp_path / "base", BASE_CALC)


@pytest.fixture
def mutants(trees):
    cand, _ = trees
    return list(suite_adequacy.generate_mutants(
        str(cand), ["calc.py"], max_mutants=12, seed=0))


def _survivor_dicts(mutant_list, ids=None):
    return [
        {"id": m.id, "file": m.file, "line": m.line, "operator": m.operator,
         "original": m.original, "mutated": m.mutated}
        for m in mutant_list if ids is None or m.id in ids
    ]


@pytest.fixture
def report(mutants):
    return {
        "verdict": "INADEQUATE",
        "reason": "score 0.222 < 0.6",
        "score": 0.222,
        "killed": 2,
        "survived": 7,
        "min_score": 0.6,
        "max_mutants": 12,
        "files": ["calc.py"],
        "survivors": _survivor_dicts(mutants, set(SURVIVOR_IDS)),
    }


@pytest.fixture
def fake_dispatch_factory():
    def make(blocks=BLOCKS, answer=None):
        prompts = []

        def dispatch(prompt):
            prompts.append(prompt)
            if answer is not None:
                return answer
            return "\n\n".join("```python\n" + b + "```" for b in blocks)

        return dispatch, prompts

    return make


def _run(report, trees, dispatch, **kw):
    cand, base = trees
    kwargs = dict(
        issue="clamp ignores hi",
        candidate_dir=str(cand),
        base_dir=str(base),
        dispatch=dispatch,
        python=sys.executable,
        max_tests=10,
        max_survivors=10,
    )
    kwargs.update(kw)
    return suite_strengthen.strengthen(report, **kwargs)


def _snapshot(root):
    snap = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                snap[os.path.relpath(full, root)] = fh.read()
    return snap


def test_full_run(trees, report, fake_dispatch_factory, tmp_path):
    dispatch, _ = fake_dispatch_factory()
    out = tmp_path / "out"
    res = _run(report, trees, dispatch, out_dir=str(out))

    assert set(res) == RESULT_KEYS
    assert res["status"] == "ran"
    assert res["score_before"] == 0.222
    assert res["targeted"] == SURVIVOR_IDS
    assert res["unmatched"] == []
    assert [t["name"] for t in res["tests"]] == ["t1", "t2", "t3", "t4", "t5"]
    for t in res["tests"]:
        assert set(t) == TEST_KEYS, t
    assert [t["kind"] for t in res["tests"]] == [
        "preserve", "target", "no_kill", "candidate_red", "inadmissible"]
    assert res["tests"][0]["kills"] == ["M03", "M05", "M07", "M09"]
    assert res["tests"][1]["kills"] == ["M03", "M06", "M07", "M08"]
    assert res["tests"][3]["on_candidate"] == "failed"
    assert res["tests"][3]["on_base"] == "failed"
    assert res["tests"][4]["why"] == "no top-level test function"
    assert res["tests"][4]["on_candidate"] is None
    assert res["admitted"] == ["t1", "t2"]
    assert res["newly_killed"] == ["M03", "M05", "M06", "M07", "M08", "M09"]
    assert res["score_after"] == 0.889
    assert res["verdict_after"] == "ADEQUATE"
    assert res["would_rescue"] is True

    for name in ("t1", "t2", "t3", "t4", "t5"):
        assert (out / f"{name}.py").is_file(), name
    with open(out / "strengthen.json", encoding="utf-8") as fh:
        assert json.load(fh) == res


def test_full_run_leaves_trees_untouched(trees, report, fake_dispatch_factory, tmp_path):
    cand, base = trees
    before = (_snapshot(cand), _snapshot(base))
    dispatch, _ = fake_dispatch_factory()
    assert _run(report, trees, dispatch, out_dir=str(tmp_path / "out"))["status"] == "ran"
    assert (_snapshot(cand), _snapshot(base)) == before


def test_targeting_is_capped(trees, report, fake_dispatch_factory, tmp_path):
    dispatch, _ = fake_dispatch_factory()
    res = _run(report, trees, dispatch, max_survivors=2,
               out_dir=str(tmp_path / "out"))
    assert res["targeted"] == ["M03", "M04"]
    assert res["admitted"] == ["t1", "t2"]
    assert res["newly_killed"] == ["M03"]
    assert res["score_after"] == 0.333
    assert res["verdict_after"] == "INADEQUATE"
    assert res["would_rescue"] is False


def test_prompt_contents(trees, report, fake_dispatch_factory):
    dispatch, prompts = fake_dispatch_factory()
    _run(report, trees, dispatch)
    (prompt,) = prompts
    assert "STRENGTHEN THE SUITE" in prompt
    assert "clamp ignores hi" in prompt
    assert "M07 calc.py:4 cmp_flip: x > hi -> x <= hi" in prompt
    assert "# file: calc.py" in prompt


def test_no_import_no_admission(trees, report, fake_dispatch_factory, tmp_path):
    dispatch, _ = fake_dispatch_factory(blocks=["def test_x():\n    assert 1 == 1\n"])
    res = _run(report, trees, dispatch, out_dir=str(tmp_path / "out"))
    (only,) = res["tests"]
    assert only["admissible"] is False
    assert only["kind"] == "inadmissible"
    assert only["why"] == "does not import the code under test"
    assert res["admitted"] == []


def test_partial_match(trees, report, fake_dispatch_factory, tmp_path):
    dispatch, _ = fake_dispatch_factory()
    with_m99 = dict(report)
    with_m99["survivors"] = list(report["survivors"]) + [
        dict(report["survivors"][0], id="M99")]
    res = _run(with_m99, trees, dispatch, out_dir=str(tmp_path / "out"))
    assert res["unmatched"] == ["M99"]
    assert res["targeted"] == SURVIVOR_IDS
    assert res["newly_killed"] == ["M03", "M05", "M06", "M07", "M08", "M09"]
    assert res["score_after"] == 0.889


def test_empty_answer(trees, report, fake_dispatch_factory):
    dispatch, _ = fake_dispatch_factory(answer="")
    res = _run(report, trees, dispatch)
    assert res["status"] == "ran"
    assert res["reason"] == "no test generated"
    assert res["tests"] == []
    assert res["score_after"] == 0.222
    assert res["verdict_after"] == "INADEQUATE"
    assert res["would_rescue"] is False


def test_skips_never_dispatch(trees, report):
    def guard(_prompt):
        raise AssertionError("dispatch was called on a skip path")

    def skipped(res, reason):
        assert res["status"] == "skipped", res
        assert res["reason"] == reason, res["reason"]
        assert set(res) == RESULT_KEYS
        assert res["targeted"] == [] and res["tests"] == []
        assert res["admitted"] == [] and res["newly_killed"] == []
        assert res["score_after"] is None and res["verdict_after"] is None
        assert res["would_rescue"] is False

    skipped(_run(dict(report, verdict="ADEQUATE"), trees, guard), "not inadequate")
    skipped(_run(dict(report, survivors=[]), trees, guard), "not inadequate")
    skipped(_run(report, trees, guard, issue="  "), "no kickoff text")

    no_files = dict(report)
    del no_files["files"]
    skipped(_run(no_files, trees, guard), "report lacks files/max_mutants")

    renamed = dict(report)
    renamed["survivors"] = [dict(s, id=f"X{i}") for i, s in enumerate(report["survivors"])]
    res = _run(renamed, trees, guard)
    skipped(res, "no survivor could be regenerated")
    assert res["unmatched"] == [f"X{i}" for i in range(len(renamed["survivors"]))]


def test_mode_parsing():
    env = "MO_SUITE_STRENGTHEN"
    assert suite_strengthen.MODE_ENV == env
    for val in ("0", "off", "false", "no"):
        assert suite_strengthen.mode({env: val}) == "off", val
    for val in ("1", "gate"):
        assert suite_strengthen.mode({env: val}) == "gate", val
    for val in ("", "x", "shadow", "2", "ON"):
        assert suite_strengthen.mode({env: val}) == "shadow", val
    assert suite_strengthen.mode({}) == "shadow"


def test_settings_defaults_and_clamps():
    s = suite_strengthen.settings({})
    assert set(s) == {"max_survivors", "max_tests", "timeout_s"}
    assert s == {"max_survivors": 5, "max_tests": 3, "timeout_s": 60.0}

    s = suite_strengthen.settings({"MO_SUITE_STRENGTHEN_MAX_SURVIVORS": "99"})
    assert s["max_survivors"] == 20
    assert suite_strengthen.settings(
        {"MO_SUITE_STRENGTHEN_MAX_SURVIVORS": "x"})["max_survivors"] == 5
    assert suite_strengthen.settings(
        {"MO_SUITE_STRENGTHEN_MAX_TESTS": "0"})["max_tests"] == 1
    assert suite_strengthen.settings(
        {"MO_SUITE_STRENGTHEN_MAX_TESTS": "50"})["max_tests"] == 10
    assert suite_strengthen.settings(
        {"MO_SUITE_STRENGTHEN_TIMEOUT_S": "0.5"})["timeout_s"] == 1.0
    assert suite_strengthen.settings(
        {"MO_SUITE_STRENGTHEN_TIMEOUT_S": "x"})["timeout_s"] == 60.0


def test_budget_s_default_floor_and_typo():
    assert suite_strengthen.budget_s({}) == 240.0
    assert suite_strengthen.budget_s({"MO_SUITE_STRENGTHEN_BUDGET_S": "30"}) == 30.0
    assert suite_strengthen.budget_s({"MO_SUITE_STRENGTHEN_BUDGET_S": "0"}) == 1.0
    assert suite_strengthen.budget_s({"MO_SUITE_STRENGTHEN_BUDGET_S": "x"}) == 240.0


def test_strengthen_threads_one_deadline_to_lane_and_runner(monkeypatch):
    # The whole pass shares one deadline (now + MO_SUITE_STRENGTHEN_BUDGET_S):
    # the default lane and the pytest runner both get it, so past it the lane
    # is not called and no pytest is spawned (bounded_dispatch / run_in).
    import time as _time

    captured = {}

    class _Runner:
        def __init__(self, base_dir, candidate_dir, *, python=None,
                     timeout_s=120.0, deadline=None):
            captured["deadline"] = deadline

        def run_in(self, tree_dir, test_src):  # pragma: no cover - no tests
            raise AssertionError("no generated test should run")

    def _bounded(deadline=None):
        captured["dispatch_deadline"] = deadline
        return lambda prompt: ""

    monkeypatch.setenv("MO_SUITE_STRENGTHEN_BUDGET_S", "5")
    monkeypatch.setattr(suite_strengthen.codefix_terms, "LocalTreeRunner", _Runner)
    monkeypatch.setattr(suite_strengthen.codefix_terms, "bounded_dispatch", _bounded)
    monkeypatch.setattr(suite_strengthen, "match_survivors",
                        lambda repo, report: ([_FakeMutant()], []))
    before = _time.monotonic()
    rec = suite_strengthen.strengthen(
        {"verdict": "INADEQUATE", "survivors": [{"id": "m1"}], "files": ["calc.py"],
         "max_mutants": 10, "score": 0.4, "killed": 2, "survived": 3},
        issue="clamp bug", candidate_dir="/nonexistent", base_dir="/nonexistent",
    )
    assert rec["status"] == "ran"
    assert rec["reason"] == "no test generated"
    assert before + 4 <= captured["dispatch_deadline"] <= _time.monotonic() + 5
    assert captured["deadline"] == captured["dispatch_deadline"]


class _FakeMutant:
    id = "m1"
    file = "calc.py"
    line = 2
    operator = "BinOp"
    original = "a + b"
    mutated = "a - b"
    source = "def add(a, b):\n    return a - b\n"
