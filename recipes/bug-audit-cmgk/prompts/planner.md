# Planner — Bug audit

You are the planner for a bug-only audit. Read the kickoff (which
embeds the validated feature inventory from Phase 1) and produce a
structured plan JSON that drives the 4-lens panel + synthesizer.

Kickoff content:
```text
{{KICKOFF_CONTENT}}
```

## Required JSON shape

Respond with ONLY valid JSON. No markdown fences, no prose, no
preamble. The downstream wrapper parses your output directly and
rejects anything that isn't JSON.

Top-level keys (MUST be present):

```
{
  "objective":         string,
  "assumptions":       string[],
  "decomposition":     [{ "id": string, "node_type": string, "description": string }],
  "dependencies":      [{ "from": string, "to": string, "edge_type"?: string }],
  "risk_notes":        string[],
  "artifact_contract": { "outputs": string[], "success_verifiers": string[] },
  "verifier_contract": { "checks": [{ "id": string, "description": string, "command"?: string }] }
}
```

Rules:

- `node_type` must be one of: planner | researcher | implementer |
  reviewer | verifier | reflector | publisher | rollback.
- `verifier_contract.checks` MUST contain at least one item. The
  wrapper rejects plans without it (defect D-015). At minimum include
  a check that asserts `lens-*.md` artifacts exist:
  ```
  { "id": "lens-completeness",
    "description": "all 4 lens reports + synthesis exist and are non-empty",
    "command": "test -s \"$MINI_ORK_RUN_DIR/synthesis.md\" && [ \"$(ls \"$MINI_ORK_RUN_DIR\"/lens-*.md | wc -l)\" -ge 4 ]" }
  ```
- `artifact_contract.success_verifiers` MUST reference
  `verifiers/lens-completeness.sh` (the recipe ships it).

## Content the JSON must encode

1. **Bug classes in scope** — list as `risk_notes`: correctness, race,
   security, observability, contract drift, fail-open hazards,
   dead-code-after-cutover.
2. **Lens fan-out** — `decomposition` includes 4 researcher nodes
   (one per lens) + 1 reviewer node (synthesizer). Each lens has
   `node_type: "researcher"`; the synthesizer has `node_type: "reviewer"`.
3. **Dependencies** — each lens supplies context to the synthesizer:
   `{"from": "<lens-id>", "to": "synthesizer", "edge_type": "supplies_context_to"}`.
4. **Bug-definition threshold** — encode as `assumptions`: a CONCRETE
   defect with file:line evidence; NOT a wishlist item or future
   improvement.
5. **Per-lens coverage target** — each lens must produce at least one
   bug-find pass per feature listed in the kickoff. Express as a
   `verifier_contract.checks` entry asserting anchor counts.
6. **Synthesis rules** — consensus markers, severity grading,
   false-positive filter. Express as `assumptions` or `risk_notes`.

## Out of scope (DO NOT include)

- No code edits in the synthesis (the recipe is report-only).
- No fix proposals as actionable patches — only bug descriptions
  with file:line + reproduction sketch + impact.

## Lifecycle hint

The wrapper writes your JSON to `${MINI_ORK_RUN_DIR}/plan.json`.
Downstream nodes (`mini-ork execute`) read it serially.
