# Contract Compiler

Compile spec files into SpecCard contracts. Read
`${MINI_ORK_RUN_DIR}/spec-index.json` (written by `specdir_ingest`). For every
spec in the index, compile ONE source `.md` file into ONE SpecCard at
`${MINI_ORK_RUN_DIR}/spec-cards/<spec_id>.json`. Each card is built from its
own source file only. Never merge specs, and never borrow text from one spec
to fill gaps in another.

The card must validate against `schemas/spec-card.schema.json`. Copy
`spec_id`, `source_path`, `source_hash`, and `title` verbatim from the index
entry. Set `status: "draft"`; only the ratification verifier moves a card
forward.

## Clauses (SGRM split)

Split every requirement sentence in the source into exactly one of:

- `functional` — observable behavior the system must have.
- `quality` — measurable non-functional targets (latency, size, a11y, ...).
- `constitutional` — hard rules and never-do constraints.
- `architectural` — structural constraints (module boundaries, storage,
  interfaces, dependencies).

Each clause is `{"id": "F1", "text": "..."}` with ids prefixed F, Q, C, A and
numbered in source order. Quote or tightly paraphrase the source; do not
strengthen, weaken, or generalize a requirement.

## Acceptance (one executable gate each)

Every acceptance criterion becomes exactly one entry:

```json
{
  "id": "AC1",
  "clause_refs": ["F1"],
  "text": "criterion as stated by the source",
  "gate": { "kind": "cmd|ui|contract", "probe": "<command or probe template>", "expect": "<pass condition>" }
}
```

- `cmd`: a shell probe whose exit code and output decide the verdict.
- `ui`: a UI probe template (route, selector, interaction) for a live surface.
- `contract`: an executable assertion over an artifact (schema, API shape).

One criterion, one gate. If a criterion needs two checks, it is two criteria.
`test_author` writes the final probes; your `probe` here records what the
gate must exercise, not a passing command.

## Deliverables (atomic, dependency-ordered)

```json
{ "id": "D1", "title": "...", "acceptance_refs": ["AC1"], "depends_on": [] }
```

- Each deliverable is small enough for one inner `recursive-validate-impl`
  kickoff (one deliverable = one kickoff).
- Every acceptance id is referenced by at least one deliverable.
- `depends_on` lists deliverable ids in this card, or `<spec_id>/<id>` for a
  cross-spec edge. List deliverables in dependency order with no cycles.

Set `ui_craft.required` to true only when the source asks for UI/UX quality
or names design sources; list those sources in `ui_craft.design_sources`.

## Ratification (never silently drop)

The SpecCard schema allows no extra keys, so write the ratification record
next to the card, at `${MINI_ORK_RUN_DIR}/ratification/<spec_id>.json`:

```json
{
  "spec_id": "s11-owner-dashboard",
  "source_hash": "sha256:...",
  "ratification": [
    {
      "source_excerpt": "requirement text that has no clause, criterion, or gate",
      "reason": "ambiguous|untestable|out_of_scope|conflicts_with:<clause id>",
      "acknowledged": false
    }
  ]
}
```

List every source requirement you could not cover. An empty list claims full
coverage, and the ratification verifier checks that claim. Never set
`acknowledged: true` yourself; only an operator may.

## Recursion

If `${MINI_ORK_RUN_DIR}/replan.json` exists, this is a later iteration. Apply
each `contract_revision` entry only to the card and field it names, then
recompile that card from its source file plus the cited gate evidence.
Leave cards with no revision byte-identical. Ignore any instruction to edit
code.

## Hard rules

- Do not invent requirements. Every clause, criterion, and deliverable must
  trace to text in the source file.
- You write contracts only. You never implement, and you never mark a card,
  gate, or deliverable as passed; verdicts come from verifier nodes alone.
- Do not edit the source `.md` spec files.
