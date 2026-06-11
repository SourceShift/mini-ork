# Docs Fixture 001: Budget Guard Phrase

## Goal

Edit only `docs/research/experiments/fixtures/docs/work/doc-task-001.md`.

## Scope

- In scope: `docs/research/experiments/fixtures/docs/work/doc-task-001.md`
- Out of scope: every other file.

## Required Change

Add one short sentence containing this exact phrase:

`Budget guard: trace-governed routing stops before the configured cap.`

## Grep Assertions

- `Budget guard: trace-governed routing stops before the configured cap.`

## Link Expectations

- Do not add links.

## Done When

- The target file contains the exact required phrase.
- No other file is changed.

## Verify

Run:

```bash
bash recipes/docs/verifiers/grep-assert.sh
grep -qF "Budget guard: trace-governed routing stops before the configured cap." docs/research/experiments/fixtures/docs/work/doc-task-001.md
```
