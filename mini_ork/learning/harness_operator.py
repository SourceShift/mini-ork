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

**apply-loop adapter** (kickoff rsi-i5-harness-sweep G02-T01):
``materialize_mutation`` is the proposal → mutation bridge for the apply
loop's harness target surface. It is a pure function with no DB I/O: it reads
the live recipe prompt file and emits an idempotent directive block keyed by
a stable ``source_ref`` (so ``apply_mutation``'s idempotency check skips
re-applies). Non-prompt surfaces (``stage_order``, ``verifier``, ``routing``,
``recovery``) have no prompt file to edit and raise ``ValueError`` — the
caller (``auto_sweep``) is expected to skip them at the gradient-SQL level.
"""
from __future__ import annotations

import os
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


def materialize_mutation(proposal: Mapping, recipe_dir: str,
                         *, node: str) -> tuple[str, str]:
    """Bridge a ``propose()`` proposal into a ``materialize_candidate``-shaped
    pair: ``(suggested_change, source_ref)``.

    The suggested change is a one-paragraph directive carrying the failure
    class, the supporting run ids, and a concrete edit instruction. The
    ``source_ref`` is stable per ``(signature, node)`` so ``apply_mutation``'s
    idempotency check (apply.py:627) skips a re-apply of the same proposal
    on the same node.

    Only ``prompt`` / ``prompt_edit`` proposals are realised — the other
    surfaces (``stage_order``, ``verifier``, ``routing``, ``recovery``) have
    no recipe prompt file to edit and raise ``ValueError``. ``auto_sweep`` is
    expected to skip those at the gradient-SQL level; this is the
    defensive guard for direct callers.

    Pure function: reads the prompt file (if any) to surface a `current`
    excerpt in the directive body, but writes nothing. The probe scorer's
    temp-copy path (``_materialize_arm``) is what writes the directive to disk,
    so this adapter can be called during scoring without touching the live
    recipe prompt file.
    """
    target = proposal.get("target")
    kind = proposal.get("kind")
    if target != "prompt" or kind != "prompt_edit":
        raise ValueError(
            f"materialize_mutation only handles prompt/prompt_edit proposals; "
            f"got target={target!r} kind={kind!r}"
        )
    # ``recipe_dir`` is part of the canonical adapter signature
    # (proposal, recipe, node) — validate it exists so a caller passing a
    # bogus path fails loudly rather than producing a directive that lands
    # on no recipe at all.
    if not recipe_dir or not os.path.isdir(recipe_dir):
        raise ValueError(
            f"materialize_mutation: recipe_dir {recipe_dir!r} does not exist"
        )
    signature = str(proposal.get("signature") or "unknown")
    support = int(proposal.get("support", 0))
    evidence = list(proposal.get("evidence") or [])
    rationale = str(proposal.get("rationale") or "").strip()

    # Stable, idempotent source_ref — keyed on the proposal's signature (which
    # is stable per failure batch) and the recipe node. apply_mutation's
    # idempotency check (apply.py:627) uses this to skip a re-apply.
    source_ref = f"harness_operator:{signature}:{node}"

    # Surface a one-paragraph directive carrying the rationale + concrete
    # edit instruction. The exact text is informational; what matters is
    # that the probe scorer appends it as a directive block to the TEMP
    # recipe copy, not the live recipe (probe_scorer._materialize_arm).
    evidence_str = ", ".join(str(e) for e in evidence[:5])
    suggested = (
        f"harness edit on {node!r}: {rationale} "
        f"(support={support}; evidence=[{evidence_str}]). "
        "Tighten the prompt to remove the failure mode without breaking the "
        "happy-path directive."
    )
    return suggested, source_ref
