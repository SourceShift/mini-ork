"""Unit tests for mini_ork.certify.relations — the metamorphic-relations oracle term.

Hermetic by design: no docker, no network, no model. A local FakeRunner scripts
ExecOutcome per call in order; a fake dispatch returns canned text keyed on prompt
shape. The two doubles are COPIED (never imported) from test_certify_oracle.py so
relations' own routing stays independent of the invariants suite.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict

import pytest

from mini_ork.certify import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    judge,
)
from mini_ork.certify import relations
from mini_ork.certify.context import CodeContext
from mini_ork.runtime import ExecOutcome


# ── Test doubles ───────────────────────────────────────────────────────────────

ISSUE_TEXT = (
    "The bug is: foo() returns -1 instead of 42.\n"
    "\n"
    "Specifically, calling foo() should return 42 (the documented value), "
    "but it returns -1."
)
ISSUE_QUOTE = "calling foo() should return 42 (the documented value)"
GROUND_JSON = (
    '{"states_expected_behaviour": true, '
    f'"quote": "{ISSUE_QUOTE}", '
    '"expected_summary": "foo returns 42", '
    '"reported_exception": ""}'
)
POC_CODE = (
    "def test_foo_returns_42():\n"
    "    assert foo() == 42\n"
)

# A well-formed metamorphic relation: one top-level test, one SOURCE/FOLLOWUP each,
# TRANSFORM/RELATION strings, an assert that loads both inputs. The FakeRunner never
# executes it — admissible only checks its SHAPE.
REL_SRC = (
    "SOURCE = [1, 2, 3]\n"
    "FOLLOWUP = [4, 5, 6]\n"
    "TRANSFORM = \"shift each element by +3\"\n"
    "RELATION = \"length is preserved\"\n"
    "def test_relation():\n"
    "    assert len(SOURCE) == len(FOLLOWUP)\n"
)


def _mr_block(n: int) -> str:
    """Produce a python block containing n `test_mr_*` functions sharing a header."""
    header = "import pytest\n"
    tests = "\n\n".join(
        f"def test_mr_{i}():\n    assert foo() == 42  # variant {i}" for i in range(1, n + 1)
    )
    return header + tests


def _fence(code: str) -> str:
    """Models answer in a ```python``` block; the probe/invariant parsers require one."""
    return f"```python\n{code}```"


def make_dispatch(mr_n: int = 3, ground: str | None = None, poc: str | None = None,
                  mr_block_n: int | None = None,
                  relations_srcs: list[str] | None = None) -> callable:
    """Build a dispatch callable that returns canned text per prompt stage.

    Routing order matters: the three existing keys come first; the relations key
    (the literal `METAMORPHIC RELATIONS`) sits after them and before the `""` fallback.
    Returning "" represents dispatch failure.
    """
    ground = ground if ground is not None else GROUND_JSON
    poc = poc if poc is not None else POC_CODE
    mr_n_actual = mr_block_n if mr_block_n is not None else mr_n
    relations_srcs = relations_srcs if relations_srcs is not None else [REL_SRC for _ in range(mr_n)]

    def fn(prompt: str) -> str:
        if not prompt:
            return ""
        if "states_expected_behaviour" in prompt:
            return ground
        if "Write ONE pytest test" in prompt:
            return _fence(poc)
        if re.search(r"Write\s+\d+\s+pytest tests", prompt):
            return _fence(_mr_block(mr_n_actual))
        if "METAMORPHIC RELATIONS" in prompt:
            return "\n".join(_fence(r) for r in relations_srcs)
        return ""

    return fn


class FakeRunner:
    """Scripted ExecOutcome per call in order. up is settable for the harness-down path."""

    def __init__(self, outcomes, *, up: bool = True) -> None:
        self._outcomes = list(outcomes)
        self.up = up
        self.calls: list[tuple[str, str]] = []

    def run_test(self, src: str, patch: str = "") -> ExecOutcome:
        self.calls.append((src, patch))
        if not self._outcomes:
            return ExecOutcome(status="error", exc="no scripted outcome")
        return self._outcomes.pop(0)


def _probe_build_pass() -> list[ExecOutcome]:
    return [ExecOutcome(status="failed", exc="AssertionError")]  # run_test(poc, "") — reproduces


def _probe_green() -> list[ExecOutcome]:
    return _probe_build_pass() + [ExecOutcome(status="passed")]  # run_test(poc, patch)


def _clear_rel_env(monkeypatch) -> None:
    for var in ("MO_ASSAY_RELATIONS", "MO_ASSAY_RELATIONS_RESCUE",
                "MO_ASSAY_RELATIONS_K", "MO_ASSAY_RELATIONS_VETO_MIN"):
        monkeypatch.delenv(var, raising=False)


# ── 1. static law ─────────────────────────────────────────────────────────────
def test_admissible_accepts_wellformed():
    ok, why = relations.admissible(REL_SRC)
    assert ok, why


BAD_CASES = [
    ("missing_followup",
     "SOURCE = [1, 2, 3]\nTRANSFORM = \"shift\"\nRELATION = \"order\"\n"
     "def test_relation():\n    assert len(SOURCE) == 3\n",
     "FOLLOWUP"),
    ("identical_inputs",
     "SOURCE = [1, 2, 3]\nFOLLOWUP = [1, 2, 3]\nTRANSFORM = \"none\"\nRELATION = \"same\"\n"
     "def test_relation():\n    assert SOURCE == FOLLOWUP\n",
     "same"),
    ("followup_is_source",
     "SOURCE = [1, 2, 3]\nFOLLOWUP = SOURCE\nTRANSFORM = \"identity\"\nRELATION = \"same\"\n"
     "def test_relation():\n    assert SOURCE == FOLLOWUP\n",
     "SOURCE"),
    ("followup_never_loaded",
     "SOURCE = [1, 2, 3]\nFOLLOWUP = [4, 5, 6]\nTRANSFORM = \"shift\"\nRELATION = \"order\"\n"
     "def test_relation():\n    assert len(SOURCE) == 3\n",
     "FOLLOWUP"),
    ("no_assert",
     "SOURCE = [1, 2, 3]\nFOLLOWUP = [4, 5, 6]\nTRANSFORM = \"shift\"\nRELATION = \"order\"\n"
     "def test_relation():\n    result = SOURCE + FOLLOWUP\n",
     "assert"),
    ("two_tests",
     "SOURCE = [1, 2, 3]\nFOLLOWUP = [4, 5, 6]\nTRANSFORM = \"shift\"\nRELATION = \"order\"\n"
     "def test_relation():\n    assert len(SOURCE) == 3\n"
     "def test_other():\n    assert len(FOLLOWUP) == 3\n",
     "one"),
    ("source_rebound",
     "SOURCE = [1, 2, 3]\nFOLLOWUP = [4, 5, 6]\nTRANSFORM = \"shift\"\nRELATION = \"order\"\n"
     "def test_relation():\n    SOURCE = [7, 8, 9]\n    assert len(SOURCE) == len(FOLLOWUP)\n",
     "rebound"),
    ("no_relation",
     "SOURCE = [1, 2, 3]\nFOLLOWUP = [4, 5, 6]\nTRANSFORM = \"shift\"\n"
     "def test_relation():\n    assert len(SOURCE) == len(FOLLOWUP)\n",
     "RELATION"),
]


@pytest.mark.parametrize("label,src,needle", BAD_CASES)
def test_admissible_rejects(label, src, needle):
    ok, why = relations.admissible(src)
    assert not ok, f"{label}: should be rejected"
    assert needle in why, f"{label}: why={why!r}"


def test_admissible_requires_import_under_context():
    ctx = CodeContext("", ("stats",), ())
    ok, why = relations.admissible(REL_SRC, ctx)
    assert not ok
    assert "import" in why

    redef = (
        "from stats import median\n"
        "SOURCE = [1, 2, 3]\n"
        "FOLLOWUP = [4, 5, 6]\n"
        "TRANSFORM = \"shift\"\n"
        "RELATION = \"median scales\"\n"
        "def test_relation():\n"
        "    def median(xs):\n"
        "        return sorted(xs)[len(xs) // 2]\n"
        "    assert median(SOURCE) == median(FOLLOWUP)\n"
    )
    ok, why = relations.admissible(redef, ctx)
    assert not ok
    assert "import" in why


def test_admissible_accepts_proper_import_under_context():
    ctx = CodeContext("", ("stats",), ())
    src = (
        "from stats import median\n"
        "SOURCE = [1, 2, 3]\n"
        "FOLLOWUP = [4, 5, 6]\n"
        "TRANSFORM = \"shift\"\n"
        "RELATION = \"median scales\"\n"
        "def test_relation():\n"
        "    assert median(SOURCE) <= median(FOLLOWUP)\n"
    )
    ok, why = relations.admissible(src, ctx)
    assert ok, why


# ── 2. special-cased patch: a held-on-base relation breaks on the patch ────────
def test_special_cased_patch_refuted_by_relation(monkeypatch):
    _clear_rel_env(monkeypatch)
    n_invs = 3
    off_outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    v_off = judge(ISSUE_TEXT, "patch text", runner=FakeRunner(list(off_outcomes)), mr_n=n_invs,
                  dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC]))
    assert v_off.verdict == PROVEN, v_off

    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    on_outcomes = off_outcomes + [
        ExecOutcome(status="failed", exc="AssertionError", output="E   assert 2 == 1"),
        ExecOutcome(status="passed"),
    ]
    v_on = judge(ISSUE_TEXT, "patch text", runner=FakeRunner(list(on_outcomes)), mr_n=n_invs,
                 dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC]))

    assert v_on.verdict == REFUTED, v_on
    assert "rel_1" in v_on.reason
    rec = v_on.detail["relations"]
    assert rec["counts"]["violated"] == 1
    record = rec["records"][0]
    assert record["status"] == "violated"
    assert record["pair"] == {"source": "[1, 2, 3]", "followup": "[4, 5, 6]"}
    assert record["detail"]
    # the invariants trail is untouched by the relations hook
    assert v_on.mr_pass_rate == v_off.mr_pass_rate
    assert v_on.mr_n == v_off.mr_n
    assert v_on.detail["invariants"] == v_off.detail["invariants"]


# ── 3. correct patch: every relation holds -> verdict/reason unchanged ─────────
def test_correct_patch_all_relations_hold(monkeypatch):
    _clear_rel_env(monkeypatch)
    n_invs = 3
    off_outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    v_off = judge(ISSUE_TEXT, "patch text", runner=FakeRunner(list(off_outcomes)), mr_n=n_invs,
                  dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC]))

    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    on_outcomes = off_outcomes + [
        ExecOutcome(status="passed"),
        ExecOutcome(status="failed", exc="AssertionError"),
    ]
    v_on = judge(ISSUE_TEXT, "patch text", runner=FakeRunner(list(on_outcomes)), mr_n=n_invs,
                 dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC]))

    assert v_on.verdict == v_off.verdict == PROVEN
    assert v_on.reason == v_off.reason
    rec = v_on.detail["relations"]
    assert rec["counts"]["held"] == 1
    assert rec["counts"]["violated"] == 0


# ── 4. unexecutable relation abstains; base is NOT run ────────────────────────
@pytest.mark.parametrize("patch_outcome", [
    ExecOutcome(status="test_defect", exc="NameError"),
    ExecOutcome(status="error", exc="ImportError"),
    ExecOutcome(status="failed", exc="TypeError"),
])
def test_unexecutable_relation_abstains(monkeypatch, patch_outcome):
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    n_invs = 3
    off_outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    runner = FakeRunner(list(off_outcomes) + [patch_outcome])
    v = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC]))
    assert v.verdict == PROVEN, v
    rec = v.detail["relations"]
    assert rec["counts"]["abstained"] == 1
    assert rec["counts"]["held"] == 0
    # exactly one extra call (the patch run); base was never run
    assert len(runner.calls) == len(off_outcomes) + 1


def test_raising_run_test_abstains(monkeypatch):
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    n_invs = 3
    off_outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]

    class RaisingRunner(FakeRunner):
        def run_test(self, src, patch=""):
            self.calls.append((src, patch))
            if len(self.calls) == len(off_outcomes) + 1:
                raise RuntimeError("sandbox died")
            if not self._outcomes:
                return ExecOutcome(status="error", exc="no scripted outcome")
            return self._outcomes.pop(0)

    v = judge(ISSUE_TEXT, "patch text", runner=RaisingRunner(list(off_outcomes)), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC]))
    assert v.verdict == PROVEN, v
    rec = v.detail["relations"]
    assert rec["counts"]["abstained"] == 1
    assert "run_test raised" in rec["records"][0]["why"]


# ── 5. patch and base both assertion-fail -> violated_unattributed ─────────────
def test_patch_and_base_both_fail_is_violated_unattributed(monkeypatch):
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    n_invs = 3
    off_outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    on_outcomes = off_outcomes + [
        ExecOutcome(status="failed", exc="AssertionError"),
        ExecOutcome(status="failed", exc="AssertionError"),
    ]
    v = judge(ISSUE_TEXT, "patch text", runner=FakeRunner(list(on_outcomes)), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC]))
    assert v.verdict == PROVEN, v
    rec = v.detail["relations"]
    assert rec["counts"]["violated_unattributed"] == 1
    assert rec["counts"]["violated"] == 0


# ── 6. knobs off => byte-identical Verdicts ────────────────────────────────────
def test_knobs_off_byte_identical(monkeypatch):
    scenarios = [
        ("proven",
         _probe_green() + [ExecOutcome(status="failed")] * 3 + [ExecOutcome(status="passed")] * 3,
         3, None),
        ("refuted",
         _probe_green() + [ExecOutcome(status="failed")] * 4 + [ExecOutcome(status="passed")]
         + [ExecOutcome(status="failed")] * 3 + [ExecOutcome(status="failed")] * 3,
         4, None),
        ("unverified",
         _probe_green() + [ExecOutcome(status="failed")] * 4
         + [ExecOutcome(status="passed"), ExecOutcome(status="passed"),
            ExecOutcome(status="failed"), ExecOutcome(status="failed")]
         + [ExecOutcome(status="failed"), ExecOutcome(status="failed")],
         4, None),
        ("no_invariant", _probe_green(), 3, 0),
    ]
    modes = ("unset", "rel0", "rescue1")
    for name, outcomes, mr_n, mr_block_n in scenarios:
        snapshots = {}
        for mode in modes:
            _clear_rel_env(monkeypatch)
            monkeypatch.setenv("MO_ASSAY_RELATIONS", "0")  # DEFAULT ON now; pin OFF
            if mode == "rel0":
                monkeypatch.setenv("MO_ASSAY_RELATIONS", "0")
            elif mode == "rescue1":
                monkeypatch.setenv("MO_ASSAY_RELATIONS_RESCUE", "1")
            seen: list[str] = []
            base = make_dispatch(mr_n=mr_n, mr_block_n=mr_block_n, relations_srcs=[REL_SRC])

            def disp(prompt: str) -> str:
                seen.append(prompt)
                return base(prompt)

            runner = FakeRunner(list(outcomes))
            v = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=mr_n, dispatch=disp)
            snapshots[mode] = (json.dumps(asdict(v), sort_keys=True), tuple(runner.calls), v, seen)

        s0 = snapshots["unset"]
        for mode in ("rel0", "rescue1"):
            assert snapshots[mode][0] == s0[0], f"{name}/{mode}: verdict bytes differ"
            assert snapshots[mode][1] == s0[1], f"{name}/{mode}: runner.calls differ"
        for mode in modes:
            assert "relations" not in snapshots[mode][2].detail, f"{name}/{mode}: relations key leaked"
            for p in snapshots[mode][3]:
                assert "METAMORPHIC RELATIONS" not in p, f"{name}/{mode}: relations prompt dispatched"


# ── 7. knobs on but the relations hook cannot fire ─────────────────────────────
def test_knobs_on_invariants_refute_skips_relations(monkeypatch):
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    n_invs = 4
    outcomes = _probe_green() + [ExecOutcome(status="failed")] * 4 \
        + [ExecOutcome(status="passed")] + [ExecOutcome(status="failed")] * 3 \
        + [ExecOutcome(status="failed")] * 3
    seen: list[str] = []
    base = make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC])

    def disp(prompt: str) -> str:
        seen.append(prompt)
        return base(prompt)

    v = judge(ISSUE_TEXT, "patch text", runner=FakeRunner(list(outcomes)), mr_n=n_invs, dispatch=disp)
    assert v.verdict == REFUTED, v
    assert "relations" not in v.detail
    assert not any("METAMORPHIC RELATIONS" in p for p in seen)


def test_knobs_on_no_invariant_rescue_off_unchanged(monkeypatch):
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_RESCUE", "0")  # "rescue off" is meant here
    seen: list[str] = []
    base = make_dispatch(mr_n=3, mr_block_n=0, relations_srcs=[REL_SRC])

    def disp(prompt: str) -> str:
        seen.append(prompt)
        return base(prompt)

    v = judge(ISSUE_TEXT, "patch text", runner=FakeRunner(list(_probe_green())), mr_n=3, dispatch=disp)
    assert v.verdict == UNVERIFIED, v
    assert "no invariant" in v.reason
    assert "relations" not in v.detail
    assert not any("METAMORPHIC RELATIONS" in p for p in seen)


# ── 8. rescue bar (no-invariant branch, both knobs on) ─────────────────────────
@pytest.mark.parametrize("outcomes,exp_verdict,needle", [
    # 3 held incl. 1 repaired -> PROVEN "relative to"
    ([ExecOutcome(status="passed"), ExecOutcome(status="failed", exc="AssertionError"),
      ExecOutcome(status="passed"), ExecOutcome(status="passed"),
      ExecOutcome(status="passed"), ExecOutcome(status="passed")],
     PROVEN, "relative to"),
    # 3 held, 0 repaired -> UNVERIFIED, original reason
    ([ExecOutcome(status="passed"), ExecOutcome(status="passed"),
      ExecOutcome(status="passed"), ExecOutcome(status="passed"),
      ExecOutcome(status="passed"), ExecOutcome(status="passed")],
     UNVERIFIED, "no invariant"),
    # 2 held + 1 violated -> REFUTED
    ([ExecOutcome(status="passed"), ExecOutcome(status="failed", exc="AssertionError"),
      ExecOutcome(status="passed"), ExecOutcome(status="failed", exc="AssertionError"),
      ExecOutcome(status="failed", exc="AssertionError"), ExecOutcome(status="passed")],
     REFUTED, "rel_"),
    # 1 held + 2 abstained -> UNVERIFIED
    ([ExecOutcome(status="passed"), ExecOutcome(status="failed", exc="AssertionError"),
      ExecOutcome(status="test_defect", exc="NameError"),
      ExecOutcome(status="error", exc="ImportError")],
     UNVERIFIED, "no invariant"),
])
def test_rescue_bar(monkeypatch, outcomes, exp_verdict, needle):
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_RESCUE", "1")
    n_rels = 3
    rels = [REL_SRC for _ in range(n_rels)]
    runner = FakeRunner(list(_probe_green()) + list(outcomes))
    v = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=3,
              dispatch=make_dispatch(mr_n=3, mr_block_n=0, relations_srcs=rels))
    assert v.verdict == exp_verdict, v
    assert needle in v.reason


# ── 9. K knob parsing + execution cap ──────────────────────────────────────────
def test_k_from_env_values(monkeypatch):
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "2")
    assert relations.k_from_env() == 2
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "99")
    assert relations.k_from_env() == 5
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "abc")
    assert relations.k_from_env() == 3


def test_k_caps_relations_executed(monkeypatch):
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "2")
    rels = [REL_SRC for _ in range(5)]
    admitted, rejected = relations.build(
        "poc", "issue", "patch",
        k=relations.k_from_env(),
        dispatch=make_dispatch(mr_n=3, relations_srcs=rels),
    )
    assert len(admitted) == 2
    assert [n for n, _ in admitted] == ["rel_1", "rel_2"]
    assert rejected == []


# ── 10. stderr observability line ──────────────────────────────────────────────
def test_stderr_observability_line(capsys):
    runner = FakeRunner([ExecOutcome(status="passed"),
                         ExecOutcome(status="failed", exc="AssertionError")])
    relations.check("poc", "issue", "patch", runner=runner,
                    dispatch=make_dispatch(mr_n=3, relations_srcs=[REL_SRC]), k=1)
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.startswith("[assay-relations] ")]
    assert len(lines) == 1
    payload = json.loads(lines[0].replace("[assay-relations] ", "", 1))
    assert "counts" in payload and "records" in payload
    for r in payload["records"]:
        assert "src" not in r
        assert "detail" not in r


def test_enabled_default_on(monkeypatch):
    monkeypatch.delenv("MO_ASSAY_RELATIONS", raising=False)
    assert relations.enabled() is True
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "0")
    assert relations.enabled() is False


def test_rescue_enabled_default_on(monkeypatch):
    monkeypatch.delenv("MO_ASSAY_RELATIONS_RESCUE", raising=False)
    assert relations.rescue_enabled() is True
    monkeypatch.setenv("MO_ASSAY_RELATIONS_RESCUE", "0")
    assert relations.rescue_enabled() is False
