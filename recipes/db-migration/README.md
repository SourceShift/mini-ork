# Recipe: db-migration

5-lens DB-migration audit + planner. Each lens reviews a distinct risk
axis routed to a DIFFERENT model family
(integrity=GLM / rollback=Kimi / perf=Codex / compat=Opus / edge-data=MiniMax).
Synthesizer produces an idempotent + reversible migration plan with
forward SQL, reversal SQL, snapshot step, smoke script, and a runnable
rollback playbook.

## When to use

- Any migration touching > 100k rows in production.
- Any migration adding NOT NULL / CHECK / UNIQUE constraints to populated
  tables.
- Migrations that backfill from another column or external source.
- Schema changes consumed by ≥ 2 downstream consumers.

## When NOT to use

- Trivial additive migrations (add a nullable column with no constraint —
  use direct review).
- Dev-only migrations where rollback isn't required.
- Cross-database / heterogeneous-RDBMS migrations (out of scope; needs
  per-RDBMS recipe).

## Dispatch

```bash
mini-ork run db-migration path/to/kickoff.md
```

(See `example-kickoff.md` for kickoff shape.)

## Cost

- Min: $2.50
- Max: $12.00
- Per lens: $1.20

Runtime: 5-15 min wall-clock.

## Outputs

- `${MINI_ORK_RUN_DIR}/migration-plan.md` — unified plan (TL;DR + 6 sections + risk summary + rollback playbook)
- `${MINI_ORK_RUN_DIR}/lens-{integrity,rollback,perf,compat,edge}.md` — per-lens audits
- `${MINI_ORK_RUN_DIR}/plan.json` — planner output (change_kind + env + downtime tolerance)

## Verifier gate

`verifiers/migration-completeness.sh` enforces:
1. migration-plan.md + all 5 lens reports present
2. each lens ≥ 200 words
3. plan has `IF (NOT) EXISTS` idempotency guards
4. plan has Reversal / Rollback SQL
5. plan has Snapshot / backup section
6. plan has Smoke script
7. plan has Risk summary table
8. plan has Process notes audit trail

## Architecture

```
              ┌─────────┐
   kickoff ──▶│ planner │ (sonnet)
              └────┬────┘
                   ├───────────────┬───────────────┬───────────────┬───────────────┬───────────────┐
                   ▼               ▼               ▼               ▼               ▼               ▼
              integrity_lens  rollback_lens    perf_lens       compat_lens     edge_lens
                  (GLM)          (Kimi)          (Codex)         (Opus)          (MiniMax)
                   └───────────────┴────────┬──────┴───────────────┴───────────────┴───────────────┘
                                            ▼
                                      synthesizer (opus)
                                            │
                                            ▼
                            migration-completeness verifier
                                            │
                                  ┌─────────┴─────────┐
                                  ▼                   ▼
                              publisher           rollback
```

## Why heterogeneous-family for DB migration specifically

Schema migrations are HIGH-STAKES — wrong migration on prod can mean
hours of downtime or irreversible data loss. The decision space is wide
and the failure modes are diverse. Same-family panel collapses these
into one stance:

- Data integrity is rule-based (constraints + types) → GLM at
  systematic enumeration.
- Rollback safety is reversibility-counterfactual → Kimi at structured-
  step thinking.
- Perf impact is metric + locking semantics → Codex at quantitative
  systems reasoning.
- App-code compat is downstream-impact tracing → Opus at long-horizon
  dependency reasoning.
- Edge-case data is adversarial — what weird rows exist that nobody
  remembered → MiniMax at corner-case generation.

These axes are nearly orthogonal — pairwise ρ across them is low by
construction. That's exactly the Rajan 2025 precondition for multi-agent
panels to actually catch what single-vendor misses.
