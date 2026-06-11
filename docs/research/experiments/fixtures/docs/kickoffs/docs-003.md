# Docs Fixture 003: Cost Telemetry Phrase

## Goal

Edit only `docs/research/experiments/fixtures/docs/work/doc-task-003.md`.

## Scope

- In scope: `docs/research/experiments/fixtures/docs/work/doc-task-003.md`
- Out of scope: every other file.

## Required Change

Add one short sentence containing this exact phrase:

`Cost telemetry: each LLM call records tokens, provider, and run id.`

## Grep Assertions

- `Cost telemetry: each LLM call records tokens, provider, and run id.`

## Link Expectations

- Do not add links.

## Done When

- The target file contains the exact required phrase.
- No other file is changed.

## Verify

Run:

```bash
bash recipes/docs/verifiers/grep-assert.sh
grep -qF "Cost telemetry: each LLM call records tokens, provider, and run id." docs/research/experiments/fixtures/docs/work/doc-task-003.md
```
