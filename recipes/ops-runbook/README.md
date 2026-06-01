# Recipe: ops-runbook

5-lens incident runbook generator. Each lens contributes a distinct
operational stance routed to a DIFFERENT model family
(detection=GLM / containment=Kimi / diagnosis=Codex / recovery=Opus /
prevention=MiniMax).

## When to use

- After an incident: capture the playbook while it's still fresh.
- Before a launch: pre-write runbooks for the failure modes you can
  anticipate.
- During on-call onboarding: junior on-call needs the runbook MORE than
  the senior who wrote the code.
- Quarterly hygiene: re-run an existing runbook with the latest data to
  catch drift.

## When NOT to use

- Active incident — use the existing runbook, not write a new one. This
  recipe is for AFTER stop-the-bleeding.
- Architecture redesign — that's a separate planning task.
- Compliance / audit documentation — different audience + format.

## Dispatch

```bash
mini-ork run ops-runbook path/to/kickoff.md
```

See `example-kickoff.md` for the kickoff shape.

## Cost

- Min: $2.00
- Max: $10.00
- Per lens: $1.00

Runtime: 4-12 min.

## Outputs

- `${MINI_ORK_RUN_DIR}/runbook.md` — runnable, sequenced incident playbook
- `${MINI_ORK_RUN_DIR}/lens-{detection,containment,diagnosis,recovery,prevention}.md`
- `${MINI_ORK_RUN_DIR}/plan.json`

## Verifier gate

`verifiers/runbook-completeness.sh` enforces:
1. runbook.md present + ≥ 5 sections (Detection / Containment / Diagnosis / Recovery / Prevention)
2. all 5 lens reports present + ≥ 150 words each
3. TL;DR section present
4. Process-notes audit-trail present
5. Recovery section has Verify or Rollback lines

## Architecture

```
              ┌─────────┐
   kickoff ──▶│ planner │ (sonnet)
              └────┬────┘
                   ├──────────────┬──────────────┬──────────────┬──────────────┬──────────────┐
                   ▼              ▼              ▼              ▼              ▼              ▼
              detection_lens containment_lens diagnosis_lens recovery_lens prevention_lens
                 (GLM)          (Kimi)           (Codex)        (Opus)         (MiniMax)
                   └──────────────┴──────┬───────┴──────────────┴──────────────┴──────────────┘
                                          ▼
                                    synthesizer (opus)
                                          │
                                          ▼
                            runbook-completeness verifier
                                          │
                                ┌─────────┴─────────┐
                                ▼                   ▼
                            publisher           rollback
```

## Why heterogeneous-family for incident response specifically

Incident response is the FAVOURITE case for heterogeneous panels because
each phase needs a different cognitive shape:

- Detection is signal-matching (which alert fires where) → GLM at
  systematic enumeration.
- Containment is decisive + risk-aware (act under uncertainty, but with
  rollback) → Kimi at quantitative-safety.
- Diagnosis is branching deduction (decision tree, hypothesis ladder) →
  Codex at structural reasoning.
- Recovery is sequenced action with verification → Opus at long-horizon
  planning.
- Prevention is adversarial (what else could break this same way?) →
  MiniMax at corner-case generation.

Same-family panel collapses these into one stance and misses 4 of 5
quality axes. Heterogeneous panel preserves them.
