# Planner prompt

You are the planner for `recursive-validate-impl`. Build a concrete plan for
the kickoff's technical feature work and its recursive validation loop.

The full kickoff content is interpolated below.

## Kickoff

{{KICKOFF_CONTENT}}

## Planner instructions

Read the kickoff carefully. Extract the complete `## Definition of Done
(probes)` section into `dod_probes[]`. Each probe must preserve enough command
text for a reviewer to rerun it. If `dod_probes[]` would be empty, reject the
plan and return a failure plan with `pass: false`, because this recipe requires
machine-checkable convergence.

Output strict JSON:

```json
{
  "objective": "one sentence",
  "assumptions": [],
  "dod_probes": [
    {
      "id": "P1",
      "command": "exact command or shell block",
      "expected": "what passing means"
    }
  ],
  "decomposition": [
    {
      "id": "implementer_feature_change",
      "description": "make the scoped feature change",
      "node_type": "implementer",
      "depends_on": []
    }
  ],
  "verifier_contract": {
    "checks": [
      {
        "id": "tier1_compile",
        "description": "compile, typecheck, and lint touched files"
      },
      {
        "id": "tier2_unit",
        "description": "run scoped tests adjacent to touched files"
      },
      {
        "id": "tier3_property",
        "description": "run property or mutation checks when configured"
      },
      {
        "id": "tier4_panel",
        "description": "run DoD probes, hard-rule review, and modern-technique compliance review"
      }
    ]
  },
  "recursion": {
    "max_iterations": 5,
    "convergence_check": "all_dod_probes_pass"
  },
  "risk_notes": [],
  "pass": true
}
```

Rules:
- `verifier_contract.checks` must include at least one row per tier.
- `dod_probes[]` must not be empty.
- Keep the first implementation scope small enough for one iteration.
- Preserve hard rules from the kickoff as plan constraints.
- **`node_type` is a CLOSED enum — every `decomposition[].node_type` MUST be
  EXACTLY one of these 8 bare values, with no suffix and no invented subtype:**
  `planner`, `researcher`, `implementer`, `reviewer`, `verifier`, `reflector`,
  `publisher`, `rollback`. It names the step's logical ROLE. The recipe's
  `workflow.yaml` already maps each role onto the concrete tier/lens nodes
  (tier1_compile, tier2_unit, tier3_property, the 4 tier4 lenses, quorum,
  synth, replanner) — you do NOT re-declare those. The validator (D-008b)
  rejects the whole plan on the FIRST out-of-enum value.
- Do NOT emit `preflight`, `verifier:tier1`, `verifier:tier4`, `verifier:merge`,
  `replan`, or any `role:subrole` colon form as a `node_type` — they are not in
  the enum and will fail the plan. Multi-tier verification is one or more steps
  each with the bare `node_type: "verifier"`; the recursive replan step uses
  `node_type: "planner"`.
- Put descriptive, unique names in the step `id` (e.g. `tier4_codex_review`,
  `implementer_jest_invariants`, `verifier_tier2_unit`). The `id` is free-form;
  the `node_type` is the closed enum. Never push a descriptive label into
  `node_type`.
