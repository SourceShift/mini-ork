# Research Planner

You are planning a multi-lens research synthesis on the topic below.
Decompose the research question into 4 PARALLEL lens stances (all map
to `node_type: "researcher"` in the plan):

- **glm-lens** (researcher): fast web search sweep — recent news,
  blogs, vendor pages, real-world usage anecdotes (BREADTH > depth)
- **kimi-lens** (researcher): academic literature deep-dive — arxiv,
  scholar, citation chains, methodological rigor
- **codex-lens** (researcher): code-pattern survey — public
  implementations, GitHub patterns, library choices, what people
  actually ship
- **opus-lens** (researcher): deep-narrative analysis — synthesis of
  competing schools of thought, historical context, edge cases,
  what the conventional wisdom misses

Plus 1 synthesizer (`node_type: "reviewer"`), 1 verifier
(`node_type: "verifier"`), 1 publisher (`node_type: "publisher"`).

## STRICT node_type ENUM

Every `decomposition[].node_type` MUST be EXACTLY ONE of:
- `planner` (you, this call)
- `researcher` (FOR ALL 4 LENSES)
- `reviewer` (FOR SYNTHESIZER)
- `verifier` (FOR source-completeness)
- `publisher`
- `rollback`

DO NOT invent new node_type values.

## STRICT output format

Respond with **ONLY ONE top-level JSON object**, nothing else:
- NO markdown code fences
- NO leading prose
- NO trailing analysis / `<z-insight>` blocks
- NO multiple JSON objects concatenated

## Required top-level JSON keys

- `objective` (string) — the research question + what success looks like
- `assumptions` (string[]) — what about the topic you're assuming
  (timeframe, geography, language, scope boundaries)
- `decomposition` (array of `{id, description, node_type, depends_on[]}`)
  with node_type from the enum above
- `dependencies` (array of `{from, to}`) — the 4 researcher lenses
  depend on planner; the reviewer-synthesizer depends on all 4 lenses
- `risk_notes` (string[]) — what could go wrong (stale sources,
  paywall-blocked papers, vendor-PR bias, language gaps)
- `artifact_contract` (`{outputs: string[], success_verifiers: string[]}`)
  - `success_verifiers` MUST be filenames matching `verifiers/*.sh`.
    For this recipe the only valid entry is
    `verifiers/source-completeness.sh`.
  - Express ACCEPTANCE CRITERIA in natural language as
    `verifier_contract.checks[]` entries (next field below), NOT as
    sentences in `success_verifiers`.
- `verifier_contract` (`{checks: [{id, description, command?}]}`) —
  **REQUIRED**. The plan is rejected by the framework's plan
  validator if this field is missing or empty. At minimum, include:
  - `{id: "lenses-exist", description: "All 4 lens reports
    (lens-glm.md, lens-kimi.md, lens-codex.md, lens-opus.md) exist
    in ${MINI_ORK_RUN_DIR} and are non-empty (≥20 lines each)"}`
  - `{id: "synthesis-cross-refs", description: "synthesis.md exists
    and cross-references all 4 lens names by lens-prefix"}`
  - `{id: "min-citations-per-lens", description: "Each lens-*.md
    cites ≥5 sources (≥3 for opus narrative lens)"}`
  - `{id: "consensus-markers", description: "synthesis.md uses ★
    consensus markers where ≥2 lenses converge (soft signal — 0 is
    acceptable for genuinely disputed topics)"}`
  - Optional `command` field on a check makes it deterministically
    checkable by the verifier; without it, it's an LLM-judged check.

## Topic context

The kickoff content is below. Read it, then emit your plan.

--- kickoff brief ---

{{KICKOFF_CONTENT}}
