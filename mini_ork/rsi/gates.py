"""The twelve merge gates — judgement over evidence, never execution.

A cycle may merge only when every gate below passes. That makes the gates the
security boundary of the whole loop, so three rules shape this module:

* **It runs nothing.** Every gate reads an evidence record a caller assembled
  (``cycle`` runs the commands; ``worktree`` owns git). A gate cannot be made to
  pass by the act of checking it, and the whole set is unit-testable against
  hand-built evidence with no subprocess, no DB and no network.
* **Missing evidence fails.** ``lint_ok``/``pytest_ok`` default to ``None`` and
  a ``None`` is a failure, not a pass. A gate that skipped itself because a
  command was never run would be a merge path with no check behind it.
* **The static and probe gates are differential** (``post ⊆ pre``). ``garden``
  and ``validate --strict`` are already red on a healthy checkout, so a strict
  rc-0 gate would make auto-merge permanently impossible — and a "fix garden"
  candidate would be indistinguishable from a commit that merely coexists with
  the red. Comparing stable finding signatures makes the gate mean *this change
  removed a finding and added none*.

G7/G9 compare signatures, not counts: a count can stay equal while one finding
is replaced by another.

Two gates carry their own vocabulary:

* **G8** reads gap *presence* rather than probe success. ``garden`` and
  ``validate`` are red on a healthy checkout, so a candidate they produced has
  no green state to reach; its success is its own finding signature disappearing
  between the two refs.
* **G4** accepts ``changed ⊆ kickoff files ∪ allow_prefixes`` and a file-count
  bound. The allowlist is deliberately the same one ``select`` used
  pre-dispatch, so the two checks cannot disagree about the fix surface.
* **G10** fails three ways — ``critic_rejected`` when every critic said no,
  ``critic_split`` when they could not agree, ``critic_not_exogenous`` when the
  critics share a family with each other or with the implementer. The first two
  are different signals and the ledger must not read them alike: a panel that
  unanimously rejects the *change* is evidence about the fix, while a panel that
  disagrees with itself is evidence about the question. A majority vote is merge
  authority under neither.
* **G12** measures a ``bench:<name>`` candidate on a sealed benchmark split:
  a dev split first (spending no error budget), then one spend-limited sealed
  look under SAGE with a no-regression budget and guard metrics. A non-bench
  candidate passes it untouched, and G8 defers to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from mini_ork.rsi.probes import Finding
from mini_ork.rsi.select import DEFAULT_ALLOW_PREFIXES
from mini_ork.verify.panel_heterogeneity import model_family

__all__ = [
    "BENCH_PREFIX",
    "FAIL_CODES",
    "GATE_ORDER",
    "CriticVerdict",
    "GateEvidence",
    "GateResult",
    "ValidationReport",
    "differential",
    "evaluate",
]

#: ``(gate id, name, fail code)``, in evaluation order. The order is the order a
#: report reads in, not a short-circuit order: every gate is evaluated so a
#: failing cycle names *all* of its broken gates, not just the first.
GATE_ORDER: tuple[tuple[str, str, str], ...] = (
    ("G1", "harvest_complete", "harvest_incomplete"),
    ("G2", "child_verdict", "child_verdict_failed"),
    ("G3", "frozen_paths", "frozen_path"),
    ("G4", "scope", "scope_violation"),
    ("G5", "lint", "lint_failed"),
    ("G6", "pytest_scoped", "tests_failed"),
    ("G7", "static_differential", "new_static_finding"),
    ("G8", "success_metric", "metric_unchanged"),
    ("G9", "no_new_probe_failures", "new_probe_failure"),
    ("G10", "critic_unanimous", "critic_split"),
    ("G11", "budget", "budget_exceeded"),
    ("G12", "impact", "no_measured_gain"),
)

#: The success-command scheme that routes a candidate to G12 instead of G8.
BENCH_PREFIX = "bench:"

#: Every fail code a gate can emit, including G10's two extra.
FAIL_CODES: tuple[str, ...] = tuple(code for _, _, code in GATE_ORDER) + (
    "critic_not_exogenous",
    "critic_rejected",
)


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    name: str
    ok: bool
    code: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.ok and self.code:
            raise ValueError(f"{self.gate}: a passing gate carries no fail code")
        if not self.ok and self.code not in FAIL_CODES:
            raise ValueError(f"{self.gate}: unknown fail code {self.code!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "name": self.name, "ok": self.ok,
                "code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    """One exogenous critic's answer. ``family`` is resolved, never trusted."""

    alias: str
    lane: str = ""
    approve: bool = False
    blocking: tuple[str, ...] = ()
    confidence: float = 0.0
    #: The critic's own account of *why* it answered this way. G10 does not read
    #: it — the verdict is ``approve`` + ``blocking`` — but the ledger keeps it,
    #: because ``"minimax_lens did not approve"`` and ``"minimax_lens did not
    #: approve because no producer emits the field the patch reads"`` send an
    #: operator to two different places. Carried here so a cycle that records
    #: its verdicts records the reasoning too, not just the vote.
    why: str = ""

    @property
    def family(self) -> str:
        return model_family(self.lane or self.alias)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CriticVerdict":
        blocking = raw.get("blocking_findings") or raw.get("blocking") or ()
        if isinstance(blocking, str):
            blocking = [blocking]
        return cls(
            alias=str(raw.get("alias") or raw.get("critic") or ""),
            lane=str(raw.get("lane") or raw.get("model") or ""),
            approve=raw.get("approve") is True,
            blocking=tuple(str(b) for b in blocking),
            confidence=float(raw.get("confidence") or 0.0),
            why=str(raw.get("why") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """The ledger/JSON shape: every field, with ``family`` resolved.

        ``family`` is included because that is what G10 actually grades — a
        reader must be able to see, from the row alone, whether two critics
        were two *families* or one model wearing two names.
        """
        return {"alias": self.alias, "lane": self.lane, "family": self.family,
                "approve": self.approve, "confidence": self.confidence,
                "blocking_findings": list(self.blocking), "why": self.why}


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """Everything the gates judge. Defaults fail closed, not open."""

    # G1
    harvest_ok: bool = False
    required_probe_failures: tuple[str, ...] = ()
    # G2
    child_verdict: Mapping[str, Any] | None = None
    # G3
    frozen_hits: tuple[str, ...] = ()
    # G4
    changed_files: tuple[str, ...] = ()
    scope_files: tuple[str, ...] = ()
    allow_prefixes: tuple[str, ...] = DEFAULT_ALLOW_PREFIXES
    max_files: int = 5
    # G5 / G6: None means "not run", which fails.
    lint_ok: bool | None = None
    lint_detail: str = ""
    pytest_ok: bool | None = None
    pytest_detail: str = ""
    # G7: findings at the base ref and at HEAD.
    static_before: tuple[Finding, ...] = ()
    static_after: tuple[Finding, ...] = ()
    static_ran: bool = False
    # G8: does the candidate's own evidence still reproduce, before and after?
    # True means "the gap is present". Presence, not probe rc: a candidate drawn
    # from a probe that is red on a healthy checkout (garden, validate) has no
    # green state to reach, so its success is its own signature disappearing.
    gap_before: bool | None = None
    gap_after: bool | None = None
    metric_command: str = ""
    # G9: probe ids that errored/timed out at each ref.
    probe_failures_before: tuple[str, ...] = ()
    probe_failures_after: tuple[str, ...] = ()
    probes_ran: bool = False
    # G10
    critics: tuple[CriticVerdict, ...] = ()
    implementer_family: str = ""
    critic_panel: Mapping[str, Any] | None = None
    # G11
    spend_24h: float = 0.0
    daily_cap: float = 0.0
    cycle_cost: float = 0.0
    per_cycle_cap: float = 0.0
    # G12
    impact: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    results: tuple[GateResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failing(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.failing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "gates": [r.to_dict() for r in self.results],
            "failed": list(self.codes),
        }


# --------------------------------------------------------------------------
# Differential helper — the one piece of set maths two gates share
# --------------------------------------------------------------------------


def differential(
    before: Iterable[Finding], after: Iterable[Finding]
) -> tuple[tuple[Finding, ...], tuple[Finding, ...], tuple[Finding, ...]]:
    """``(new, removed, unchanged)`` findings, compared by signature.

    ``new`` is what a merge must not introduce; ``removed`` is what a fix is
    expected to produce. Both are returned in first-seen order with duplicates
    collapsed, because one finding can be reported by two probe parsers.
    """
    def _indexed(findings: Iterable[Finding]) -> dict[str, Finding]:
        out: dict[str, Finding] = {}
        for finding in findings:
            out.setdefault(finding.signature(), finding)
        return out

    pre = _indexed(before)
    post = _indexed(after)
    new = tuple(post[sig] for sig in post if sig not in pre)
    removed = tuple(pre[sig] for sig in pre if sig not in post)
    unchanged = tuple(post[sig] for sig in post if sig in pre)
    return new, removed, unchanged


# --------------------------------------------------------------------------
# Individual gates
# --------------------------------------------------------------------------


def _g1(ev: GateEvidence) -> GateResult:
    if ev.harvest_ok and not ev.required_probe_failures:
        return GateResult("G1", "harvest_complete", True)
    detail = "harvest did not complete"
    if ev.required_probe_failures:
        detail += f"; required probe(s) failed: {', '.join(ev.required_probe_failures)}"
    return GateResult("G1", "harvest_complete", False, "harvest_incomplete", detail)


def _g2(ev: GateEvidence) -> GateResult:
    verdict = ev.child_verdict
    if not isinstance(verdict, Mapping):
        return GateResult("G2", "child_verdict", False, "child_verdict_failed",
                          "the child produced no verdict.json")
    if verdict.get("pass") is not True:
        return GateResult("G2", "child_verdict", False, "child_verdict_failed",
                          f"verdict.pass is {verdict.get('pass')!r}")
    changed = int(verdict.get("files_changed") or 0)
    if changed == 0:
        return GateResult("G2", "child_verdict", False, "child_verdict_failed",
                          "verdict declares files_changed == 0")
    return GateResult("G2", "child_verdict", True)


def _g3(ev: GateEvidence) -> GateResult:
    if ev.frozen_hits:
        return GateResult("G3", "frozen_paths", False, "frozen_path",
                          f"changed frozen path(s): {', '.join(ev.frozen_hits)}")
    return GateResult("G3", "frozen_paths", True)


def _inside(path: str, prefixes: Sequence[str]) -> bool:
    for prefix in prefixes:
        p = prefix.strip().rstrip("/")
        if path == p or path.startswith(p + "/"):
            return True
    return False


def _g4(ev: GateEvidence) -> GateResult:
    changed = tuple(dict.fromkeys(ev.changed_files))
    if not changed:
        return GateResult("G4", "scope", False, "scope_violation",
                          "the change is empty")
    outside = [
        p for p in changed
        if p not in ev.scope_files and not _inside(p, ev.allow_prefixes)
    ]
    if outside:
        return GateResult("G4", "scope", False, "scope_violation",
                          f"outside the kickoff scope: {', '.join(outside)}")
    if len(changed) > ev.max_files:
        return GateResult("G4", "scope", False, "scope_violation",
                          f"{len(changed)} files > max {ev.max_files}")
    return GateResult("G4", "scope", True)


def _g5(ev: GateEvidence) -> GateResult:
    if ev.lint_ok is True:
        return GateResult("G5", "lint", True)
    detail = ev.lint_detail or (
        "the lint gate did not run" if ev.lint_ok is None else "ruff reported errors"
    )
    return GateResult("G5", "lint", False, "lint_failed", detail)


def _g6(ev: GateEvidence) -> GateResult:
    if ev.pytest_ok is True:
        return GateResult("G6", "pytest_scoped", True)
    detail = ev.pytest_detail or (
        "the scoped test gate did not run" if ev.pytest_ok is None else "pytest failed"
    )
    return GateResult("G6", "pytest_scoped", False, "tests_failed", detail)


def _g7(ev: GateEvidence) -> GateResult:
    if not ev.static_ran:
        return GateResult("G7", "static_differential", False, "new_static_finding",
                          "the differential static check did not run")
    new, removed, _ = differential(ev.static_before, ev.static_after)
    if new:
        first = new[0]
        return GateResult(
            "G7", "static_differential", False, "new_static_finding",
            f"{len(new)} new finding(s), first: {first.level} {first.message[:90]}",
        )
    return GateResult("G7", "static_differential", True,
                      detail=f"{len(removed)} finding(s) removed")


def _g8(ev: GateEvidence) -> GateResult:
    if ev.metric_command.startswith(BENCH_PREFIX):
        return GateResult("G8", "success_metric", True, detail="measured by G12")
    command = ev.metric_command or "the success command"
    if ev.gap_before is True and ev.gap_after is False:
        return GateResult("G8", "success_metric", True)
    if ev.gap_after is True:
        return GateResult("G8", "success_metric", False, "metric_unchanged",
                          f"the gap {command} measures is still present after the change")
    if ev.gap_after is False and ev.gap_before is False:
        return GateResult("G8", "success_metric", False, "metric_unchanged",
                          f"the gap {command} measures was already absent before the change")
    return GateResult("G8", "success_metric", False, "metric_unchanged",
                      f"{command} was not measured on both trees")


def _g9(ev: GateEvidence) -> GateResult:
    if not ev.probes_ran:
        return GateResult("G9", "no_new_probe_failures", False, "new_probe_failure",
                          "the differential probe run did not happen")
    new = tuple(p for p in dict.fromkeys(ev.probe_failures_after)
                if p not in set(ev.probe_failures_before))
    if new:
        return GateResult("G9", "no_new_probe_failures", False, "new_probe_failure",
                          f"probe(s) newly failing: {', '.join(new)}")
    return GateResult("G9", "no_new_probe_failures", True)


def _g10(ev: GateEvidence) -> GateResult:
    critics = ev.critics
    if len(critics) < 2:
        return GateResult("G10", "critic_unanimous", False, "critic_not_exogenous",
                          f"{len(critics)} critic(s); at least 2 independent ones are required")
    families = [c.family for c in critics]
    if "" in families or len(set(families)) != len(families):
        return GateResult("G10", "critic_unanimous", False, "critic_not_exogenous",
                          f"critic families are not distinct: {families}")
    impl = model_family(ev.implementer_family)
    if impl and impl in families:
        return GateResult("G10", "critic_unanimous", False, "critic_not_exogenous",
                          f"a critic shares the implementer's family ({impl})")
    if ev.critic_panel is not None:
        problems = list(ev.critic_panel.get("problems") or ())
        if problems or ev.critic_panel.get("ok") is not True:
            return GateResult("G10", "critic_unanimous", False, "critic_not_exogenous",
                              "panel check: " + (", ".join(map(str, problems)) or "not ok"))
    dissent = [c.alias for c in critics if not c.approve or c.blocking]
    if dissent:
        # A panel that rejected in unison is not a split. Cycle 13 (2026-10-26)
        # merged the two under ``critic_split`` and a reader could not tell
        # "the fix is wrong" from "the panel could not agree" — which is the
        # difference between a lesson about the fix and a lesson about the
        # question.
        code = "critic_rejected" if len(dissent) == len(critics) else "critic_split"
        return GateResult("G10", "critic_unanimous", False, code,
                          f"critic(s) did not approve: {', '.join(dissent)}")
    return GateResult("G10", "critic_unanimous", True)


def _g11(ev: GateEvidence) -> GateResult:
    if ev.daily_cap <= 0 or ev.per_cycle_cap <= 0:
        return GateResult("G11", "budget", False, "budget_exceeded",
                          "a budget cap is unset or non-positive (fail closed)")
    if ev.spend_24h >= ev.daily_cap:
        return GateResult("G11", "budget", False, "budget_exceeded",
                          f"spent ${ev.spend_24h:.2f} of the ${ev.daily_cap:.2f} daily cap")
    if ev.cycle_cost > ev.per_cycle_cap:
        return GateResult("G11", "budget", False, "budget_exceeded",
                          f"cycle cost ${ev.cycle_cost:.2f} > ${ev.per_cycle_cap:.2f} cap")
    return GateResult("G11", "budget", True)


def _g12(ev: GateEvidence) -> GateResult:
    if not ev.metric_command.startswith(BENCH_PREFIX):
        return GateResult("G12", "impact", True)
    if ev.impact is None:
        return GateResult("G12", "impact", False, "no_measured_gain",
                          "impact was not measured")
    if ev.impact.get("status") == "accepted":
        return GateResult("G12", "impact", True)
    stage = ev.impact.get("stage") or "setup"
    reason = ev.impact.get("reason") or "unknown"
    return GateResult("G12", "impact", False, "no_measured_gain",
                      f"{stage}: {reason}")


_GATE_FNS = {
    "G1": _g1, "G2": _g2, "G3": _g3, "G4": _g4, "G5": _g5, "G6": _g6,
    "G7": _g7, "G8": _g8, "G9": _g9, "G10": _g10, "G11": _g11, "G12": _g12,
}


def evaluate(ev: GateEvidence) -> ValidationReport:
    """Run every gate, in order, with no short-circuit.

    All gates are evaluated because a cycle that quarantines with one reason
    sends the operator round the loop again for the next one. ``passed`` is the
    AND of every gate; there is no weighting and no majority.
    """
    return ValidationReport(tuple(_GATE_FNS[gate](ev) for gate, _, _ in GATE_ORDER))
