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
      "id": "implementer",
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
