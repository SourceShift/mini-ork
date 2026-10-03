# Replanner

Apply `reflector.json` to the current SpecCards in
`${MINI_ORK_RUN_DIR}/spec-cards/` and produce `replan.json`. The goal is to
make the next iteration's contracts smaller, clearer, and more testable
without losing intent the source spec requires.

The spec is the only mutable artifact. Your only output is
`contract_revision` entries against SpecCards. Each must cite the gate
evidence that justifies it.

Return strict JSON:

```json
{
  "contract_revisions": [
    {
      "spec_id": "spec-id",
      "deliverable_id": "D1",
      "field": "acceptance[AC1].gate.probe|deliverables[D1].depends_on|...",
      "before": "current value",
      "after": "revised value",
      "reason": "why this follows from reflector evidence",
      "evidence_refs": ["gate evidence paths"]
    }
  ],
  "regenerate": ["spec_id/deliverable_id"],
  "next_iteration_focus": [],
  "stop": false,
  "operator_escalation": null
}
```

Replanning policy:

- Regenerate from the SpecCard plus the latest gate evidence. Every listed
  deliverable restarts from a fresh child kickoff, never from chat history or
  a previous child's patch.
- Do not emit code-side instructions ("patch function X", "add a retry to
  Y", "fix the failing test"). Patch-the-patch is forbidden. If the fix
  belongs in code, the right revision is a clearer gate or a smaller
  deliverable.
- Split a deliverable when its child run failed because the scope was too
  broad.
- Add a `precondition` gate when verifiers lacked data, fixtures, or
  environment setup.
- A revision that contradicts the source spec is not allowed. Set
  `operator_escalation` instead.
- Stop only when every deliverable is delivered or the divergence rule fired.
  Escalate when the same failed-deliverable set repeats without progress.
