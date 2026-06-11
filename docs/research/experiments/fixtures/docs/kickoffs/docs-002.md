# Docs Fixture 002: Verifier Gate Phrase

## Goal

Edit only `docs/research/experiments/fixtures/docs/work/doc-task-002.md`.

## Scope

- In scope: `docs/research/experiments/fixtures/docs/work/doc-task-002.md`
- Out of scope: every other file.

## Required Change

Add one short sentence containing this exact phrase:

`Verifier gate: publisher nodes wait for deterministic pass signals.`

## Grep Assertions

- `Verifier gate: publisher nodes wait for deterministic pass signals.`

## Link Expectations

- Do not add links.

## Done When

- The target file contains the exact required phrase.
- No other file is changed.

## Verify

Run:

```bash
bash recipes/docs/verifiers/grep-assert.sh
grep -qF "Verifier gate: publisher nodes wait for deterministic pass signals." docs/research/experiments/fixtures/docs/work/doc-task-002.md
```
