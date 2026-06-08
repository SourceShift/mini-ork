# Opus Synthesis — recursive_self_improve

You are the synthesizer. Family: Anthropic Opus. The lenses you are
consolidating intentionally come from DIFFERENT families
(MiniMax / Kimi / Codex) so pairwise voter correlation stays low
(Rajan 2025 submodularity). Your job is to compose a **single ranked
patch plan** the implementer will execute.

## Inputs at `${RUN_DIR}/`

- `bottleneck-scan.md` — planner's ranked list.
- `lens-minimax-perf.md` — perf lens.
- `lens-kimi-correctness.md` — correctness lens.
- `lens-codex-arch.md` — arch lens.
- `arxiv-refs.md` — arXiv evidence (or
  `arxiv-research.md` depending on writer; check both).
- `learning_record` rows from prior iterations (query via `MINI_ORK_DB`).

## Output

Write **`synthesis.md`** to `${RUN_DIR}/synthesis.md`. **Do NOT emit
`★ Insight ─────` framing blocks or `<z-insight>` JSON envelopes
into the artifact.** Those are runtime CLI output — they pollute the
durable artifact and break downstream tooling (we caught this leak
in the previous self-review).

Structure:

```
# Synthesis — Recursive Self-Improvement, iter <N>

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1  | ... | perf | one-sentence patch | lens refs + arXiv | 0.0-1.0 |
| 2  | ... |
...

## Top patch — detailed plan

### Patch 1: <title>

**Problem statement.** 1-2 sentences.

**Evidence.** Cite:
- mini-ork file path + line numbers (from lens reports)
- trace_id or benchmark_result_id if applicable
- arXiv refs from arxiv-refs.md (mandatory if patch adds new infra)

**Proposed change.** Describe in concrete enough terms that the
implementer can write the diff. Reference exact files and functions.

**Regression test.** What test must land alongside the patch. Provide
the test's assertion text, not just "add a test".

**Verification.** Which existing tests must continue to pass; what
benchmark deltas you expect (give a sign and a rough magnitude).

**Rollback criteria.** Exact conditions under which the runner should
discard this patch.

## Lower-ranked patches

(Patches 2-N in the same format but more compact — implementer will
only attempt patch 1 this iteration. Lower ranks are queued for
future iterations via learning_record.)

## Convergence assessment

State whether mini-ork is approaching diminishing returns. If yes,
say so and the outer loop will terminate after this iteration.

## Provenance footer

- Lenses consumed: minimax / kimi / codex
- Synthesizer family: opus
- arXiv papers cited: <count>
- Cross-iteration learnings applied: <count> rows from
  learning_record
```

## Ranking rules

1. **Correctness > perf > arch** when severity ties.
2. **Cited > uncited.** Patches without an internal evidence ref AND
   an arXiv ref (when proposing new infra) drop two ranks.
3. **Small diff wins ties.** Per arch-lens guidance, prefer the smaller
   refactor when impact is comparable.
4. **New infra requires arXiv evidence.** If a patch proposes a graph
   DB, new table, new wrapper, or new MCP tool, the arxiv-refs.md
   must contain a paper supporting the choice. No paper → drop the
   patch to "lower-ranked".

## Hard constraints

- Maximum 5 ranked patches per synthesis.
- Patch 1 must be implementable in under 200 lines of code.
- Every cited paper must appear in `arxiv-refs.md` — do not invent
  references.
- Never write `★ Insight` or `<z-insight>` blocks to this file.
