# Recipe: blog-post

5-lens collaborative blog drafting recipe. Each lens contributes from a
distinct perspective routed to a DIFFERENT model family
(editor=GLM / researcher=Kimi / narrative=Codex / audience=Opus /
counter=MiniMax). Synthesizer composes the final draft.

## When to use

- You're drafting a thought-leadership post and want a heterogeneous-family
  panel pass instead of single-vendor drafting.
- The post has ≥ 2 load-bearing claims that need primary-source grounding.
- You want a steelman of the opposing case in the body, not just the
  cheerful version.

## When NOT to use

- Quick announcement post (≤ 300 words) — overhead exceeds value.
- Internal-only memo where audience already shares full context.
- Strongly time-sensitive (recipe takes 3-12 min).

## Dispatch

```bash
mini-ork run blog-post path/to/kickoff.md
```

(See `example-kickoff.md` for the kickoff shape.)

## Cost

Per `cost_model` in `task_class.yaml`:
- Min: $1.50
- Max: $8.00
- Per lens: $0.80 (5 lenses × 1 dispatch + 1 synthesizer + 1 verifier)

Runtime: 3-12 min wall-clock depending on lens dispatch parallelism.

## Outputs

- `${MINI_ORK_RUN_DIR}/draft.md` — final post (with process-notes block at end)
- `${MINI_ORK_RUN_DIR}/lens-{editor,researcher,narrative,audience,counter}.md` — per-lens briefs
- `${MINI_ORK_RUN_DIR}/plan.json` — planner output

## Verifier gate

`verifiers/draft-completeness.sh` enforces:
1. plan.json present + parseable
2. draft.md ≥ 0.8 × target_word_count
3. each lens-*.md ≥ 200 words
4. synthesizer Process-notes block present
5. no fabrication-smell URLs (fake/example/placeholder/lorem paths)

## Architecture

```
              ┌─────────┐
   kickoff ──▶│ planner │ (sonnet)
              └────┬────┘
                   ├─────────────┬─────────────┬─────────────┬─────────────┐
                   ▼             ▼             ▼             ▼             ▼
              editor_lens   researcher_lens narrative_lens audience_lens counter_lens
                (GLM)          (Kimi)         (Codex)        (Opus)        (MiniMax)
                   └─────────────┴──────┬──────┴─────────────┴─────────────┘
                                        ▼
                                  synthesizer (opus)
                                        │
                                        ▼
                            draft-completeness verifier
                                        │
                                  ┌─────┴──────┐
                                  ▼            ▼
                              publisher    rollback
```

## Heterogeneity rationale

Per Rajan 2025 (arxiv:2511.16708) + Nasser 2026 (arxiv:2601.05114):
multi-agent panels are most defensible when their detectors are low
redundancy and their judges do not share the same evaluative disposition.
Rajan reports low detector correlation in CodeX-Verify; Nasser reports
near-zero agreement across LLM judges under the same rubric.

This recipe assigns each of the 5 perspective stances to a DISTINCT
model family by construction. The names of the lens roles
(editor/researcher/narrative/audience/counter) are stable; the model
mapping is governed by `config/agents.yaml`.
