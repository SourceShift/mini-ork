# Example 02 — Bug Hunt: Fix Empty catch{} Blocks

Multi-hunter scan pattern: 3 parallel GLM agents comb the source tree for
empty `catch {}` blocks, emit NDJSON findings, a dedup pass merges them,
then a single Sonnet worker applies all fixes plus regression tests.

## Prerequisites

| Requirement | Check |
|---|---|
| mini-ork installed | `mini-ork version` |
| Project with TypeScript source in `src/` | adapt paths in kickoff if different |
| Logger utility at `src/utils/logger.ts` (or equivalent) | adjust kickoff if using `console.error` |
| `npm test` (or equivalent) working | required for BDD gate |

## Command

```bash
cp ~/ps/mini-ork/examples/02-bug-hunt/kickoff.md ./kickoff.md
# Optional: edit kickoff.md to match your actual src/ structure
mini-ork deliver kickoff.md
```

## Expected Cost and Runtime

| Metric | Value |
|---|---|
| Wall-clock time | ~8–12 min (hunters parallel, fix sequential) |
| Total cost | ~$0.40–0.55 |
| Hunter cost | ~$0.02–0.05 each (GLM, cheap + fast) |
| Fix worker | ~$0.03–0.12 (Sonnet, depends on # of sites) |
| Opus reviewer | ~$0.05–0.15 (one pass over diff) |

## Features Demonstrated

- **Fan-out hunters** (3 GLM agents scanning in parallel)
- **NDJSON dedup** (cross-hunter duplicate removal)
- **Dependency gate** (fix epic waits for all 3 hunters to complete)
- **Self-correction** (worker applies reviewer feedback in one extra pass)
- **Regression tests** (each changed site gets a test)
- **BDD gate** with `grep` + `npm test` assertions

## Adapting for Your Project

### Different source directory

Edit `kickoff.md`:
```
## Scope
- Read access: entire `lib/` tree for discovery.
- Write access: any `.py` file under `lib/` ...
```

And update the three hunter scope lines in `## Agents`.

### Different logger

Replace `logger.warn` / `logger.error` references in the kickoff with your
actual logger import path or `console.error`.

### Fewer hunters

For small repos, reduce to 1 hunter. Remove Hunter-B and Hunter-C from the
kickoff `## Agents` section.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Hunter emits 0 findings | No empty catch blocks found | That's fine — BDD PASS, nothing to fix |
| Fix worker breaks existing test | Rethrow changed call contract | Review diff; add `@ts-expect-error` or update test expectation |
| BDD FAIL: grep still matches | Worker missed a site | Check `dedup.ndjson` in `.mini-ork/runs/` — site may be in a string literal |
| Cost >> $0.55 | Many catch sites + large diff | Use `--max-lanes 2` to limit parallelism |
