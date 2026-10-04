# K6 — contract-compiler coverage determinism (constraints are clauses, not ratification noise)

## Goal

Make `recipes/spec-driven-delivery/prompts/contract-compiler.md` (plus, if
needed, a small deterministic post-pass) produce stable clause coverage:
mappable requirement lines always land in SpecCard clauses; `ratification[]`
is reserved for genuinely unmappable/conflicting requirements and every entry
carries `acknowledged` + `reason`.

## Evidence (2026-10-04, spec-driven-delivery campaign, home `/Volumes/docker-ssd/ps/sdd-10x-ork/home`)

- `run-sdd10x-202610040245`: 35 cards, ratification flagged only 4 specs
  (real gaps — fixed spec-side).
- `run-sdd10x-202610040328`: same specs (34 after revision), same prompt —
  ratification flagged ALL 34, with entries like
  'snake_case everywhere.', 'Reuse the existing QR renderer; do not add a new
  QR library.', 'The badge renders for authenticated owners.' dumped as
  unacknowledged-uncovered instead of mapped to
  `clauses.constitutional`/`clauses.functional`. Same input, opposite
  outcome ⇒ prompt-level nondeterminism, gate-breaking.

## Feature scope

- Prompt hardening in `contract-compiler.md`:
  - Explicit rule: every imperative/constraint line from the source spec MUST
    be mapped to exactly one clause bucket (functional / quality /
    constitutional / architectural) — repo-convention lines (naming, library
    reuse, auth visibility) are `constitutional`.
  - `ratification[]` may ONLY contain requirements the compiler cannot map or
    believes conflict; each entry REQUIRES `acknowledged: true|false` plus a
    one-sentence `reason`. An unacknowledged entry must state the conflict.
  - Few-shot: one worked example mapping a constraint-heavy spec (use the
    fixture `tests/fixtures/sdd_e2e/specs/multiply-feature.md` shape, not a
    domain example).
- Deterministic backstop in `verifiers/ratification-check.py` (behavior
  addition, covered by tests): before failing, attempt exact/normalized
  substring matching of each unacknowledged ratification entry against the
  card's clause texts; an entry whose text IS covered by a clause is treated
  as compiler noise, logged `reclassified_covered`, and does not fail the
  gate. Entries with no clause match still fail as today.
- Tests: extend `tests/test_sdd_verifiers.py` with the noise-reclassification
  case (entry text duplicated in a clause → pass) and the genuine-gap case
  (no match → fail), keeping the existing drift/acknowledged tests green.

## Definition of Done (probes)

```bash
# P1: verifier tests incl. new cases
python3 -m pytest -q tests/test_sdd_verifiers.py

# P2: e2e dry-run still green
python3 -m pytest -q tests/test_sdd_e2e_dryrun.py
```

## Hard rules

- NEVER run the repo-wide full pytest suite as a probe.
- The backstop must not auto-acknowledge genuinely uncovered requirements —
  only exact/normalized text-coverage reclassification.
- Do not weaken source_hash drift detection or the acknowledged-flag contract.

## Success command

```bash
python3 -m pytest -q tests/test_sdd_verifiers.py tests/test_sdd_e2e_dryrun.py
```

## Verification command

- `python3 -m pytest -q tests/test_sdd_verifiers.py tests/test_sdd_e2e_dryrun.py`
