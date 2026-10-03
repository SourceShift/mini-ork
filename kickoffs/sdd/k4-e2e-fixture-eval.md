# K4 — end-to-end fixture dry-run eval + docs for spec-driven-delivery

Design doc (authoritative, read first):
`/Users/admin/ps/mini-ork-sdd-wt/docs/plans/2026-10-03-spec-driven-delivery.md`
Prerequisites (already on this branch): K1 specdir, K2 recipe, K3 verifiers.

## Goal

Prove the deterministic spine of `spec-driven-delivery` end-to-end on a toy
fixture WITHOUT spending LLM budget, and document the recipe for consumers.

## Feature scope

- Fixture target repo: `tests/fixtures/sdd_e2e/target/` — a tiny git-initable
  python project (one module `calc.py` missing a `multiply` function, one
  passing test for existing `add`).
- Fixture spec dir: `tests/fixtures/sdd_e2e/specs/` — one spec
  `multiply-feature.md` describing the multiply feature with: Inputs/Outputs/
  Edge-cases sections, two acceptance criteria each with a backticked cmd
  probe (`python3 -m pytest -q tests/test_multiply.py` style) that FAILS on
  the untouched fixture tree, and a `precondition` probe that passes
  (`python3 -m pytest -q tests/test_add.py`).
- Golden SpecCard: `tests/fixtures/sdd_e2e/golden/multiply-feature.spec-card.json`
  — a hand-written, schema-valid SpecCard for that spec (what the
  contract_compiler SHOULD produce), used to test the deterministic pipeline
  downstream of the LLM nodes.
- `tests/test_sdd_e2e_dryrun.py` — an integration test that, inside a tmp
  copy of the fixture: runs `specs ingest` + `specs lint` on the spec dir;
  plants the golden SpecCard + a gates file into a synthetic
  `MINI_ORK_RUN_DIR`; runs `ratification-check.py`, `test-validity.py`
  (asserting broken-baseline logic: acceptance probes fail on untouched tree
  → validity PASSES), then applies the trivial multiply implementation to the
  tmp tree, runs `smoke-live.py` (now gates pass), a synthetic
  `dispatch-results.json` through `dispatch-aggregator.py`, and
  `ledger-writer.py`; asserts `ledger.jsonl` + `slide-back.json` contents.
- Docs: extend `recipes/spec-driven-delivery/README.md` with a "Run it"
  section (env table, example command line
  `bin/mini-ork run spec-driven-delivery kickoffs/<your-kickoff>.md`,
  kickoff `## Spec dir:` convention) and add a row for the recipe to the
  repo-level recipe index if one exists (check `recipes/docs/` or the
  top-level README recipe table — update whichever lists recipes; if none
  lists them, skip).
- CHANGELOG.md entry under Unreleased: `feat(recipes): spec-driven-delivery —
  spec-dir → verified features with live smoke + UI-craft gates`.

## Definition of Done (probes)

```bash
# P1: e2e dry-run integration test passes
python3 -m pytest -q tests/test_sdd_e2e_dryrun.py

# P2: fixture spec survives real CLI lint with zero errors
./bin/mini-ork specs lint tests/fixtures/sdd_e2e/specs --json | python3 -c "import json,sys; f=json.load(sys.stdin); assert not [x for x in f if x.get('severity')=='error'], f; print('lint clean')"

# P3: whole suite green
python3 -m pytest -q
```

## Hard rules

- The integration test must not invoke any LLM lane — deterministic spine
  only (ingest → lint → ratification → test-validity → smoke → aggregate →
  ledger).
- Fixture stays tiny (< 200 lines total across fixture files).
- Do not modify verifier/recipe behavior to make the test pass; if a gap is
  found, fix it in place and cover it with a unit test in the matching K3
  test module.

## Success command

```bash
python3 -m pytest -q tests/test_sdd_e2e_dryrun.py
```

## Verification command

- `python3 -m pytest -q tests/test_sdd_e2e_dryrun.py`

## Expected outputs

- `${MINI_ORK_RUN_DIR}/implementer-summary.json`
- tier evidence logs per recursive-validate-impl contract
