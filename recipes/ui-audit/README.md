# Recipe: ui-audit

5-lens UI audit recipe. Each lens audits a distinct quality axis routed
to a DIFFERENT model family
(a11y=GLM / perf=Kimi / visual=Codex / interaction=Opus / edge-cases=MiniMax).
Synthesizer ranks findings by severity (P0..P3) with file:line anchors.

## When to use

- Pre-release audit of a high-traffic surface.
- Quarterly hygiene sweep over a feature cluster.
- Post-redesign verification.
- Onboarding-experience audit (1-3 surfaces a new user touches first).

## When NOT to use

- Single-component micro-tweak (use direct code review).
- Visual-only polish pass (visual_lens alone is fine — full panel is
  overkill).
- Major incident triage (use `ops-runbook` recipe instead).

## Dispatch

```bash
mini-ork run ui-audit path/to/kickoff.md
```

(See `example-kickoff.md` for the kickoff shape.)

## Cost

Per `task_class.yaml.cost_model`:
- Min: $2.00 (small surface, single page)
- Max: $12.00 (multi-surface flow audit)
- Per lens: $1.20

Runtime: 4-15 min wall-clock.

## Outputs

- `${MINI_ORK_RUN_DIR}/findings.md` — ranked findings (P0..P3) + cross-lens patterns + lens-contributions summary
- `${MINI_ORK_RUN_DIR}/lens-{a11y,perf,visual,interaction,edge}.md` — per-lens reports
- `${MINI_ORK_RUN_DIR}/plan.json` — planner output

## Verifier gate

`verifiers/findings-completeness.sh` enforces:
1. findings.md + all 5 lens reports present
2. ≥ 1 finding entry overall
3. Cross-lens patterns section present
4. Lens-contributions summary table present
5. Process-notes audit-trail block present

## Architecture

```
              ┌─────────┐
   kickoff ──▶│ planner │ (sonnet)
              └────┬────┘
                   ├─────────┬───────────┬───────────┬───────────┬───────────┐
                   ▼         ▼           ▼           ▼           ▼           ▼
              a11y_lens  perf_lens   visual_lens interaction_lens          edge_lens
                (GLM)     (Kimi)      (Codex)      (Opus)                  (MiniMax)
                   └─────────┴───────────┴──────┬────┴───────────┴───────────┘
                                                ▼
                                          synthesizer (opus)
                                                │
                                                ▼
                                  findings-completeness verifier
                                                │
                                       ┌────────┴────────┐
                                       ▼                 ▼
                                  publisher           rollback
```

## Why heterogeneous-family for UI audit specifically

UI audit is the FAVOURITE case for heterogeneous-family panels — each
quality axis has a different cognitive shape:

- A11y is rule-based (WCAG criteria as a checklist) → GLM does well at
  systematic enumeration.
- Perf is metric-based (numbers + traces) → Kimi at quantitative.
- Visual is pattern-based (token-system conformance) → Codex at structural.
- Interaction is empathic (mental-model simulation) → Opus at narrative.
- Edge cases is adversarial (what breaks?) → MiniMax at corner-case
  generation.

Routing each axis to a different family is a practical proxy for reducing
cross-lens correlation. Rajan 2025 supports the low-correlation detector
pattern; Nasser 2026 supports treating model/judge choice as a substantive
source of evaluative disposition rather than an interchangeable detail.
