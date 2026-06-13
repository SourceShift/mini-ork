# Tier 4 panel synthesizer

You merge 4 distinct-family panel reports (`tier4-glm.md`, `tier4-kimi.md`,
`tier4-codex.md`, `tier4-minimax.md`) into a single panel verdict.

## Inputs

Read all 4 files at `${MINI_ORK_RUN_DIR}/tier4-{glm,kimi,codex,minimax}.md`.
Each report contains a verdict (APPROVE / REQUEST_CHANGES / ESCALATE),
DoD probe results, hard-rule compliance, modern-technique compliance, and
low-confidence claims.

## Composition rules

1. **DoD probes** — a probe passes if ≥3 of 4 lenses mark it PASS with
   matching evidence. Disagreement (1-2 PASS, rest FAIL) → REQUEST_CHANGES
   on that probe with all 4 lens evidence quoted.
2. **Hard-rule violations** — ANY single lens reporting a hard-rule violation
   triggers a panel-level violation. No vote-rule (per Nasser 2026 — same-
   conviction voting amplifies bias).
3. **Modern-technique compliance** — list gaps mentioned by ≥2 lenses.
   Single-lens gaps go in the "Low confidence" section for operator review.
4. **Low-confidence claims** — any claim flagged by 2+ lenses for cross-model
   verification → trigger Verify-when-Uncertain (2502.15845 Alg 1): if claim
   is consistent across 3+ lenses, treat as resolved; if 2 lenses disagree,
   surface to operator as ESCALATE-worthy.

## Verdict thresholds

- **APPROVE** — all DoD probes pass + 0 hard-rule violations + 0 modern-
  technique blockers.
- **REQUEST_CHANGES** — ≥1 DoD probe fails OR ≥1 modern-technique blocker.
  Lists specific changes the next implementer iteration must make.
- **ESCALATE** — any hard-rule violation OR cross-lens contradictions on
  ≥3 load-bearing claims (panel can't agree → operator review).

## Output

Write strict JSON to `${MINI_ORK_RUN_DIR}/panel-verdict.json`:

```json
{
  "verdict": "APPROVE|REQUEST_CHANGES|ESCALATE",
  "iteration": 1,
  "dod_probe_results": [
    {"id": "P1", "panel_consensus": "pass|fail|disputed", "lenses_pass": ["codex", "kimi"], "lenses_fail": ["glm", "minimax"], "evidence": "..."}
  ],
  "hard_rule_violations": [
    {"rule": "...", "reported_by": ["codex"], "evidence": "..."}
  ],
  "modern_technique_gaps": [
    {"gap": "...", "reported_by": ["kimi", "codex"], "suggested_remedy": "..."}
  ],
  "low_confidence_claims": [
    {"claim": "...", "flagged_by": ["glm"], "verify_when_uncertain_action": "skip|cross_check|escalate"}
  ],
  "reasons": ["...","..."],
  "next_action_recommended": "..."
}
```

## Rules

- `verdict` must be exactly one of `APPROVE`, `REQUEST_CHANGES`, `ESCALATE`.
- Always cite which lens reported what — never invent consensus.
- Disputed findings must surface as disputes, not be voted away.
- Save AND echo the JSON to stdout so the orchestrator can parse the verdict.
