"""Typed harness-edit proposal + realized-outcome scoring (Harness-R1, arXiv 2608.02276).

mini-ork's self-improvement loop has exactly one operator: dispatch a
``code-fix`` child and observe what comes back. When the defect is not in a
file but in the *harness* — a stage that runs in the wrong order, a prompt that
invites a malformed answer, a retry policy that cannot survive a turn cap — the
loop has no way to say so. It can only re-dispatch the same child against the
same harness and hope.

This module is the *measurement* half of making harness editing a learnable
capability. It is deliberately split into two phases that must never collapse
into one:

* **propose** — a batch of failure receipts becomes a *typed* proposal (which
  surface, which kind of edit, supported by how many observations).
  Deterministic, no model. A group of one is not a pattern and is omitted.
* **score** — a proposal is judged by the *realized* downstream success it
  produces over the same failure batch, never by the proposer's own opinion.

The proposer's claimed direction (``expected_direction``) is recorded *beside*
the realized outcome and never used to compute it. A proposer that says "this
will improve things" is a claim; the gap between that claim and the measured
outcome is the finding. A proposer that also applied its own edit could not be
validated without acting, so this module applies nothing — it only reports.

No DB, no file I/O, no network, no lane, no model: the caller assembles the
receipts and (optionally) the paired outcome rows; this module reads them.
``classify`` is delegated to ``failure_classifier`` and the delta arithmetic to
``harness_contrast.attribute`` — neither is re-derived here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from mini_ork.learning import failure_classifier, harness_contrast

HARNESS_SURFACES = ("prompt", "stage_order", "verifier", "routing", "recovery")

MIN_SUPPORT = 2

# failure_class -> (target surface, edit kind). ``routing`` is in
# HARNESS_SURFACES but no class maps to it today; a routing defect is reported
# through its observed class until the vocabulary grows.
_TYPED_MAPPING = {
    failure_classifier.OUTPUT_INVALID: ("prompt", "prompt_edit"),
    failure_classifier.PROVIDER_LIMIT: ("recovery", "retry_policy"),
    failure_classifier.INFRA_INTERRUPT: ("recovery", "retry_policy"),
    failure_classifier.INPUT_REQUIRED: ("stage_order", "reorder"),
    failure_classifier.TERMINAL: ("verifier", "gate_edit"),
}


def failure_signature(receipt: Mapping) -> str:
    """Canonical ``failure_class:node`` key for one receipt.

    Classification is delegated to ``failure_classifier.classify``; the node is
    normalized to a non-empty string (``-`` when missing/empty).
    """
    failure_class = failure_classifier.classify(
        reason=receipt.get("reason", ""),
        exit_code=receipt.get("exit_code"),
        signal=receipt.get("signal"),
        stderr=receipt.get("stderr", ""),
        max_turns_hit=receipt.get("max_turns_hit", False),
        provider_status=receipt.get("provider_status"),
    )
    node_value = receipt.get("node")
    node = str(node_value).strip() if node_value is not None else ""
    if not node:
        node = "-"
    return f"{failure_class}:{node}"


def group_failures(receipts: Iterable[Mapping]) -> list[dict]:
    """Group receipts by ``failure_signature``.

    Each group is ``{"signature", "failure_class", "node", "n", "runs",
    "reasons"}`` where ``runs`` is the sorted, de-duplicated list of non-empty
    ``run_id`` values and ``reasons`` is the first up-to-3 distinct non-empty
    ``reason`` values in input order. Sorted by ``(-n, signature)``.
    """
    groups: dict[str, dict] = {}
    for receipt in receipts:
        signature = failure_signature(receipt)
        failure_class, _, node = signature.partition(":")
        group = groups.setdefault(
            signature,
            {
                "signature": signature,
                "failure_class": failure_class,
                "node": node,
                "n": 0,
                "runs": [],
                "reasons": [],
            },
        )
        group["n"] += 1

        run_id = receipt.get("run_id")
        run_id_str = str(run_id).strip() if run_id is not None else ""
        if run_id_str:
            group["runs"].append(run_id_str)

        reason = receipt.get("reason")
        reason_str = str(reason).strip() if reason is not None else ""
        if reason_str and reason_str not in group["reasons"] and len(group["reasons"]) < 3:
            group["reasons"].append(reason_str)

    grouped = []
    for group in groups.values():
        group["runs"] = sorted(set(group["runs"]))
        grouped.append(group)
    grouped.sort(key=lambda g: (-g["n"], g["signature"]))
    return grouped


def propose(receipts: Iterable[Mapping], *, min_support: int = MIN_SUPPORT) -> list[dict]:
    """Emit one typed proposal per group at or above ``min_support``.

    A group below the threshold is omitted entirely — not emitted with a weak
    flag. Each proposal is ``{"signature", "target", "kind", "support",
    "rationale", "evidence"}``. Sorted by ``(-support, signature)``.
    """
    proposals = []
    for group in group_failures(receipts):
        if group["n"] < min_support:
            continue
        target, kind = _TYPED_MAPPING[group["failure_class"]]
        proposals.append(
            {
                "signature": group["signature"],
                "target": target,
                "kind": kind,
                "support": group["n"],
                "rationale": (
                    f"{group['failure_class']} failures implicate the {target} "
                    "harness surface"
                ),
                "evidence": group["runs"],
            }
        )
    proposals.sort(key=lambda p: (-p["support"], p["signature"]))
    return proposals


def score(proposal: Mapping, rows: Iterable[Mapping]) -> dict:
    """Score a proposal by realized outcome, not by its own claim.

    Delegates the delta arithmetic to ``harness_contrast.attribute`` and
    returns that report's keys plus ``proposer_direction`` and ``agrees``.
    ``agrees`` is ``None`` when the delta is unmeasured, the proposal carries
    no claim, or the claim is ``"none"``; otherwise it is whether the realized
    direction matches a claimed improvement. Contamination raises and is not
    swallowed.
    """
    report = harness_contrast.attribute(rows)
    proposer_direction = proposal.get("expected_direction")
    delta = report["delta"]
    if delta is None or proposer_direction is None or proposer_direction == "none":
        agrees = None
    else:
        agrees = (delta > 0) == (proposer_direction == "improve")
    return {**report, "proposer_direction": proposer_direction, "agrees": agrees}


def summarize(receipts, *, rows=None, min_support=MIN_SUPPORT) -> dict:
    """One call: group, propose, and (when ``rows`` is given) score.

    Returns ``{"n_receipts", "n_groups", "n_proposals", "proposals", "scores"}``.
    ``scores`` is ``[score(p, rows) for p in proposals]`` when ``rows`` is not
    ``None``, else ``[]``.
    """
    materialized = list(receipts)
    groups = group_failures(materialized)
    proposals = propose(materialized, min_support=min_support)
    # Materialize the rows ONCE: every proposal is scored against the same
    # batch, and a caller passing a one-shot iterable must not silently leave
    # the second and later proposals unmeasured.
    scored_rows = list(rows) if rows is not None else None
    scores = [score(p, scored_rows) for p in proposals] if scored_rows is not None else []
    return {
        "n_receipts": len(materialized),
        "n_groups": len(groups),
        "n_proposals": len(proposals),
        "proposals": proposals,
        "scores": scores,
    }
