"""Unit tests for mini_ork.certify.differential — the differential behavioural-equivalence term.

Hermetic by design: no docker, no network, no model. A `DiffRunner` scripts ExecOutcome
per (case, side) keyed on the harness's `MO_DIFF_CASE` marker; a fake dispatch returns
canned text keyed on prompt shape. The doubles are COPIED (never imported) from
test_certify_relations.py so the two suites' routing stays independent.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict

import pytest

from mini_ork.certify import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    judge,
)
from mini_ork.certify import differential
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

# A well-formed metamorphic relation (for the relations+differential interaction tests).
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


# A well-formed differential input suite: 1 bug_domain + 3 preserve, one observe.
SUITE_SRC = (
    "from stats import median\n"
    "CASES = [\n"
    "    (\"bug_domain\", [1, 2, 3, 4]),\n"
    "    (\"preserve\", [1, 2, 3]),\n"
    "    (\"preserve\", [5]),\n"
    "    (\"preserve\", [3, 1, 2]),\n"
    "]\n"
    "def observe(x):\n"
    "    return median(x)\n"
)

# A 2-case suite (1 + 1) for the check-level exclusion tests.
SUITE_2 = (
    "from stats import median\n"
    "CASES = [\n"
    "    (\"bug_domain\", [1, 2, 3, 4]),\n"
    "    (\"preserve\", [1, 2, 3]),\n"
    "]\n"
    "def observe(x):\n"
    "    return median(x)\n"
)

# A self-contained suite (no import) for the real-pytest transport test.
PURE_SUITE = (
    "def median(x):\n"
    "    return sorted(x)[len(x) // 2]\n"
    "CASES = [\n"
    "    (\"bug_domain\", [1, 2, 3, 4]),\n"
    "    (\"preserve\", [1, 2, 3]),\n"
    "    (\"preserve\", [5]),\n"
    "    (\"preserve\", [3, 1, 2]),\n"
    "]\n"
    "def observe(x):\n"
    "    return median(x)\n"
)


def make_dispatch(mr_n: int = 3, ground: str | None = None, poc: str | None = None,
                  mr_block_n: int | None = None,
                  relations_srcs: list[str] | None = None,
                  diff_suite: str | None = None) -> Callable[[str], str]:
    """Build a dispatch callable that returns canned text per prompt stage.

    Routing order matters: the three existing keys come first, then the relations key
    (`METAMORPHIC RELATIONS`), then the differential key (`DIFFERENTIAL INPUT SUITE`),
    then the `""` fallback. Returning "" represents dispatch failure.
    """
    ground = ground if ground is not None else GROUND_JSON
    poc = poc if poc is not None else POC_CODE
    mr_n_actual = mr_block_n if mr_block_n is not None else mr_n
    relations_srcs = relations_srcs if relations_srcs is not None else [REL_SRC for _ in range(mr_n)]
    diff_suite = diff_suite if diff_suite is not None else SUITE_SRC

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
        if "DIFFERENTIAL INPUT SUITE" in prompt:
            return _fence(diff_suite)
        return ""

    return fn


class DiffRunner:
    """Serves ExecOutcome per (case, side) keyed on the harness's `MO_DIFF_CASE`.

    A harness src (matching `MO_DIFF_CASE = (\\d+)`) is served from `cases[(i, side)]`,
    side = "head" iff `patch` is non-empty. A value may be an ExecOutcome or a list
    (served in order, last repeated — the confirm re-run reuses the last). Any other
    src pops `ordered` (an error outcome when empty).
    """

    def __init__(self, ordered, cases) -> None:
        self._ordered = list(ordered)
        self._cases = dict(cases)
        self.up = True
        self.calls: list[tuple[str, str]] = []

    def _serve_case(self, i: int, patch: str) -> ExecOutcome:
        key = (i, "head" if patch else "base")
        val = self._cases.get(key)
        if val is None:
            return ExecOutcome(status="error", exc="no scripted case")
        if isinstance(val, list):
            return val.pop(0) if len(val) > 1 else val[0]
        return val

    def run_test(self, src: str, patch: str = "") -> ExecOutcome:
        self.calls.append((src, patch))
        m = re.search(r"MO_DIFF_CASE = (\d+)", src)
        if m:
            return self._serve_case(int(m.group(1)), patch)
        if not self._ordered:
            return ExecOutcome(status="error", exc="no scripted outcome")
        return self._ordered.pop(0)


def mo_line(i: int, value) -> str:
    """The `MO_DIFF_OBS <json>` line the harness emits for `observe(...) == value`."""
    canon = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    line = json.dumps({"case": i, "obs": canon[:300],
                       "sha256": hashlib.sha256(canon.encode()).hexdigest()}, sort_keys=True)
    return "MO_DIFF_OBS " + line


def obs_outcome(i: int, value) -> ExecOutcome:
    """A passed ExecOutcome whose tail carries the harness's observation line."""
    return ExecOutcome(status="passed", output="1 passed\n" + mo_line(i, value) + "\nCRUCIBLE_RC=0\n")


def _raw_marker(case: int, obs, sha: str) -> str:
    line = json.dumps({"case": case, "obs": obs, "sha256": sha}, sort_keys=True)
    return "1 passed\nMO_DIFF_OBS " + line + "\nCRUCIBLE_RC=0\n"


def _probe_build_pass() -> list[ExecOutcome]:
    return [ExecOutcome(status="failed", exc="AssertionError")]  # run_test(poc, "") — reproduces


def _probe_green() -> list[ExecOutcome]:
    return _probe_build_pass() + [ExecOutcome(status="passed")]  # run_test(poc, patch)


def _clear_diff_env(monkeypatch) -> None:
    for var in ("MO_ASSAY_DIFFERENTIAL", "MO_ASSAY_DIFFERENTIAL_N", "MO_ASSAY_DIFFERENTIAL_VETO_MIN"):
        monkeypatch.delenv(var, raising=False)


def _clear_rel_env(monkeypatch) -> None:
    for var in ("MO_ASSAY_RELATIONS", "MO_ASSAY_RELATIONS_RESCUE",
                "MO_ASSAY_RELATIONS_K", "MO_ASSAY_RELATIONS_VETO_MIN"):
        monkeypatch.delenv(var, raising=False)


def _run_check(monkeypatch, cases, *, n: int = 4, diff_suite: str = SUITE_SRC, extra_env=None):
    """Run `differential.check` with a scripted DiffRunner; return `(rec, runner)`."""
    _clear_diff_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    for k, v in (extra_env or {}).items():
        monkeypatch.setenv(k, v)
    runner = DiffRunner([], cases)
    rec = differential.check("poc", ISSUE_TEXT, "patch text", runner=runner,
                             dispatch=make_dispatch(mr_n=3, diff_suite=diff_suite), n=n)
    return rec, runner


# ── 1. static law ─────────────────────────────────────────────────────────────
def test_admissible_accepts_wellformed():
    ok, why = differential.admissible(SUITE_SRC)
    assert ok, why


def test_cases_returns_partition_input_pairs():
    got = differential.cases(SUITE_SRC)
    assert got == [
        ("bug_domain", "[1, 2, 3, 4]"),
        ("preserve", "[1, 2, 3]"),
        ("preserve", "[5]"),
        ("preserve", "[3, 1, 2]"),
    ]


BAD_CASES = [
    ("no_cases",
     "from stats import median\n"
     "def observe(x):\n    return median(x)\n",
     "CASES"),
    ("non_literal_cases",
     "from stats import median\n"
     "CASES = make_cases()\n"
     "def observe(x):\n    return median(x)\n",
     "literal"),
    ("bad_label",
     "from stats import median\n"
     "CASES = [(\"wrong\", [1, 2]), (\"preserve\", [3])]\n"
     "def observe(x):\n    return median(x)\n",
     "preserve"),
    ("no_preserve",
     "from stats import median\n"
     "CASES = [(\"bug_domain\", [1, 2]), (\"bug_domain\", [3])]\n"
     "def observe(x):\n    return median(x)\n",
     "preserve"),
    ("no_bug_domain",
     "from stats import median\n"
     "CASES = [(\"preserve\", [1, 2]), (\"preserve\", [3])]\n"
     "def observe(x):\n    return median(x)\n",
     "bug_domain"),
    ("duplicate_inputs",
     "from stats import median\n"
     "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [1, 2])]\n"
     "def observe(x):\n    return median(x)\n",
     "identical"),
    ("no_observe",
     "from stats import median\n"
     "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [3])]\n",
     "observe"),
    ("two_arg_observe",
     "from stats import median\n"
     "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [3])]\n"
     "def observe(x, y):\n    return median(x)\n",
     "one positional"),
    ("top_level_test",
     "from stats import median\n"
     "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [3])]\n"
     "def observe(x):\n    return median(x)\n"
     "def test_foo():\n    assert True\n",
     "test"),
    ("mo_diff_in_source",
     "from stats import median\n"
     "# MO_DIFF is reserved\n"
     "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [3])]\n"
     "def observe(x):\n    return median(x)\n",
     "MO_DIFF"),
    ("cases_rebound",
     "from stats import median\n"
     "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [3])]\n"
     "def observe(x):\n    return median(x)\n"
     "def f():\n    CASES = [(\"preserve\", [1])]\n",
     "rebound"),
]


@pytest.mark.parametrize("label,src,needle", BAD_CASES)
def test_admissible_rejects(label, src, needle):
    ok, why = differential.admissible(src)
    assert not ok, f"{label}: should be rejected"
    assert needle in why, f"{label}: why={why!r}"


def test_admissible_requires_import_under_context():
    ctx = CodeContext("", ("stats",), ())
    src = (
        "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [3])]\n"
        "def observe(x):\n    return x\n"
    )
    ok, why = differential.admissible(src, ctx)
    assert not ok
    assert "import" in why


def test_admissible_accepts_import_under_context():
    ctx = CodeContext("", ("stats",), ())
    src = (
        "from stats import median\n"
        "CASES = [(\"bug_domain\", [1, 2]), (\"preserve\", [3])]\n"
        "def observe(x):\n    return median(x)\n"
    )
    ok, why = differential.admissible(src, ctx)
    assert ok, why


def test_built_prompt_markers():
    seen: list[str] = []

    def disp(prompt: str) -> str:
        seen.append(prompt)
        return ""

    differential.build("poc", "issue", "patch", n=4, dispatch=disp)
    assert seen, "build should dispatch"
    p = seen[0]
    assert "DIFFERENTIAL INPUT SUITE" in p
    for bad in ("METAMORPHIC RELATIONS", "states_expected_behaviour", "Write ONE pytest test", "MO_DIFF"):
        assert bad not in p
    assert not re.search(r"Write\s+\d+\s+pytest tests", p)


# ── 2. transport: real pytest subprocess emits the observation line ───────────
def test_transport_subprocess_emits_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    h = differential.harness(PURE_SUITE, 1)
    probe = tmp_path / "crucible_probe.py"
    probe.write_text(h)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    output = (r.stdout + r.stderr)[-800:]
    obs, why = differential.parse_obs(ExecOutcome(status="passed", output=output), 1)
    assert obs is not None, f"why={why!r}\noutput={output!r}"
    assert obs["obs"] == "2"  # median([1, 2, 3]) == 2
    assert len(obs["sha256"]) == 64
    assert obs["sha256"] == hashlib.sha256(b"2").hexdigest()


# ── 3. collateral: preserve diverges confirmed on re-run -> REFUTED ───────────
def test_collateral_divergence_refutes(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    n_invs = 3
    off_ordered = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    v_off = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(off_ordered), {}), mr_n=n_invs,
                  dispatch=make_dispatch(mr_n=n_invs, diff_suite=SUITE_SRC))
    assert v_off.verdict == PROVEN, v_off

    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "4")
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): [obs_outcome(1, 2)], (1, "head"): [obs_outcome(1, 1.5)],
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): [obs_outcome(3, 2)], (3, "head"): [obs_outcome(3, 1.5)],
    }
    runner = DiffRunner(list(off_ordered), cases)
    v_on = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=n_invs,
                 dispatch=make_dispatch(mr_n=n_invs, diff_suite=SUITE_SRC))

    assert v_on.verdict == REFUTED, v_on
    assert v_on.reason.startswith("differential:")
    assert "case_1" in v_on.reason
    assert "input=[1, 2, 3]" in v_on.reason
    assert "base=2" in v_on.reason
    assert "head=1.5" in v_on.reason
    # the invariants trail is untouched by the differential hook
    assert v_on.mr_pass_rate == v_off.mr_pass_rate
    assert v_on.mr_n == v_off.mr_n
    assert v_on.detail["invariants"] == v_off.detail["invariants"]
    rec = v_on.detail["differential"]
    assert rec["state"] == "refuted"
    assert rec["anchored"] is True
    assert rec["counts"]["preserve_diverged"] == 2
    assert rec["counts"]["bug_diverged"] == 1


# ── 4. correct fix: preserve agree -> verdict/reason unchanged ────────────────
def test_correct_fix_preserves_agree(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    n_invs = 3
    off_ordered = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    v_off = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(off_ordered), {}), mr_n=n_invs,
                  dispatch=make_dispatch(mr_n=n_invs, diff_suite=SUITE_SRC))

    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "4")
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): obs_outcome(1, 2), (1, "head"): obs_outcome(1, 2),
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): obs_outcome(3, 2), (3, "head"): obs_outcome(3, 2),
    }
    v_on = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(off_ordered), cases), mr_n=n_invs,
                 dispatch=make_dispatch(mr_n=n_invs, diff_suite=SUITE_SRC))

    assert v_on.verdict == v_off.verdict == PROVEN
    assert v_on.reason == v_off.reason
    rec = v_on.detail["differential"]
    assert rec["state"] == "equivalent"
    assert rec["verdict"] is None
    assert rec["counts"]["preserve_diverged"] == 0
    assert rec["counts"]["preserve_agree"] == 3


# ── 5. excluded inputs never agree ────────────────────────────────────────────
@pytest.mark.parametrize("base_outcome,needle", [
    (ExecOutcome(status="test_defect", exc="NameError"), "test_defect"),
    (ExecOutcome(status="failed", exc="AssertionError"), "failed"),
    (ExecOutcome(status="passed", output="1 passed\nCRUCIBLE_RC=0\n"), "no observation line"),
])
def test_base_unparseable_excluded_head_not_run(monkeypatch, base_outcome, needle):
    cases = {
        (0, "base"): base_outcome,
        (1, "base"): obs_outcome(1, 2),
        (1, "head"): obs_outcome(1, 2),
    }
    rec, runner = _run_check(monkeypatch, cases, n=2, diff_suite=SUITE_2)
    r0 = [r for r in rec["records"] if r["name"] == "case_0"][0]
    assert r0["status"] == "excluded"
    assert needle in r0["why"]
    assert r0["on_head"] is None
    assert not any(p for s, p in runner.calls if "MO_DIFF_CASE = 0" in s and p)
    assert r0["status"] != "agree"


def test_raising_base_run_test_excluded():
    class RaisingBaseRunner(DiffRunner):
        def run_test(self, src, patch=""):
            if "MO_DIFF_CASE = 0" in src and not patch:
                self.calls.append((src, patch))
                raise RuntimeError("sandbox died")
            return super().run_test(src, patch)

    runner = RaisingBaseRunner([], {(1, "base"): obs_outcome(1, 2), (1, "head"): obs_outcome(1, 2)})
    rec = differential.check("poc", ISSUE_TEXT, "patch text", runner=runner,
                             dispatch=make_dispatch(mr_n=3, diff_suite=SUITE_2), n=2)
    r0 = [r for r in rec["records"] if r["name"] == "case_0"][0]
    assert r0["status"] == "excluded"
    assert "base: run_test raised" in r0["why"]
    assert r0["on_head"] is None


@pytest.mark.parametrize("head_outcome,needle", [
    (ExecOutcome(status="error", exc="ImportError"), "error"),
    (ExecOutcome(status="passed", output="1 passed\nMO_DIFF_OBS {not valid json}\nCRUCIBLE_RC=0\n"), "malformed observation"),
    (ExecOutcome(status="passed", output=_raw_marker(99, "x", "a" * 64)), "malformed observation"),
    (ExecOutcome(status="passed", output=_raw_marker(0, "x", "xyz")), "malformed observation"),
    (ExecOutcome(status="passed", output=_raw_marker(0, 7, "a" * 64)), "malformed observation"),
    (ExecOutcome(status="passed", output="1 passed\n" + mo_line(0, "x") + "\n" + mo_line(0, "y") + "\nCRUCIBLE_RC=0\n"), "ambiguous observation"),
])
def test_head_unparseable_excluded(monkeypatch, head_outcome, needle):
    cases = {
        (0, "base"): obs_outcome(0, 3),
        (0, "head"): head_outcome,
        (1, "base"): obs_outcome(1, 2),
        (1, "head"): obs_outcome(1, 2),
    }
    rec, _ = _run_check(monkeypatch, cases, n=2, diff_suite=SUITE_2)
    r0 = [r for r in rec["records"] if r["name"] == "case_0"][0]
    assert r0["status"] == "excluded"
    assert needle in r0["why"]
    assert r0["on_base"] == "passed"
    assert r0["status"] != "agree"


def test_all_preserve_excluded_unverified(monkeypatch):
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): ExecOutcome(status="test_defect", exc="NameError"),
        (2, "base"): ExecOutcome(status="test_defect", exc="NameError"),
        (3, "base"): ExecOutcome(status="test_defect", exc="NameError"),
    }
    rec, _ = _run_check(monkeypatch, cases)
    assert rec["state"] == "unverified"
    assert rec["verdict"] is None
    assert rec["anchored"] is True
    assert rec["counts"]["preserve_agree"] == 0
    assert rec["counts"]["preserve_diverged"] == 0
    assert rec["counts"]["excluded"] == 3


def test_all_preserve_excluded_judge_proven_stays(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    n_invs = 3
    off_ordered = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): ExecOutcome(status="test_defect", exc="NameError"),
        (2, "base"): ExecOutcome(status="test_defect", exc="NameError"),
        (3, "base"): ExecOutcome(status="test_defect", exc="NameError"),
    }
    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(off_ordered), cases), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, diff_suite=SUITE_SRC))
    assert v.verdict == PROVEN, v
    assert v.detail["differential"]["state"] == "unverified"


# ── 6. unanchored: no bug_domain diverges -> no veto ──────────────────────────
def test_unanchored_no_bug_diverged_no_veto(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    n_invs = 3
    off_ordered = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 3),
        (1, "base"): [obs_outcome(1, 2)], (1, "head"): [obs_outcome(1, 1.5)],
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): obs_outcome(3, 2), (3, "head"): obs_outcome(3, 2),
    }
    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(off_ordered), cases), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, diff_suite=SUITE_SRC))
    assert v.verdict == PROVEN, v
    rec = v.detail["differential"]
    assert rec["state"] == "unverified"
    assert rec["anchored"] is False


def test_base_no_run_all_excluded(monkeypatch):
    cases = {
        (0, "base"): ExecOutcome(status="no_run"),
        (1, "base"): ExecOutcome(status="no_run"),
        (2, "base"): ExecOutcome(status="no_run"),
        (3, "base"): ExecOutcome(status="no_run"),
    }
    rec, _ = _run_check(monkeypatch, cases)
    assert rec["state"] == "unverified"
    assert rec["verdict"] is None
    assert rec["anchored"] is False
    assert rec["counts"]["excluded"] == 4


# ── 7. nondeterministic confirm re-run -> excluded ────────────────────────────
def test_nondeterministic_confirm_excluded(monkeypatch):
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): [obs_outcome(1, 2), obs_outcome(1, 999)],
        (1, "head"): [obs_outcome(1, 1.5)],
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): obs_outcome(3, 2), (3, "head"): obs_outcome(3, 2),
    }
    rec, runner = _run_check(monkeypatch, cases)
    r1 = [r for r in rec["records"] if r["name"] == "case_1"][0]
    assert r1["status"] == "excluded"
    assert "nondeterministic observation (base)" in r1["why"]
    assert rec["verdict"] is None  # no veto
    case1_calls = [1 for s, _ in runner.calls if "MO_DIFF_CASE = 1" in s]
    assert len(case1_calls) == 4  # base, head, base re-run, head re-run


# ── 8. knobs off => byte-identical Verdicts ───────────────────────────────────
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
    modes = ("unset", "diff0", "n6")
    for name, outcomes, mr_n, mr_block_n in scenarios:
        snapshots = {}
        for mode in modes:
            _clear_diff_env(monkeypatch)
            _clear_rel_env(monkeypatch)
            monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "0")  # DEFAULT ON now; pin OFF
            if mode == "diff0":
                monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "0")
            elif mode == "n6":
                monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "6")
            seen: list[str] = []
            base = make_dispatch(mr_n=mr_n, mr_block_n=mr_block_n)

            def disp(prompt: str) -> str:
                seen.append(prompt)
                return base(prompt)

            runner = DiffRunner(list(outcomes), {})
            v = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=mr_n, dispatch=disp)
            snapshots[mode] = (json.dumps(asdict(v), sort_keys=True), tuple(runner.calls), v, seen)

        s0 = snapshots["unset"]
        for mode in ("diff0", "n6"):
            assert snapshots[mode][0] == s0[0], f"{name}/{mode}: verdict bytes differ"
            assert snapshots[mode][1] == s0[1], f"{name}/{mode}: runner.calls differ"
        for mode in modes:
            assert "differential" not in snapshots[mode][2].detail, f"{name}/{mode}: differential key leaked"
            for p in snapshots[mode][3]:
                assert "DIFFERENTIAL INPUT SUITE" not in p, f"{name}/{mode}: differential prompt dispatched"


# ── 9. knobs on but the hook cannot fire (non-PROVEN paths) ───────────────────
def test_knobs_on_invariants_refute_skips_differential(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    n_invs = 4
    outcomes = _probe_green() + [ExecOutcome(status="failed")] * 4 \
        + [ExecOutcome(status="passed")] + [ExecOutcome(status="failed")] * 3 \
        + [ExecOutcome(status="failed")] * 3
    seen: list[str] = []
    base = make_dispatch(mr_n=n_invs)

    def disp(prompt: str) -> str:
        seen.append(prompt)
        return base(prompt)

    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(outcomes), {}), mr_n=n_invs, dispatch=disp)
    assert v.verdict == REFUTED, v
    assert "differential" not in v.detail
    assert not any("DIFFERENTIAL INPUT SUITE" in p for p in seen)


def test_knobs_on_unverified_skips_differential(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    outcomes = _probe_green() + [ExecOutcome(status="failed")] * 4 \
        + [ExecOutcome(status="passed"), ExecOutcome(status="passed"),
           ExecOutcome(status="failed"), ExecOutcome(status="failed")] \
        + [ExecOutcome(status="failed"), ExecOutcome(status="failed")]
    seen: list[str] = []
    base = make_dispatch(mr_n=4)

    def disp(prompt: str) -> str:
        seen.append(prompt)
        return base(prompt)

    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(outcomes), {}), mr_n=4, dispatch=disp)
    assert v.verdict == UNVERIFIED, v
    assert "differential" not in v.detail
    assert not any("DIFFERENTIAL INPUT SUITE" in p for p in seen)


def test_knobs_on_no_invariant_rescue_skips_differential(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_RESCUE", "1")
    # 3 relations each pass on patch and on base -> 3 held, 0 repaired -> UNVERIFIED
    outcomes = list(_probe_green()) + [ExecOutcome(status="passed")] * 6
    seen: list[str] = []
    base = make_dispatch(mr_n=3, mr_block_n=0, relations_srcs=[REL_SRC] * 3)

    def disp(prompt: str) -> str:
        seen.append(prompt)
        return base(prompt)

    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(outcomes, {}), mr_n=3, dispatch=disp)
    assert v.verdict == UNVERIFIED, v
    assert "differential" not in v.detail
    assert not any("DIFFERENTIAL INPUT SUITE" in p for p in seen)


# ── 10. relations + differential interplay ────────────────────────────────────
def test_relations_and_differential_both_veto(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    n_invs = 3
    ordered = _probe_green() + [ExecOutcome(status="failed")] * n_invs \
        + [ExecOutcome(status="passed")] * n_invs \
        + [ExecOutcome(status="failed", exc="AssertionError", output="E   assert 2 == 1"),
           ExecOutcome(status="passed")]
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): [obs_outcome(1, 2)], (1, "head"): [obs_outcome(1, 1.5)],
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): [obs_outcome(3, 2)], (3, "head"): [obs_outcome(3, 1.5)],
    }
    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(ordered), cases), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC], diff_suite=SUITE_SRC))
    assert v.verdict == REFUTED, v
    assert v.reason.startswith("metamorphic relation"), v.reason  # relations reason wins
    assert "relations" in v.detail
    assert "differential" in v.detail
    assert v.detail["differential"]["verdict"] == REFUTED


def test_relations_hold_collateral_differential_reason(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    n_invs = 3
    ordered = _probe_green() + [ExecOutcome(status="failed")] * n_invs \
        + [ExecOutcome(status="passed")] * n_invs \
        + [ExecOutcome(status="passed"), ExecOutcome(status="failed", exc="AssertionError")]
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): [obs_outcome(1, 2)], (1, "head"): [obs_outcome(1, 1.5)],
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): [obs_outcome(3, 2)], (3, "head"): [obs_outcome(3, 1.5)],
    }
    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(ordered), cases), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC], diff_suite=SUITE_SRC))
    assert v.verdict == REFUTED, v
    assert v.reason.startswith("differential:"), v.reason
    assert "relations" in v.detail
    assert "differential" in v.detail
    assert v.detail["differential"]["verdict"] == REFUTED


def test_relations_violated_differential_equivalent(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "1")
    monkeypatch.setenv("MO_ASSAY_RELATIONS_K", "1")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")
    n_invs = 3
    ordered = _probe_green() + [ExecOutcome(status="failed")] * n_invs \
        + [ExecOutcome(status="passed")] * n_invs \
        + [ExecOutcome(status="failed", exc="AssertionError", output="E   assert 2 == 1"),
           ExecOutcome(status="passed")]
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): obs_outcome(1, 2), (1, "head"): obs_outcome(1, 2),
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): obs_outcome(3, 2), (3, "head"): obs_outcome(3, 2),
    }
    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(ordered), cases), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, relations_srcs=[REL_SRC], diff_suite=SUITE_SRC))
    assert v.verdict == REFUTED, v
    assert v.reason.startswith("metamorphic relation"), v.reason  # relations reason wins
    assert "relations" in v.detail
    assert "differential" in v.detail
    assert v.detail["differential"]["verdict"] is None
    assert v.detail["differential"]["state"] == "equivalent"


# ── 11. knob parsing + veto_min supermajority ─────────────────────────────────
def test_enabled_values(monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", v)
        assert differential.enabled(), v
    for v in ("0", "false", "no", "off", "", "maybe"):
        monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", v)
        assert not differential.enabled(), v


def test_enabled_default_on(monkeypatch):
    monkeypatch.delenv("MO_ASSAY_DIFFERENTIAL", raising=False)
    assert differential.enabled() is True
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "0")
    assert differential.enabled() is False


def test_split_values():
    assert differential.split(6) == (2, 4)
    assert differential.split(4) == (1, 3)
    assert differential.split(2) == (1, 1)
    assert differential.split(8) == (2, 6)
    assert differential.split(3) == (1, 2)


def test_n_from_env_values(monkeypatch):
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "2")
    assert differential.n_from_env() == 2
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "99")
    assert differential.n_from_env() == 8
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "abc")
    assert differential.n_from_env() == 6
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_N", "1")
    assert differential.n_from_env() == 2


def test_veto_min_from_env_values(monkeypatch):
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_VETO_MIN", "1")
    assert differential.veto_min_from_env() == 1
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_VETO_MIN", "0")
    assert differential.veto_min_from_env() == 1
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL_VETO_MIN", "abc")
    assert differential.veto_min_from_env() == 2


def test_n_two_selects_one_bug_one_preserve(monkeypatch):
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): obs_outcome(1, 2), (1, "head"): obs_outcome(1, 2),
    }
    rec, _ = _run_check(monkeypatch, cases, n=2)
    assert rec["n"] == 2
    assert [r["name"] for r in rec["records"]] == ["case_0", "case_1"]


def test_single_divergence_no_veto_by_default(monkeypatch):
    _clear_diff_env(monkeypatch)
    _clear_rel_env(monkeypatch)
    n_invs = 3
    off_ordered = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "1")  # default veto_min = 2
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): [obs_outcome(1, 2)], (1, "head"): [obs_outcome(1, 1.5)],
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): obs_outcome(3, 2), (3, "head"): obs_outcome(3, 2),
    }
    v = judge(ISSUE_TEXT, "patch text", runner=DiffRunner(list(off_ordered), cases), mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs, diff_suite=SUITE_SRC))
    assert v.verdict == PROVEN, v
    rec = v.detail["differential"]
    assert rec["state"] == "unverified"
    assert rec["counts"]["preserve_diverged"] == 1


def test_single_divergence_veto_min_one_refutes(monkeypatch):
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): [obs_outcome(1, 2)], (1, "head"): [obs_outcome(1, 1.5)],
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): obs_outcome(3, 2), (3, "head"): obs_outcome(3, 2),
    }
    rec, _ = _run_check(monkeypatch, cases, extra_env={"MO_ASSAY_DIFFERENTIAL_VETO_MIN": "1"})
    assert rec["verdict"] == REFUTED
    assert rec["state"] == "refuted"
    assert rec["counts"]["preserve_diverged"] == 1


# ── 12. stderr observability line ─────────────────────────────────────────────
def test_stderr_observability_line(capsys, monkeypatch):
    cases = {
        (0, "base"): obs_outcome(0, 3), (0, "head"): obs_outcome(0, 2.5),
        (1, "base"): obs_outcome(1, 2), (1, "head"): obs_outcome(1, 2),
        (2, "base"): obs_outcome(2, 5), (2, "head"): obs_outcome(2, 5),
        (3, "base"): obs_outcome(3, 2), (3, "head"): obs_outcome(3, 2),
    }
    _run_check(monkeypatch, cases)
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.startswith("[assay-differential] ")]
    assert len(lines) == 1
    payload = json.loads(lines[0].replace("[assay-differential] ", "", 1))
    assert "counts" in payload and "records" in payload
    assert "state" in payload and "anchored" in payload
    assert "src" not in payload
    for r in payload["records"]:
        assert "src" not in r
