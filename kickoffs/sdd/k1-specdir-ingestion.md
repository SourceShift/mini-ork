# K1 — specdir ingestion library + `mini-ork specs` CLI

Design doc (authoritative, read first):
`/Users/admin/ps/mini-ork-sdd-wt/docs/plans/2026-10-03-spec-driven-delivery.md`

## Goal

Add the deterministic (zero-LLM) spec-directory ingestion layer: a
`mini_ork/specdir/` package, a `schemas/spec-card.schema.json` +
`schemas/spec-index.schema.json`, a `mini-ork specs` CLI subcommand, a test
fixture spec directory, and pytest coverage.

## Feature scope

- `mini_ork/specdir/scan.py` — scan a directory for spec files
  (`MO_SDD_SPEC_GLOB`, default `*.md`, non-recursive by default,
  `--recursive` flag), skipping README/index files; emit a spec inventory with
  `source_path` (absolute), `source_hash` (sha256 of bytes), `title` (first
  H1 or filename), `spec_id` (slugified filename, unique).
- `mini_ork/specdir/spec_card.py` — SpecCard dataclass mirroring the design
  doc's YAML shape exactly (clauses: functional/quality/constitutional/
  architectural; acceptance[] with gate {kind: cmd|ui|contract, probe,
  expect}; deliverables[] with depends_on; ui_craft; status enum
  draft|ratified|dispatched|delivered|failed|blocked), with
  `to_dict`/`from_dict` and jsonschema validation against
  `schemas/spec-card.schema.json`.
- `mini_ork/specdir/index.py` — build/write/read `spec-index.json`
  (validates against `schemas/spec-index.schema.json`): entries keyed by
  `spec_id` with source_path/source_hash/title/status plus a
  `generated_at` ISO timestamp; detect dependency cycles across deliverables
  and duplicate spec_ids as hard errors.
- `mini_ork/specdir/lint.py` — deterministic spec lint returning a list of
  `{spec_id, code, severity, message}` findings. Codes (all from the design
  doc's "7 authoring errors" reduced to what is deterministically checkable):
  `NO_ACCEPTANCE` (no acceptance-criteria-like section), `NO_VERIFY_CMD`
  (no backticked command anywhere), `VAGUE_CRITERIA` (acceptance lines
  containing configurable vague terms: "should work", "properly", "correctly",
  "as expected", "robust", "seamless"), `OVERSIZE` (> `MO_SDD_SPEC_MAX_BYTES`,
  default 262144), `MISSING_SECTIONS` (none of Inputs/Outputs/Errors/
  Edge-cases/Examples-like headings present), `DUP_ID`, `DEP_CYCLE`.
  Severity: DEP_CYCLE/DUP_ID/NO_ACCEPTANCE = error; others = warning.
- `mini_ork/cli/specs.py` + wiring in the existing subcommand dispatch so
  `bin/mini-ork specs ingest <dir> [--out PATH] [--recursive]`,
  `bin/mini-ork specs list <spec-index.json>`, and
  `bin/mini-ork specs lint <dir> [--json]` work. `ingest` writes
  `spec-index.json` (default `<dir>/spec-index.json`) and exits non-zero if
  any error-severity lint finding exists. Follow the structure of an existing
  small CLI module (e.g. `mini_ork/cli/epics.py`) for argument parsing and
  registration.
- Fixture: `tests/fixtures/specdir/` with `good-feature.md` (clean spec with
  acceptance criteria, backticked verify command, Inputs/Outputs/Edge-cases
  sections) and `bad-feature.md` (no acceptance section, no commands, vague
  terms) so lint has one clean and one dirty target.
- Tests: `tests/test_specdir_ingest.py` covering scan/index/lint/CLI
  round-trip (invoke the CLI via subprocess on the fixture dir; assert
  spec-index.json validates against the schema; assert bad-feature yields
  NO_ACCEPTANCE + NO_VERIFY_CMD; assert exit codes).

## Definition of Done (probes)

```bash
# P1: new package imports and schemas are valid JSON Schema
python3 -c "import json, pathlib; [json.loads(pathlib.Path(p).read_text()) for p in ['schemas/spec-card.schema.json','schemas/spec-index.schema.json']]; import mini_ork.specdir.scan, mini_ork.specdir.spec_card, mini_ork.specdir.index, mini_ork.specdir.lint"

# P2: specdir tests pass
python3 -m pytest -q tests/test_specdir_ingest.py

# P3: CLI end-to-end on the fixture
./bin/mini-ork specs lint tests/fixtures/specdir --json | python3 -c "import json,sys; f=json.load(sys.stdin); assert any(x['code']=='NO_ACCEPTANCE' for x in f), f"

# P4: whole suite not broken
python3 -m pytest -q
```

## Hard rules

- Zero LLM calls anywhere in `mini_ork/specdir/` — pure deterministic Python,
  stdlib + existing project deps only (jsonschema is already a dependency).
- Do not modify existing recipes, dispatch code, or `mini_ork/cli/` modules
  other than the minimal subcommand registration touchpoint.
- Absolute paths in all emitted artifacts.
- Keep naming/style consistent with neighboring `mini_ork/` packages.

## Success command

```bash
python3 -m pytest -q tests/test_specdir_ingest.py
```

## Verification command

- `python3 -m pytest -q tests/test_specdir_ingest.py`

## Expected outputs

- `${MINI_ORK_RUN_DIR}/implementer-summary.json`
- tier evidence logs per recursive-validate-impl contract
