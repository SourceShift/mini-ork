# framework-edit

Proposes verifier-gated 2+ file edits to the mini-ork repo itself.
Produces a unified diff plus `verdict.json` without applying the change.

## Topology

```
planner → [code_impact_lens, prior_art_lens] → codex_implementer
                                                ↓
                              [static_check_verifier, test_verifier]
                                                ↓
                                        opus_arbiter
                                                ↓
                                     verifier_smith
                                                ↓
                                     recipe_validator
                                                ↓
                              [publisher, rollback]
```

## Failure-Mode Coverage

| Failure mode | Verifier / mitigation |
|---|---|
| Drafter collapses to identical DAG | Recipe-validator enforces ≥3 distinct model families |
| Verifier rubber-stamps (empty checks) | Verifier output contract: empty `checks[]` = hard failure |
| Reviewer verdict parse regression | Arbiter node hard-fails if `verdict` missing or ∉ {approve,revise,reject} |
| Artifact naming drift | Binding artifact manifest in planner; copied verbatim downstream |
| Typecheck silently skipped | Static-check verifier records skip as explicit check entry |
| Web smoke tests need network | Test verifier scrubs env and asserts hermetic execution |
| High-blast-radius edit without approval | Static-check verifier blocks exact-match path list unless kickoff token present |
| Schema-touching without migration file | Static-check verifier flags missing `0NNN_*.sql` pair |
| Recipe-validator NameError on booleans | Self-test against known-good recipe (`recipes/code-fix/`) |
