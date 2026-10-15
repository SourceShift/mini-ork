"""Escalation policy — may the self-driving loop open a framework-edit fix epic?

Operator decision (2026-10-13): **aggressive**; gate order and bars chosen
2026-10-15 on the operator's delegation (plan: docs/plans/2026-10-13-self-driving-epic.md). A deterministic (rule) diagnosis
that blames the engine escalates; a model-made diagnosis escalates too, when its
confidence clears a calibrated bar — bounded by a per-day cap on fix epics.

This module is pure policy: no I/O, no DB, no env reads. The caller (the
self-driving loop, SDE-3) gathers ``EscalationInputs`` — the calibrated bar comes
from the Learn-Then-Test threshold in ``mini_ork.gates.abstain_gate`` computed
over labelled model diagnoses, the cap and counts from ``bug_reports`` /
``epics`` — and records the returned ``reason`` verbatim on the run.

The file is on the frozen-kernel list (``config/kernel-frozen.yaml``): an
auto-opened fix epic can never edit the policy that admits it.
"""
from __future__ import annotations

from dataclasses import dataclass

# Used for model diagnoses until enough labelled outcomes exist for the
# calibrated (LTT) bar. A label is "the diagnosis was right": the fix epic it
# opened merged AND the revived run got past the node that failed.
BOOTSTRAP_BAR = 0.85
MIN_LABELS = 30  # mirrors abstain_gate.DEFAULT_MIN_CALIB


@dataclass(frozen=True)
class EscalationInputs:
    layer: str                          # Diagnosis layer; only "engine" is escalatable
    source: str                         # "rule" | "model"
    confidence: float                   # 1.0 for rules; the diagnoser's stated value for models
    calibrated_bar: float | None        # LTT bar over labelled model diagnoses; None when uncertifiable
    labelled_count: int                 # labelled model diagnoses available for calibration
    fix_epics_last_24h: int             # auto-opened fix epics in the trailing 24 h
    daily_cap: int                      # MO_FIX_EPIC_DAILY_CAP
    signature_fix_failed_before: bool   # a fix epic for this failure signature already failed (GRAO)
    collapse_tripped: bool              # circuit breaker's collapse halt is open


@dataclass(frozen=True)
class EscalationDecision:
    escalate: bool
    reason: str


def should_escalate(x: EscalationInputs) -> EscalationDecision:
    """Decide whether this diagnosis may open a framework-edit fix epic.

    Every refusal must say why (the reason lands in repair.json and the
    oversight inbox). A refusal is not a dead end: the caller hands the run to
    the human with the diagnosis attached.
    """
    # Hard stops, cheapest-to-explain first: the first one that fires is the
    # reason the operator reads.
    if x.layer != "engine":
        return EscalationDecision(False, f"not an engine fault (layer={x.layer})")
    if x.collapse_tripped:
        return EscalationDecision(
            False, "fix-epic breaker open: the most recent auto fix epics all failed")
    if x.signature_fix_failed_before:
        return EscalationDecision(
            False, "a fix epic for this failure signature already failed; not re-proposed")
    if x.fix_epics_last_24h >= x.daily_cap:
        return EscalationDecision(
            False, f"daily fix-epic cap reached ({x.fix_epics_last_24h}/{x.daily_cap})")

    # A rule diagnosis cites deterministic evidence: escalate.
    if x.source == "rule":
        return EscalationDecision(True, "rule diagnosis of an engine fault")
    if x.source != "model":
        return EscalationDecision(False, f"unknown diagnosis source {x.source!r}")

    # A model diagnosis must clear a bar. Until enough outcomes are labelled the
    # bootstrap bar applies; after that, only a certifiable calibrated bar does —
    # an uncertifiable diagnoser is refused, never waved through on the bootstrap.
    if x.labelled_count < MIN_LABELS:
        bar, which = BOOTSTRAP_BAR, f"bootstrap bar ({x.labelled_count}/{MIN_LABELS} labels)"
    elif x.calibrated_bar is None:
        return EscalationDecision(
            False, "model diagnoser is uncertifiable at the target risk; not escalated")
    else:
        bar, which = x.calibrated_bar, "calibrated bar"
    if x.confidence >= bar:
        return EscalationDecision(
            True, f"model diagnosis confidence {x.confidence:.2f} >= {which} {bar:.2f}")
    return EscalationDecision(
        False, f"model diagnosis confidence {x.confidence:.2f} < {which} {bar:.2f}")
