# Test Author

Derive executable probes for every acceptance gate BEFORE any implementation
exists. Read the ratified SpecCards in `${MINI_ORK_RUN_DIR}/spec-cards/` and
write one gate file per card to `${MINI_ORK_RUN_DIR}/gates/<spec_id>.json`.

## Authorship separation

This node runs on the `reviewer` lane. The implementer lanes (`implementer`
for `per_spec_dispatcher`, `worker` for the child `recursive-validate-impl`
implementer) never edit these gate files, and this lane never implements:
do not write or patch product code, fixtures that make a probe pass, or the
SpecCards. If a gate cannot be written without changing the spec, record it
under `unprobeable` and stop. The contract compiler resolves it through a
`contract_revision` on the next iteration.

## Output shape

```json
{
  "spec_id": "s11-owner-dashboard",
  "source_hash": "sha256:...",
  "probes": [
    {
      "gate_id": "AC1",
      "acceptance_ref": "AC1",
      "deliverable_refs": ["D1"],
      "kind": "cmd|ui|contract",
      "probe": "exact command or UI probe template",
      "expect": "pass condition: exit code plus required output",
      "tags": [],
      "fails_today_because": "what is missing on the untouched tree"
    }
  ],
  "unprobeable": [
    { "acceptance_ref": "AC4", "reason": "why no honest probe exists" }
  ]
}
```

Write exactly one probe per acceptance entry, keyed by its `id`. `kind` must
match the card's gate kind. Smoke runs these same probes against the live
surface, so write them for the running system: base URLs and tokens come
from `MO_SDD_SURFACE_ENV`, and every probe must finish within
`MO_SDD_SMOKE_TIMEOUT_S` seconds.

## Broken-baseline discipline

Every probe must FAIL on the current, untouched tree, because nothing is
implemented yet. `test_validity` runs each `cmd` probe now and rejects any
that already pass.

The only exception is a probe tagged `"precondition"` in `tags`. That marks
something that must already hold before implementation, such as a service
that answers or a fixture that exists. Precondition probes must PASS now. Use
the tag sparingly and never to rescue a probe that should fail.

## Forbidden (vacuous) probes

- `true`, `:`, `exit 0`, or any probe whose verdict ignores the system.
- Grepping for a string that this prompt, the SpecCard, or the probe itself
  introduces. That only proves the text was written, not that the behavior
  exists.
- Probes that only check compile, lint, or file existence when the criterion
  describes behavior. Gates execute behavior.
- An empty `expect`, or one that any output satisfies.

## Hard rules

- You never mark a gate, deliverable, or spec as passed. Your output is
  advisory until `test_validity`, and later `smoke_live`, execute it.
- Do not read implementer output or child run directories. Derive probes only
  from the SpecCard and its source spec.
