# mini-ork Examples

Three runnable examples, ordered by complexity. Start with `01-hello-world`
to verify your install, then progress to the multi-agent patterns.

---

## Index

| # | Name | Description | Expected Cost | Expected Runtime | Features |
|---|---|---|---|---|---|
| 01 | [hello-world](./01-hello-world/) | Add a CHANGELOG entry under `[Unreleased]` | ~$0.004 | < 60 s | single epic, minimal kickoff, fast install check |
| 02 | [bug-hunt](./02-bug-hunt/) | Find + fix all empty `catch {}` blocks in `src/` | ~$0.40–0.55 | ~8–12 min | 3 parallel GLM hunters, NDJSON dedup, regression tests |
| 03 | [refactor-pipeline](./03-refactor-pipeline/) | Extract shared helpers via ARCH → MODULE → ATOM pipeline | ~$0.15–0.50 | ~14 min | 3-stage pipeline, consensus gate, parallel Sonnet workers |

---

## Choosing an Example

**Verify install only** → `01-hello-world`. Needs any git repo with a
`CHANGELOG.md`. Single LLM call, exits in under a minute.

**Multi-agent fan-out** → `02-bug-hunt`. Shows how mini-ork runs hunters in
parallel, merges their NDJSON, then chains a fix worker. Any TypeScript
project with `src/` works.

**Large refactor** → `03-refactor-pipeline`. Shows the ARCH→MODULE→ATOM
three-stage pipeline with an explicit consensus gate. Best for files with
100+ lines that mix unrelated concerns.

---

## Common Pattern

Every example follows the same flow:

```
kickoff.md  →  mini-ork deliver  →  state.db verdict
```

1. Copy the example `kickoff.md` into your project root.
2. Edit paths / model preferences to match your repo.
3. Run `mini-ork deliver kickoff.md`.
4. Inspect the result with `sqlite3 .mini-ork/state.db`.

All examples are designed to be safe to run against a real project. They
only touch the files listed in the kickoff `## Scope` section.

---

## Cost Model

| Layer | Model | Cost range |
|---|---|---|
| Decomposer / ARCH planner | `claude-opus-4` | $0.03–0.15 per epic |
| Implementation worker | `claude-sonnet-4-5` | $0.01–0.08 per epic |
| Hunter (scan only) | `glm-4` | $0.005–0.02 per epic |
| Reviewer (adversarial) | `claude-opus-4` | $0.02–0.08 per review |

A typical delivery of 3–6 epics runs **$0.10–$2.00 total**. Use
`mini-ork inspect <epic-id>` to see per-epic model costs after a run.

---

## Adding Your Own Example

1. Create `examples/NN-<slug>/`.
2. Write `kickoff.md`, `expected-output.md`, `README.md`.
3. Add a row to this file's index table.
4. Run `bash tests/smoke.sh` to verify the file structure is intact.
