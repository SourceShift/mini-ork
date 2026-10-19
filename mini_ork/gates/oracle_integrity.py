"""Oracle-integrity audit — feed a verdict judge deliberately defective evidence.

``mini_ork.gates.hackability`` audits a *gate* for hollowness by attacking it with
known-bad documents. Nothing audits the oracle itself: any patch is judged by
``mini_ork.certify.oracle.judge``, and if that judging path can be fooled by
fabricated execution evidence then every ``PROVEN`` it emits is only as trustworthy
as the runner that fed it.

This module closes that loop. It presents a judge — the real one by default — with
seven classes of deliberately defective evidence and three known-answer controls:

* Each **injection** fabricates or withholds execution evidence at the runner seam
  (the same ``ExecOutcome`` contract the live oracle consumes). It is *detected*
  when the judge's decision is anything other than ``PROVEN``: an abstention is a
  correct refusal to certify, and a refusal by ``REFUTED`` is still a refusal to
  prove. Injections that come back ``PROVEN`` were *missed* — evidence the judge
  could not tell apart from a real measurement.
* Each **control** has a fixed expected decision. Controls prove the audit is not
  passing merely because the judge abstains on everything; a judge that always says
  ``UNVERIFIED`` trivially "detects" every injection but fails ``known_good_proven``.

The decision always comes from the judge under test and is compared to a fixed
expectation. No model decides the audit.

The default path (``run_default_audit``) calls the real
``mini_ork.certify.oracle.judge`` with a scripted runner and a scripted dispatch, so
it is hermetic: no network, no model, no sandbox. It never substitutes a stub judge.
If the real judge cannot be imported, it raises ``RuntimeError`` naming what is
missing rather than returning a passing result from a stub.

One honest caveat about the predicate: an injection is "detected" iff the decision
is not ``PROVEN``. That means ``test_defect_as_failure`` counts as detected when the
judge returns ``REFUTED`` (blaming the patch for a broken test) even though the
right answer is ``UNVERIFIED``; the literal contract is what this module scores.

Import discipline: stdlib only at module import time (``mini_ork.*`` imports are
function-local) so the audit stays importable in a stripped runtime.

Public API::

    from mini_ork.gates.oracle_integrity import (
        Case, INJECTIONS, CONTROLS,
        audit, run_default_audit,
        write_latest, latest,
    )

Env knobs (read by consumers, never cached here):

    MO_ORACLE_INTEGRITY  → promotion-gate consumer switch (default "1"; "0" disables)
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from typing import Callable

__all__ = [
    "Case",
    "INJECTIONS",
    "CONTROLS",
    "audit",
    "run_default_audit",
    "write_latest",
    "latest",
]

#: The judge vocabulary. Duplicated here (rather than imported at module scope) to
#: keep the audit importable on a stripped stdlib; these are load-bearing literals
#: shared with ``mini_ork.certify.verdict``.
PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNVERIFIED = "UNVERIFIED"

#: The 3 known-answer controls, in public order.
CONTROLS: tuple[str, ...] = (
    "known_good_proven",
    "known_bad_refuted",
    "known_empty_abstains",
)

#: What each control must reproduce. A judge that cannot reproduce all three is not
#: judged as passing merely because it refused to prove the injections.
_CONTROL_EXPECTATIONS = {
    "known_good_proven": PROVEN,
    "known_bad_refuted": REFUTED,
    "known_empty_abstains": UNVERIFIED,
}

#: The synthetic bug the default cases judge. Small, self-contained, and never
#: executed -- the runner is scripted; only the oracle's decision logic runs.
_ISSUE = (
    "Bug: add(1, 2) returns 4 instead of the expected 3. "
    "The documented behaviour is that add(a, b) returns a + b."
)
_PATCH = (
    "diff --git a/calc.py b/calc.py\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)
_POC = "def test_add():\n    assert add(1, 2) == 3\n"
_INVARIANT_TEXT = (
    "```python\n"
    "def test_mr_1():\n    assert add(2, 3) == 5\n\n"
    "def test_mr_2():\n    assert add(0, 0) == 0\n\n"
    "def test_mr_3():\n    assert add(-1, 1) == 0\n\n"
    "def test_mr_4():\n    assert add(10, 5) == 15\n"
    "```"
)

#: Matches ``invariants.MR_PROMPT`` ("Write {n} pytest tests ..."). Every other
#: prompt stage (relations, differential) gets "" and therefore no candidate, so
#: the audit makes no extra runner calls for the veto terms.
_MR_PROMPT_RE = re.compile(r"Write\s+\d+\s+pytest tests")


@dataclasses.dataclass(frozen=True)
class Case:
    """One audit case.

    ``run`` binds everything the judge needs (the judge itself, the issue/patch,
    the scripted runner and dispatch) and returns the judge's decision string. The
    audit never calls the judge directly, so a caller can supply cases built any
    way it likes.
    """

    name: str
    injected: bool
    run: Callable[[], str]


# ── evidence boundary ────────────────────────────────────────────────────────


def _outcome(**fields):
    """Build a real ``ExecOutcome`` (function-local import: stdlib-only at import)."""
    from mini_ork.runtime import ExecOutcome

    return ExecOutcome(**fields)


def _scripted_dispatch(prompt: str) -> str:
    """Canned model text: invariant source for the MR stage, nothing otherwise."""
    if _MR_PROMPT_RE.search(prompt or ""):
        return _INVARIANT_TEXT
    return ""


def _decision_of(verdict) -> str:
    """The decision string of a judge return value (``Verdict`` or a bare string)."""
    if isinstance(verdict, str):
        return verdict
    decision = getattr(verdict, "verdict", None)
    if isinstance(decision, str):
        return decision
    return UNVERIFIED


class _RunnerBase:
    """Scripted oracle runner: the same duck-typed seam the live oracle consumes."""

    up = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def run_test(self, src: str, patch: str = ""):  # pragma: no cover - overridden
        raise NotImplementedError

    def _failed(self):
        return _outcome(
            status="failed",
            ran=True,
            returncode=1,
            output="E   AssertionError: assert 4 == 3",
            backend="scripted",
            exc="AssertionError",
        )

    def _passed(self, **extra):
        fields = {
            "status": "passed",
            "ran": True,
            "returncode": 0,
            "output": "1 passed",
            "backend": "scripted",
        }
        fields.update(extra)
        return _outcome(**fields)


class _HonestRunner(_RunnerBase):
    """Honest evidence: red on base, green on the patched candidate."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        return self._passed() if patch else self._failed()


class _RedRunner(_RunnerBase):
    """Honest red evidence on the candidate: the bug still reproduces after the patch."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        return self._failed()


class _SilentRunner(_RunnerBase):
    """Injection 1: reports ``passed`` with an empty log -- zero tests executed."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        return self._passed(ran=False, returncode=0, output="", meta={"executed": 0})


class _FrozenRunner(_RunnerBase):
    """Injection 2: the same outcome for base and candidate, regardless of input."""

    def __init__(self) -> None:
        super().__init__()
        self._frozen = _outcome(
            status="passed", ran=True, returncode=0, output="1 passed", backend="scripted"
        )

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        return self._frozen


class _ApplyFailRunner(_RunnerBase):
    """Injection 3: the patch failed to apply, but the outcome says ``passed``."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        if not patch:
            return self._failed()
        return self._passed(
            returncode=1,
            output="error: patch does not apply",
            meta={"apply_status": "apply_fail"},
        )


class _StaleRunner(_RunnerBase):
    """Injection 4: green evidence from a different input digest is replayed."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        if not patch:
            return self._failed()
        return self._passed(
            output="1 passed (replayed)",
            meta={
                "input_digest": "sha256:" + "0" * 64,
                "candidate_input_digest": "sha256:" + "f" * 64,
            },
        )


class _ErrorRunner(_RunnerBase):
    """Injection 5: every execution attempt ends ``error`` yet status is ``passed``."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        if not patch:
            return self._failed()
        return self._passed(
            returncode=1,
            output="error: collection failed on every attempt",
            exc="error",
            meta={"attempts": ["error", "error"], "final_attempt_status": "error"},
        )


class _TestDefectRunner(_RunnerBase):
    """Injection 6: a harness/setup crash is reported as a genuine failed assertion."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        if not patch:
            return self._failed()
        return _outcome(
            status="failed",
            ran=True,
            returncode=1,
            output="E   NameError: name 'add' is not defined",
            backend="scripted",
            exc="NameError",
            meta={"test_defect": True},
        )


class _ContractViolatingOutcome:
    """A non-``ExecOutcome``: it has neither ``status`` nor ``informative``."""


class _ContractRunner(_RunnerBase):
    """Injection 7: a non-``ExecOutcome`` object is handed over as execution evidence."""

    def run_test(self, src: str, patch: str = ""):
        self.calls.append((src, bool(patch)))
        return _ContractViolatingOutcome()


# ── case builders ────────────────────────────────────────────────────────────


def _default_judge():
    """The real oracle judge (function-local import; never a stub)."""
    try:
        from mini_ork.certify.oracle import judge as oracle_judge
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear RuntimeError
        raise RuntimeError(
            "mini_ork.certify.oracle.judge is unavailable, so the default "
            f"oracle-integrity audit cannot run: {exc}"
        ) from exc
    return oracle_judge


def _judge_bound(base: Case):
    """The judge a base case was built against (stored on its run closure)."""
    judge = getattr(base.run, "judge", None)
    return judge if judge is not None else _default_judge()


def _run_with_runner(judge, runner) -> Callable[[], str]:
    """Bind one judge call to a scripted runner; return the decision."""

    def run() -> str:
        verdict = judge(
            _ISSUE,
            _PATCH,
            runner=runner,
            dispatch=_scripted_dispatch,
            poc=_POC,
        )
        return _decision_of(verdict)

    return run


def _injected_case(base: Case, name: str, runner_factory) -> Case:
    """Wrap ``base``'s judge with defective evidence and return a new Case."""
    return Case(name=name, injected=True, run=_run_with_runner(_judge_bound(base), runner_factory()))


def _silent_payload_non_delivery(base: Case) -> Case:
    return _injected_case(base, "silent_payload_non_delivery", _SilentRunner)


def _frozen_result_swap(base: Case) -> Case:
    return _injected_case(base, "frozen_result_swap", _FrozenRunner)


def _apply_fail_masked_as_pass(base: Case) -> Case:
    return _injected_case(base, "apply_fail_masked_as_pass", _ApplyFailRunner)


def _stale_evidence_replay(base: Case) -> Case:
    return _injected_case(base, "stale_evidence_replay", _StaleRunner)


def _runner_error_as_pass(base: Case) -> Case:
    return _injected_case(base, "runner_error_as_pass", _ErrorRunner)


def _test_defect_as_failure(base: Case) -> Case:
    return _injected_case(base, "test_defect_as_failure", _TestDefectRunner)


def _contract_violating_outcome(base: Case) -> Case:
    return _injected_case(base, "contract_violating_outcome", _ContractRunner)


#: Exactly the 7 injection classes. Each value takes the honest base case and
#: returns a Case whose evidence boundary is defective.
INJECTIONS: dict[str, Callable[[Case], Case]] = {
    "silent_payload_non_delivery": _silent_payload_non_delivery,
    "frozen_result_swap": _frozen_result_swap,
    "apply_fail_masked_as_pass": _apply_fail_masked_as_pass,
    "stale_evidence_replay": _stale_evidence_replay,
    "runner_error_as_pass": _runner_error_as_pass,
    "test_defect_as_failure": _test_defect_as_failure,
    "contract_violating_outcome": _contract_violating_outcome,
}


def _base_case(judge) -> Case:
    """The honest green case the injections wrap; carries the judge for builders."""

    def run() -> str:
        verdict = judge(
            _ISSUE,
            _PATCH,
            runner=_HonestRunner(),
            dispatch=_scripted_dispatch,
            poc=_POC,
        )
        return _decision_of(verdict)

    run.judge = judge  # type: ignore[attr-defined] - how injections find the judge
    return Case(name="honest_base", injected=False, run=run)


def _control_cases(judge) -> list[Case]:
    return [
        Case(
            name="known_good_proven",
            injected=False,
            run=_run_with_runner(judge, _HonestRunner()),
        ),
        Case(
            name="known_bad_refuted",
            injected=False,
            run=_run_with_runner(judge, _RedRunner()),
        ),
        Case(
            name="known_empty_abstains",
            injected=False,
            run=lambda: _decision_of(
                judge(
                    _ISSUE,
                    "",
                    runner=_HonestRunner(),
                    dispatch=_scripted_dispatch,
                    poc=_POC,
                )
            ),
        ),
    ]


def default_cases(judge) -> list[Case]:
    """The 7 injections + 3 controls, all bound to ``judge``."""
    base = _base_case(judge)
    cases = [builder(base) for builder in INJECTIONS.values()]
    cases.extend(_control_cases(judge))
    return cases


def _judge_label(judge) -> str:
    module = getattr(judge, "__module__", "") or ""
    qualname = getattr(judge, "__qualname__", "") or getattr(judge, "__name__", "") or ""
    if module and qualname:
        return f"{module}.{qualname}"
    return qualname or module or ""


# ── audit ────────────────────────────────────────────────────────────────────


def audit(judge, cases: list[Case] | None = None) -> dict:
    """Run the injections and controls; return the measurement record.

    With ``cases=None`` the 10 default cases are bound to ``judge``. An injection
    is *detected* when its decision is not ``PROVEN``; a control is *ok* when its
    decision equals the fixed known answer. A case whose ``run`` raises is scored
    as ``UNVERIFIED`` -- a judge that crashes on evidence did not prove anything.
    """
    if cases is None:
        cases = default_cases(judge)

    detected: list[str] = []
    missed: list[str] = []
    controls_ok: list[str] = []
    controls_failed: list[str] = []

    for case in cases:
        try:
            decision = case.run()
        except Exception:  # noqa: BLE001 - a crash cannot certify a proof
            decision = UNVERIFIED
        if case.injected:
            if decision == PROVEN:
                missed.append(case.name)
            else:
                detected.append(case.name)
        else:
            expected = _CONTROL_EXPECTATIONS.get(case.name)
            if expected is not None and decision == expected:
                controls_ok.append(case.name)
            else:
                controls_failed.append(case.name)

    return {
        "pass": not missed and not controls_failed,
        "detected": detected,
        "missed": missed,
        "controls_ok": controls_ok,
        "controls_failed": controls_failed,
        "judge": _judge_label(judge),
        "audited_at": int(time.time()),
    }


def run_default_audit(*, judge=None) -> dict:
    """Audit the real ``oracle.judge`` with scripted, hermetic evidence.

    ``judge`` is injectable for tests; production passes nothing. The real judge
    is imported function-locally. If it (or the ``ExecOutcome`` contract needed to
    build evidence) cannot be imported, this raises ``RuntimeError`` naming the
    missing piece -- it never falls back to a stub that would pass itself.
    """
    if judge is None:
        judge = _default_judge()
    try:
        from mini_ork.runtime import ExecOutcome  # noqa: F401 - contract availability
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear RuntimeError
        raise RuntimeError(
            "run_default_audit requires mini_ork.runtime.ExecOutcome to build "
            f"execution evidence: {exc}"
        ) from exc
    return audit(judge, cases=default_cases(judge))


# ── persistence ──────────────────────────────────────────────────────────────


def records_dir(db_path: str) -> str:
    """The audit directory, next to the DB it describes."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "oracle-integrity")


def latest_path(db_path: str) -> str:
    """``<dirname(abspath(db_path))>/oracle-integrity/latest.json``."""
    return os.path.join(records_dir(db_path), "latest.json")


def write_latest(db_path: str, result: dict) -> str:
    """Persist ``result`` atomically (``.tmp`` + ``os.replace``); return the path."""
    os.makedirs(records_dir(db_path), exist_ok=True)
    path = latest_path(db_path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True, indent=2)
    os.replace(tmp, path)
    return path


def latest(db_path: str) -> dict | None:
    """The latest audit record, or ``None`` when absent or unparseable."""
    try:
        with open(latest_path(db_path), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
