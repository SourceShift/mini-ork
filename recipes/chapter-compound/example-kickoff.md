# Example kickoff — Write Chapter 1 via chapter-compound

This is an example kickoff for the chapter-compound recipe. The libwit BE stages the inputs via `server/resources/daytona/run-miniork-agent.cjs:stageChapterCompoundInputs`; this file shows what a manual operator-side dispatch looks like for testing.

## Problem

Write Chapter 1 of the "Self-Evolving by Design" book (chapter_title: "The Static Pipeline and Its Seven Signals") via multi-pass writer fanout with audit-bundle output.

## Definition of Done

Produce a valid `chapter-compound.json` under `${MINI_ORK_RUN_DIR}` containing:
- 4 writer-lens drafts (glm, kimi, codex, opus) — all `accepted_for_review`
- 4 critique cells (one chapter-review per draft)
- Selection rationale + composite scores
- Revisions array (0-2 iterations)
- `final_markdown` (4000-8000 words)
- `cost_summary.total_usd` ≤ $5.00

Both `schema.sh` and `writer-completeness.sh` verifiers MUST pass.

## Scope

- Target chapter: chapter-compound-inputs/chapter_contract.json + evidence_pack.md + chapter_outline.md + context.json + style_blueprint.md
- Budget: $5 max per chapter
- Max revise iterations: 2

## Lineage

Dispatched by libwit BE via runMiniOrkRecipe('chapter-compound', ...) → Hatchet dispatchMiniOrkCritique → Daytona miniork-stable-v1 sandbox → run-miniork-agent.cjs → mini-ork run.

## Manual smoke test (without libwit)

```bash
export MINI_ORK_RUN_DIR=/tmp/chapter-compound-smoke-$(date +%s)
mkdir -p $MINI_ORK_RUN_DIR/chapter-compound-inputs

# Stage minimal inputs for smoke
cat > $MINI_ORK_RUN_DIR/chapter-compound-inputs/chapter_contract.json <<'JSON'
{
  "genre": { "chapter_genre": "technical-tutorial" },
  "structure_mode": "plan_steps_exact",
  "required_h2_count": 6,
  "calibration_bucket": "oreilly_technical",
  "forbidden_constructs": []
}
JSON
cat > $MINI_ORK_RUN_DIR/chapter-compound-inputs/evidence_pack.md <<'MD'
# Evidence
- [arXiv:2510.05520] CAM architecture paper — Constructivist Agentic Memory partitions
  memory into episodic, semantic, and skill stores.
MD
cat > $MINI_ORK_RUN_DIR/chapter-compound-inputs/chapter_outline.md <<'MD'
## Summary
This chapter introduces the static-pipeline problem and the 7 signals.

## Key topics
1. Static pipelines
2. Seven signals
3. Artifact-first patterns
4. CAM partitioning
5. Decay mechanisms
6. SAP-based feedback loop
MD
cat > $MINI_ORK_RUN_DIR/chapter-compound-inputs/context.json <<'JSON'
{
  "chapter_title": "The Static Pipeline and Its Seven Signals",
  "chapter_number": 1,
  "book_topic": "Self-Evolving by Design",
  "book_audience": "AI engineers",
  "language": "en",
  "style_blueprint_summary": "O'Reilly technical voice; 2nd-person; concrete examples",
  "prior_chapters_outline": [],
  "max_revise_iterations": 2
}
JSON
echo "# Stub style blueprint" > $MINI_ORK_RUN_DIR/chapter-compound-inputs/style_blueprint.md

# Dispatch
mini-ork run chapter-compound recipes/chapter-compound/example-kickoff.md
```
