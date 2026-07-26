# Audit Planner

You are planning a multi-lens code audit. Read the kickoff below and emit
a structured plan JSON.

The audit is composed of 5 parallel **lens** stances (all map to
`node_type: "researcher"` in the plan, since they research the codebase):

- **glm-lens** (researcher): fast tactical bottleneck scan (breadth > depth, grep-driven)
- **kimi-lens** (researcher): code-level refactor proposals with concrete before/after diffs
- **codex-lens** (researcher): LLM-dispatch / cost optimization deep-dive
- **opus-lens** (researcher): architectural-shape + final synthesis perspective
- **minimax-lens** (researcher): cross-system integration and data-flow tracing

Plus 1 synthesizer node (`node_type: "reviewer"` — it reviews + composes
the anonymous panel bundle) and 1 completeness-verifier (`node_type:
"verifier"`) and 1 publisher (`node_type: "publisher"`). The recipe declares
the `anonymize_panel` transform statically; do not add it to plan decomposition.

## STRICT node_type ENUM (D-008b / D-017 requirement)

Every `decomposition[].node_type` MUST be EXACTLY ONE of:
- `planner` — emits the plan (you, this call)
- `researcher` — investigates the codebase / scans / reads (USE FOR ALL 5 LENSES)
- `implementer` — writes code/files
- `reviewer` — composes / synthesizes / passes verdict (USE FOR SYNTHESIZER)
- `verifier` — runs deterministic checks (USE FOR COMPLETENESS VERIFIER)
- `reflector` — extracts gradients from completed runs
- `publisher` — commits / merges / publishes artifact (USE FOR PUBLISHER)
- `rollback` — undoes a publish on failure

DO NOT invent plan node_type values like `"lens"`, `"synthesizer"`, or
`"audit"`. The workflow may declare deterministic system nodes such as
`transform`, but plan decomposition accepts agent roles only; unknown plan
node types are rejected at validation (D-008b).

## STRICT output format (D-011 / D-016 requirement)

Respond with **ONLY ONE top-level JSON object**, nothing else:
- NO markdown code fences (` ```json ` or ` ``` `)
- NO leading prose ("Here is the plan:")
- NO trailing analysis / commentary / `<z-insight>` blocks
- NO multiple JSON objects concatenated

If your wrapper appends meta-blocks (z-insight, status reports, etc.),
those break the parser. Emit ONE `{ ... }` and STOP.

## Required top-level JSON keys

- `objective` (string) — what is being audited and on what dimensions
- `assumptions` (string[]) — what about the target codebase you're
  assuming (language, scale, deployment shape)
- `decomposition` (array of `{id, description, node_type, depends_on[]}`)
  with node_type strictly from the enum above
- `dependencies` (array of `{from, to}`) — the 5 researcher lenses must
  depend on planner; the reviewer-synthesizer must depend on all 5 lenses
- `risk_notes` (string[]) — what could go wrong
- `artifact_contract` (`{outputs: string[], success_verifiers: string[]}`)
  - **STRICT (D-018)**: `success_verifiers` MUST be filenames matching
    `verifiers/*.sh` that the framework can shell-execute, NOT natural-language
    sentences. For this recipe the only valid entry is `verifiers/lens-completeness.sh`.
    Express acceptance criteria as `verifier_contract.checks[]` entries
    (those CAN be natural-language with optional `command` field) — NOT in
    `success_verifiers`.
    Wrong: `"All 5 lens-*.md files exist..."`
    Right: `"verifiers/lens-completeness.sh"`
- `verifier_contract` (`{checks: [{id, description, command?}]}`) —
  REQUIRED. At minimum: "all 5 lens reports exist and non-empty",
  "synthesis cross-references all 5 anonymous responses", "each finding cites
  file:line"

--- KICKOFF ---
{{KICKOFF_CONTENT}}
