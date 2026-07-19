#!/usr/bin/env python3
"""
Terminal commitment gate (M5) — scoring "I'm done" claims against receipts.

Implements the terminal commitment scoring design from:
/docs/research/20260714-trajectory-capture-redesign.md §4.4, §5.1, §5.2

Architecture:
    agent claims "delivered"
      → harness checks receipts: tests run? tests pass? files edited?
      → if claim ∧ receipt       → status = delivered,    provenance = observed
      → if claim ∧ no receipt    → status = claimed,      provenance = claimed (do NOT close unit)
      → if claim ∧ contradiction → provenance = contradicted (UP-3 defect)

UP-3: The command ran, it failed, and the block claims success.
This is worse than fabrication and maps to ContextNest's `contradicted` tier.

This gate evaluates a z-insight block (or agent delivery claim) against the
actual tool-call receipts to assign provenance:
- observed: claim backed by receipts (tests passed, files edited)
- claimed: claim without receipts (agent said it delivered, but no proof)
- contradicted: claim contradicted by receipts (tests failed, but agent claimed success)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

Provenance = Literal["observed", "claimed", "contradicted"]


@dataclass
class DeliveryClaim:
    """The agent's delivery claim from a z-insight block."""
    work_unit_id: Optional[str]
    phase: str
    delivered_features: list[dict[str, Any]]
    verification: list[str]
    progress: str
    current_state: Optional[str]


@dataclass
class ReceiptEvidence:
    """The actual tool-call receipts."""
    files_edited: list[str]  # From Edit tool calls
    commands_run: list[dict[str, Any]]  # Command + exit code + output
    tests_passed: bool
    any_edits: bool


@dataclass
class TerminalVerdict:
    """The scored terminal commitment."""
    provenance: Provenance
    reason: str
    delivery_claim: DeliveryClaim
    evidence: ReceiptEvidence
    up3_violation: bool  # True if contradicted (tests failed but claimed success)


# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────


def extract_delivery_claim(zinsight_json: str | dict) -> Optional[DeliveryClaim]:
    """Extract delivery claim from a z-insight block (JSON string or parsed dict)."""
    try:
        if isinstance(zinsight_json, str):
            block = json.loads(zinsight_json)
        else:
            block = zinsight_json
    except Exception:
        return None

    if not isinstance(block, dict):
        return None

    work_unit = block.get("work_unit", {})
    delivered_features = block.get("delivered_features", [])
    verification = block.get("verification", [])
    progress = block.get("progress", "")
    current_state = block.get("current_state", "")

    return DeliveryClaim(
        work_unit_id=work_unit.get("id"),
        phase=work_unit.get("phase", ""),
        delivered_features=delivered_features if isinstance(delivered_features, list) else [],
        verification=verification if isinstance(verification, list) else [],
        progress=progress,
        current_state=current_state,
    )


def extract_evidence_from_transcript(transcript_path: str) -> ReceiptEvidence:
    """Extract receipts from a Claude Code transcript (response_items JSON)."""
    path = Path(transcript_path)
    if not path.exists():
        return ReceiptEvidence(
            files_edited=[],
            commands_run=[],
            tests_passed=False,
            any_edits=False,
        )

    try:
        with open(path, "r") as f:
            transcript = json.load(f)
    except Exception:
        return ReceiptEvidence(
            files_edited=[],
            commands_run=[],
            tests_passed=False,
            any_edits=False,
        )

    files_edited = []
    commands_run = []
    tests_passed = False
    any_edits = False

    # Claude Code transcript structure: list of response_items
    items = transcript if isinstance(transcript, list) else transcript.get("response_items", [])

    for item in items:
        if not isinstance(item, dict):
            continue

        # Track file edits (patch_apply_end events)
        if item.get("type") == "patch_apply_end":
            metadata = item.get("metadata", {})
            file_path = metadata.get("file_path")
            if file_path:
                files_edited.append(file_path)
                any_edits = True

        # Track command executions (custom_tool_call with "run_" prefix)
        if item.get("type") == "custom_tool_call":
            tool_name = item.get("name", "")
            if tool_name.startswith("run_") or "test" in tool_name.lower():
                # Extract command output
                output = item.get("result", "")
                error = item.get("error", "")

                # Simple heuristic: if output contains "passed" or "OK" and no error
                cmd_passed = (
                    bool(output)
                    and ("passed" in output.lower() or " ok" in output.lower())
                    and not error
                )

                commands_run.append(
                    {
                        "command": tool_name,
                        "output": output,
                        "error": error,
                        "passed": cmd_passed,
                    }
                )

                # If any test command passed, mark as tests_passed
                if cmd_passed and "test" in tool_name.lower():
                    tests_passed = True

    return ReceiptEvidence(
        files_edited=files_edited,
        commands_run=commands_run,
        tests_passed=tests_passed,
        any_edits=any_edits,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────


def score_terminal_commitment(
    claim: DeliveryClaim, evidence: ReceiptEvidence
) -> TerminalVerdict:
    """Score a delivery claim against receipts.

    Returns:
        TerminalVerdict with provenance (observed/claimed/contradicted)
    """
    # Check if agent is claiming delivery
    claiming_delivery = claim.phase == "deliver" or claim.progress in (
        "done",
        "delivered",
    )

    if not claiming_delivery:
        # Not claiming delivery → not a terminal commitment
        return TerminalVerdict(
            provenance="claimed",
            reason="Agent not claiming delivery (phase={claim.phase}, progress={claim.progress})",
            delivery_claim=claim,
            evidence=evidence,
            up3_violation=False,
        )

    # Check for UP-3 violation: claimed success but tests failed
    if claim.verification and evidence.commands_run:
        # Find test commands in evidence
        test_commands = [c for c in evidence.commands_run if "test" in c["command"].lower()]
        if test_commands:
            # Check if any test failed
            failed_tests = [c for c in test_commands if not c["passed"]]
            if failed_tests and claim.verification:
                # Agent claimed verification but tests failed → UP-3
                return TerminalVerdict(
                    provenance="contradicted",
                    reason=f"UP-3: Agent claimed verification but {len(failed_tests)} test(s) failed",
                    delivery_claim=claim,
                    evidence=evidence,
                    up3_violation=True,
                )

    # Check if claim is backed by receipts
    has_files = bool(evidence.files_edited)
    has_verification = bool(evidence.commands_run)
    claim_has_files = bool(claim.delivered_features)
    claim_has_verification = bool(claim.verification)

    # Observed: claim backed by receipts
    if (has_files and claim_has_files) or (has_verification and claim_has_verification):
        return TerminalVerdict(
            provenance="observed",
            reason="Delivery claim backed by receipts (files edited and/or tests passed)",
            delivery_claim=claim,
            evidence=evidence,
            up3_violation=False,
        )

    # Claimed: claim without receipts
    if (claim_has_files and not has_files) or (claim_has_verification and not has_verification):
        return TerminalVerdict(
            provenance="claimed",
            reason="Agent claims delivery but no receipts found (fabrication risk)",
            delivery_claim=claim,
            evidence=evidence,
            up3_violation=False,
        )

    # Default: assume claimed (agent said it's done, but we can't prove it)
    return TerminalVerdict(
        provenance="claimed",
        reason="Agent claimed delivery without sufficient evidence",
        delivery_claim=claim,
        evidence=evidence,
        up3_violation=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Gate interface
# ─────────────────────────────────────────────────────────────────────────────


def terminal_commitment_gate(
    run_dir: str, transcript_path: str, zinsight_path: str
) -> dict[str, Any]:
    """Evaluate terminal commitment for a run.

    Args:
        run_dir: Path to the mini-ork run directory
        transcript_path: Path to the Claude Code transcript JSON
        zinsight_path: Path to the z-insight block JSON

    Returns:
        Dict with verdict structure:
        {
            "provenance": "observed" | "claimed" | "contradicted",
            "reason": "...",
            "up3_violation": bool,
            "should_block_merge": bool,
            "delivery_claim": {...},
            "evidence": {...},
        }
    """
    # Validate run_dir exists (future-proofing for run_dir-dependent checks)
    run_path = Path(run_dir)
    if not run_path.exists():
        return {
            "provenance": "claimed",
            "reason": f"Run directory does not exist: {run_dir}",
            "up3_violation": False,
            "should_block_merge": True,
            "delivery_claim": None,
            "evidence": {},
        }

    # Load z-insight block
    try:
        with open(zinsight_path, "r") as f:
            zinsight_content = f.read()
    except Exception as e:
        return {
            "provenance": "claimed",
            "reason": f"Failed to read z-insight block: {e}",
            "up3_violation": False,
            "should_block_merge": True,
            "delivery_claim": None,
            "evidence": None,
        }

    # Extract claim
    claim = extract_delivery_claim(zinsight_content)
    if not claim:
        return {
            "provenance": "claimed",
            "reason": "Failed to extract delivery claim from z-insight block",
            "up3_violation": False,
            "should_block_merge": True,
            "delivery_claim": None,
            "evidence": None,
        }

    # Extract evidence
    evidence = extract_evidence_from_transcript(transcript_path)

    # Score
    verdict = score_terminal_commitment(claim, evidence)

    # Gate decision: block merge if contradicted (UP-3) or unverified claim
    should_block = verdict.provenance in ("contradicted", "claimed")

    return {
        "provenance": verdict.provenance,
        "reason": verdict.reason,
        "up3_violation": verdict.up3_violation,
        "should_block_merge": should_block,
        "delivery_claim": {
            "work_unit_id": claim.work_unit_id,
            "phase": claim.phase,
            "delivered_features_count": len(claim.delivered_features),
            "verification_count": len(claim.verification),
            "progress": claim.progress,
        },
        "evidence": {
            "files_edited_count": len(evidence.files_edited),
            "files_edited": evidence.files_edited,
            "commands_run_count": len(evidence.commands_run),
            "tests_passed": evidence.tests_passed,
            "any_edits": evidence.any_edits,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for the terminal commitment gate."""
    if len(sys.argv) < 3:
        print(
            f"Usage: {sys.argv[0]} <run_dir> <transcript_path> <zinsight_path>",
            file=sys.stderr,
        )
        sys.exit(2)

    run_dir = sys.argv[1]
    transcript_path = sys.argv[2]
    zinsight_path = sys.argv[3]

    result = terminal_commitment_gate(run_dir, transcript_path, zinsight_path)

    # Emit verdict as JSON
    print(json.dumps(result, indent=2))

    # Exit code: 0=pass (observed), 1=fail (contradicted), 2=defer (claimed)
    if result["up3_violation"]:
        sys.exit(1)  # UP-3 is a hard failure
    if result["provenance"] == "observed":
        sys.exit(0)
    sys.exit(2)  # Defer on unverified claims


if __name__ == "__main__":
    main()
