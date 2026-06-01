# Research-synthesis kickoff (example)

## Topic

What does the 2024-2026 literature say about heterogeneous-family
multi-agent LLM review? Specifically: is the heterogeneity precondition
actually load-bearing (per Rajan 2025 submodularity proof + Nasser
2026 evaluative-fingerprints), or is "more agents" sufficient
regardless of family mix?

## Definition of Done

The recipe produces:

1. Four lens reports under `${MINI_ORK_RUN_DIR}/lens-*.md`:
   - `lens-glm.md` — 10-25 recent web sources (blogs, vendor posts,
     news) with date + author + TL;DR + confidence
   - `lens-kimi.md` — 8-15 arxiv papers with methodology + effect
     size + replication status + citation chain
   - `lens-codex.md` — 8-15 public implementations with file:line
     evidence of the architectural choices people actually ship
   - `lens-opus.md` — 1500-2500 word essay in 6 sections (history,
     conventional wisdom, dissent, edge cases, open questions,
     numbered recommendations)

2. A synthesis at `${MINI_ORK_RUN_DIR}/synthesis.md` with:
   - TL;DR (≤5 bullets)
   - Consensus findings marked ★ / ★★ / ★★★ by lens count
   - Disputed findings reported honestly (no vote-rule)
   - Cross-lens gaps
   - Numbered recommendations (falsifiable)
   - Source manifest

3. `verifiers/source-completeness.sh` passes: each lens has its
   minimum citation count, synthesis references all 4 lens names.

4. Publisher copies `synthesis.md` to `docs/research/synthesis-latest.md`
   and `git commit`s under `mini-ork@local` identity.

## Scope

Read-only research synthesis. No code mutation. No external services
beyond `claude --print` (which the providers wrap for glm/kimi/codex/
opus). Per-lens budget cap: $4 (per-run cap: $30 via `MO_DAILY_BUDGET_USD`).

## Branch

`docs/research/synthesis-latest.md` is the canonical output path.
Per-run lens reports stay under `.mini-ork/runs/<run-id>/`.
