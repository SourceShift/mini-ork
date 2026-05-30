# Audit Planner

You are planning a multi-lens code audit. Read the kickoff below and emit
a structured plan JSON.

The audit is composed of 4 parallel **lens** stances:

- **glm-lens**: fast tactical bottleneck scan (breadth > depth, grep-driven)
- **kimi-lens**: code-level refactor proposals with concrete before/after diffs
- **codex-lens**: LLM-dispatch / cost optimization deep-dive
- **opus-lens**: architectural-shape + final synthesis composer

Plus 1 synthesis node that composes the 4 lens reports into a ranked
finding matrix.

Your plan must emit valid JSON with these top-level keys:

- `objective` (string) — what is being audited and on what dimensions
- `assumptions` (string[]) — what about the target codebase you're
  assuming (language, scale, deployment shape)
- `decomposition` (array of `{id, description, node_type, depends_on[]}`):
  one entry per lens + one for synthesis + one for completeness
  verifier + one for publisher
- `dependencies` (array of `{from, to}`) — the 4 lenses must depend on
  planner; synthesizer must depend on all 4 lenses
- `risk_notes` (string[]) — what could go wrong (cost overrun, lens
  contradicts itself, audit target is too large)
- `artifact_contract` (`{outputs: string[], success_verifiers: string[]}`)
- `verifier_contract` (`{checks: [{id, description, command?}]}`) —
  REQUIRED. At minimum: "all 4 lens reports exist and non-empty",
  "synthesis cross-references all 4 lenses", "each finding cites
  file:line"

Respond with ONLY valid JSON. No markdown fences, no prose.

--- KICKOFF ---
{{KICKOFF_CONTENT}}
