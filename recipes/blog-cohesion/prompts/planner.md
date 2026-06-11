# Blog-cohesion Planner

You are planning a 5-lens cohesion audit of an EXISTING blog-post draft.
The kickoff below names the post path. Your plan must surface that path
verbatim so the lens nodes can read the post.

## Decomposition shape

Five PARALLEL researcher lenses, each with a distinct structural focus
(all map to `node_type: "researcher"` in the plan):

- **thesis** (researcher / glm_lens) — one-thesis check: identify the
  load-bearing thesis sentence and flag over-restatement (RST anti-pattern).
- **bridge** (researcher / codex_lens) — H2 boundary audit: every
  section-to-section transition should carry a load-bearing bridge
  sentence (not pure signposting).
- **topic** (researcher / opus_lens) — paragraph topic-sentence audit:
  Nielsen F-pattern — the first sentence of each paragraph should carry
  the paragraph's load-bearing claim.
- **entity** (researcher / minimax_lens) — Halliday entity-continuity:
  every adjacent sentence pair within paragraphs (and paragraph pair
  across H2 boundaries) needs ≥1 cohesive tie.
- **rhythm** (researcher / kimi_lens) — Tufte/Lorch rhythm: paragraph
  length variance, one structural cue per ~250–350 words, em-dash
  density ≤1.5 per 100 visible words.

Plus 1 reviewer (the **synth_arbiter** — name MUST contain "synth" so
the executor routes it as a synthesizer that writes `applied_post.md`
directly, not as a classic reviewer that emits a verdict envelope),
1 verifier (cohesion-completeness), 1 publisher, 1 rollback.

## STRICT node_type ENUM

Every `decomposition[].node_type` MUST be EXACTLY ONE of:
- `planner` (you, this call)
- `researcher` (FOR ALL 5 LENSES)
- `reviewer` (FOR THE ARBITER)
- `verifier` (FOR cohesion-completeness)
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

- `objective` (string) — MUST begin with the literal substring
  `POST_PATH: <absolute path to the .md file from the kickoff>` on
  the first line, followed by a one-sentence statement of what
  cohesion success looks like. The `POST_PATH:` marker is what lens
  nodes grep for to find the post.
- `assumptions` (string[]) — what about the draft you're assuming
  (length range, audience, voice constraints from frontmatter).
- `decomposition` (array of `{id, description, node_type, depends_on[]}`)
  with node_type from the enum above. The 5 researcher entries MUST
  use the ids `thesis`, `bridge`, `topic`, `entity`, `rhythm`. The
  reviewer entry MUST use id `synth_arbiter` (the "synth" substring
  is required by mini-ork's reviewer dispatch).
- `dependencies` (array of `{from, to}`) — the 5 lenses depend on
  planner; the synth_arbiter (reviewer) depends on all 5 lenses; the
  verifier depends on synth_arbiter.
- `risk_notes` (string[]) — what could go wrong (post outside
  600–1500 word range, code-heavy post with little prose, frontmatter
  voice rules conflict with arbiter edits, lens disagreement on
  topic-sentence opener).
- `artifact_contract` (`{outputs: string[], success_verifiers: string[]}`)
  - `success_verifiers` MUST be `["verifiers/cohesion-completeness.sh"]`.
  - `outputs` MUST include `applied_post.md` and `apply_log.md`.
- `verifier_contract` (`{checks: [{id, description, command?}]}`) —
  **REQUIRED**. At minimum include:
  - `{id: "lens-jsons-exist", description: "All 5 lens JSON outputs
    (context-thesis.json, context-bridge.json, context-topic.json,
    context-entity.json, context-rhythm.json) exist in ${MINI_ORK_RUN_DIR}
    and parse as valid JSON with a top-level verdict field"}`
  - `{id: "applied-post-exists", description: "applied_post.md exists
    in ${MINI_ORK_RUN_DIR}, has YAML frontmatter, and is in the
    range 80%–130% of the source post's word count (figures excluded)"}`
  - `{id: "apply-log-exists", description: "apply_log.md exists with
    the required sections: Inputs read / Applied / Rejected / Open
    carve-outs"}`
  - `{id: "frontmatter-preserved", description: "applied_post.md
    preserves the original frontmatter fields (title, description,
    pubDate, draft, tags, authors)"}`
  - Optional `command` field makes a check deterministic.

## Topic context

The kickoff content is below. Read it, identify the post path, then
emit your plan with `POST_PATH:` as the first marker in `objective`.

--- kickoff brief ---

{{KICKOFF_CONTENT}}
