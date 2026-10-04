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

Coverage rule (MUST): every imperative or constraint line in the source maps
to exactly one clause bucket. That includes MUST / never / do not / always /
only / reuse lines, naming rules, and visibility rules. Map repo-convention
lines to `constitutional`:

- naming: "snake_case everywhere."
- library reuse: "Reuse the existing QR renderer; do not add a new QR
  library."
- auth visibility: "The badge renders for authenticated owners."

Placement lines ("next to the existing `add(a, b)`", "in `calc.py`") go in
`architectural`; edge cases and input/output behavior go in `functional`.
Being hard to test is not a reason to leave a line out of the clauses. A line
that is mapped to a clause MUST NOT also appear in `ratification[]`.

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

## Ratification (real gaps and conflicts only, never silently drop)

The SpecCard schema allows no extra keys, so write the ratification record
next to the card, at `${MINI_ORK_RUN_DIR}/ratification/<spec_id>.json`:

```json
{
  "spec_id": "s11-owner-dashboard",
  "source_hash": "sha256:...",
  "ratification": [
    {
      "source_excerpt": "The badge is visible to every visitor.",
      "reason": "conflicts_with:C2 — C2 limits the badge to authenticated owners.",
      "acknowledged": false
    }
  ]
}
```

`ratification[]` may ONLY contain a source requirement that either:

- cannot be mapped to any clause bucket without strengthening, weakening, or
  inventing it, or
- conflicts with another clause (start the reason with
  `conflicts_with:<clause id>`).

Every entry REQUIRES all three keys:

- `source_excerpt`: the source text, verbatim.
- `acknowledged`: `true` or `false`. You always write `false`; only an
  operator may set `true`.
- `reason`: one sentence. It names the conflict, or the specific reason the
  line cannot be mapped. A bare label ("untestable", "ambiguous") is not a
  reason.

"Hard to test", "vague", "a convention", or "non-functional" never justify an
entry. Those lines still become `quality` or `constitutional` clauses. An
empty list is the expected result for a well-formed spec. It claims full
coverage, and the ratification verifier checks that claim. A requirement
that is not a clause must be a ratification entry; never drop one silently.

## Worked example

Source, in the shape of the `multiply-feature` fixture spec with a
constraints section added:

```markdown
# Multiply two numbers

Add `multiply(a, b)` to `calc.py`, next to the existing `add(a, b)`.

## Inputs
- `a`, `b`: two numbers, int or float.

## Outputs
- `multiply(a, b)` returns the product `a * b`.

## Edge cases
- A zero operand returns 0.
- Signs: `multiply(-2, 3) == -6` and `multiply(-2, -3) == 6`.
- Floats: `multiply(0.5, 4) == 2.0`.

## Constraints
- snake_case for every new name.
- Use only the standard library; do not add a dependency.
- Do not change `add(a, b)`; its existing test keeps passing.
```

Clauses: the placement line is architectural, inputs, outputs and edge cases
are functional, and every constraint line is constitutional.

```json
"clauses": {
  "functional": [
    {"id": "F1", "text": "a and b are two numbers, int or float"},
    {"id": "F2", "text": "multiply(a, b) returns the product a * b"},
    {"id": "F3", "text": "a zero operand returns 0"},
    {"id": "F4", "text": "multiply(-2, 3) == -6 and multiply(-2, -3) == 6"},
    {"id": "F5", "text": "multiply(0.5, 4) == 2.0"}
  ],
  "quality": [],
  "constitutional": [
    {"id": "C1", "text": "snake_case for every new name"},
    {"id": "C2", "text": "use only the standard library; do not add a dependency"},
    {"id": "C3", "text": "do not change add(a, b); its existing test keeps passing"}
  ],
  "architectural": [
    {"id": "A1", "text": "multiply(a, b) is added to calc.py, next to the existing add(a, b)"}
  ]
}
```

Ratification record: `{"spec_id": "multiply-feature", "source_hash":
"sha256:...", "ratification": []}`. Every line is a clause, so the list is
empty.

Wrong: a mappable constraint dumped into ratification.

```json
"ratification": [
  {"source_excerpt": "snake_case for every new name.", "reason": "untestable", "acknowledged": false}
]
```

Right: the same line as `{"id": "C1", "text": "snake_case for every new
name"}` in `constitutional`, and `"ratification": []`.

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
