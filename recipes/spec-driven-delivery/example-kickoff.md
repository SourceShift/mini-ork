# Example Kickoff: spec-driven-delivery

## Spec dir:

`/absolute/path/to/specs`

## Hard rules

- The `.md` files in the spec dir are the only source of truth. Do not
  invent requirements; flag anything uncovered in the ratification record.
- Compile one SpecCard per spec; every acceptance criterion gets exactly one
  executable gate.
- Write gates before implementation. Every gate must fail on the untouched
  tree unless tagged `precondition`.
- The lane that writes contracts and gates never implements, and the
  implementer never edits SpecCards, gates, or specs.
- Dispatch each deliverable through `recursive-validate-impl`, one
  deliverable per child kickoff, in dependency order.
- When information is missing, write an ASK artifact and mark the
  deliverable blocked; do not guess.

## Success

- `${MINI_ORK_RUN_DIR}/spec-index.json` lists every spec in the dir.
- `${MINI_ORK_RUN_DIR}/aggregate-verdict.json` has shape
  `{total, delivered, failed, blocked, pending, pass_rate, deliverables[]}`,
  with every deliverable delivered.
- `${MINI_ORK_RUN_DIR}/smoke-live.json` shows zero failed gates, and
  `${MINI_ORK_RUN_DIR}/ledger.jsonl` has a row for every gate verdict,
  including WAIVED ones.

## Verification command

- `bin/mini-ork specs lint /absolute/path/to/specs`
