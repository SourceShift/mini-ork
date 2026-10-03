# Per-Spec Dispatcher

For each SpecCard in `${MINI_ORK_RUN_DIR}/spec-cards/`, walk its
`deliverables` in dependency order (cross-spec `<spec_id>/<id>` edges
included). Dispatch them serially, one at a time. Write one child kickoff per
deliverable and run the inner implementation recipe:

```bash
bin/mini-ork run recursive-validate-impl <child-kickoff.md>
```

Do not start a deliverable until every deliverable it depends on has a
delivered verdict. If a dependency failed or is blocked, mark the dependent
`pending` and skip it.

Write each child kickoff to
`${MINI_ORK_RUN_DIR}/child-kickoffs/<spec_id>--<deliverable_id>.md`. It must
include:

- One deliverable only: its spec id, deliverable id, title, and
  `depends_on`. One deliverable = one kickoff.
- The SpecCard as the sole anchor: the card path, its `source_hash`, and the
  clause and acceptance entries this deliverable references. Do not add
  requirements from chat history, earlier failed patches, or other specs.
- The gates as Definition of Done probes, copied verbatim from
  `${MINI_ORK_RUN_DIR}/gates/<spec_id>.json` for every
  `acceptance_ref` of this deliverable.
- A hard rule that the child must not edit the SpecCard, the gate file, or
  the source spec, and must keep its patch scoped and preserve unrelated user
  changes.

## Authorship separation

This node runs on the `implementer` lane, and the child implementer runs on
`worker`. Contracts and gates were written on the `planner` and `reviewer`
lanes, and neither this node nor its children may edit them. If a gate looks
wrong or unsatisfiable, do not work around it. Raise an ASK.

## Structured ASK instead of guessing

When the SpecCard or gates lack information needed to write a correct child
kickoff, do not guess. Write the ASK to
`${MINI_ORK_RUN_DIR}/asks/<spec_id>--<deliverable_id>.json`. Deliverable ids
are unique only within one card, so the spec id prefix keeps two specs'
`D1` asks from overwriting each other.

```json
{
  "spec_id": "s11-owner-dashboard",
  "deliverable_id": "D2",
  "question": "the specific missing fact",
  "blocking_refs": ["AC3", "F2"],
  "options": ["candidate answers, if any"],
  "evidence": ["paths that show the gap"]
}
```

Then mark the deliverable `blocked`, set its `ask_path`, and move to the next
one that does not depend on it.

## Dispatch record

Record every deliverable in `${MINI_ORK_RUN_DIR}/dispatch-results.json`:

```json
{
  "deliverables": [
    {
      "deliverable_id": "D1",
      "spec_id": "s11-owner-dashboard",
      "child_kickoff": "path",
      "child_run_id": "run-id-or-null",
      "child_run_dir": "path-or-null",
      "status": "delivered|failed|blocked|pending",
      "verdict_path": "path-or-null",
      "ask_path": "path-or-null"
    }
  ]
}
```

`status` reflects what the child run's own verdict artifact says. Never mark
a deliverable `delivered` unless the child verdict says `pass: true`. Treat
missing run ids, malformed verdicts, and incomplete child runs as `pending` or
`failed`. Only verifier nodes give verdicts, and `dispatch_aggregator`
recomputes every status from the child run directories, not from this file's
claims.
