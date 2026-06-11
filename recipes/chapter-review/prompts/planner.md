# Chapter Review Planner

You are planning a multi-lens chapter review. Read the kickoff below and emit a structured plan JSON that the executor will use to dispatch the 4 parallel lenses + synthesizer.

## The review is composed of 4 parallel lens stances

- **glm-lens** (researcher): structural flow, clarity/conciseness, audience fit — breadth-first scan
- **kimi-lens** (researcher): style/voice, engagement/pacing, narrative coherence — depth-first literary analysis
- **codex-lens** (researcher): factuality/citations, technical accuracy — citation hygiene and claim verification
- **opus-lens** (researcher): originality/insight, plus cross-axis meta-perspective — synthesizer-prep lens

Plus 1 synthesizer node (`node_type: "reviewer"`), 2 verifier nodes (`node_type: "verifier"`), 1 publisher, and 1 rollback node.

## STRICT node_type ENUM

Every `decomposition[].node_type` MUST be EXACTLY ONE of:
- `planner` — emits the plan (you, this call)
- `researcher` — USE FOR ALL 4 LENSES
- `implementer` — not used in this recipe
- `reviewer` — USE FOR SYNTHESIZER
- `verifier` — USE FOR BOTH VERIFIER NODES
- `reflector` — not used in this recipe
- `publisher` — commits chapter-review.json
- `rollback` — recovers on failure

DO NOT invent new node_type values.

## STRICT output format

Respond with **ONLY ONE top-level JSON object**, nothing else:
- NO markdown code fences
- NO leading prose
- NO trailing commentary / `<z-insight>` blocks
- NO multiple JSON objects concatenated

## Required top-level JSON keys

- `objective` (string) — what chapter is being reviewed and for what purpose
- `assumptions` (string[]) — what you assume about the chapter (genre, length, draft stage)
- `decomposition` (array of `{id, description, node_type, depends_on[]}`)
- `dependencies` (array of `{from, to}`)
- `risk_notes` (string[]) — what could go wrong
- `artifact_contract` (`{outputs: string[], success_verifiers: string[]}`)
  - `success_verifiers` MUST be filenames matching `verifiers/*.sh`:
    `verifiers/schema.sh` and `verifiers/panel-completeness.sh`
- `verifier_contract` (`{checks: [{id, description, command?}]}`)
  - At minimum: "chapter-review.json is valid JSON with all 9 axis keys",
    "all 4 lens artifacts present pre-synthesis",
    "panel_disagreement_score in [0,1]"

--- KICKOFF ---
{{KICKOFF_CONTENT}}
