# Example 03 — Refactor Pipeline: ARCH → MODULE → ATOM

The flagship three-stage pipeline. An Opus architect proposes a module split,
a consensus gate validates it, parallel Sonnet workers execute each module
extraction, and a single ATOM agent stitches the import sites together.

## Prerequisites

| Requirement | Check |
|---|---|
| mini-ork installed | `mini-ork version` |
| TypeScript project with large utility file | adapt kickoff for your target file |
| `npm test` passing before the refactor | baseline must be green |
| `jq` | for arch plan parsing |

## Command

```bash
# Edit kickoff.md to point at YOUR large utility file:
cp ~/ps/mini-ork/examples/03-refactor-pipeline/kickoff.md ./kickoff.md
# Replace "src/utils/dataHelpers.ts" with your actual file path.
# Replace the module names in ## Agents with a first guess (ARCH will refine).

mini-ork deliver kickoff.md
```

## Expected Cost and Runtime

| Stage | Agents | Model | Cost | Wall-clock |
|---|---|---|---|---|
| ARCH plan | 1 | Opus | $0.04–0.10 | 1–2 min |
| ARCH consensus | 1 | Opus | $0.02–0.05 | 30 s |
| MODULE (per module) | 1 each, parallel | Sonnet | $0.01–0.03 | 2–4 min |
| ATOM integrate | 1 | Sonnet | $0.03–0.08 | 2–4 min |
| **Total** | | | **$0.15–$0.50** | **~14 min** |

Cost scales with file size and number of import sites. An 800-line file with
20 import sites runs around $0.25.

## Features Demonstrated

- **Three-stage pipeline** (ARCH → MODULE → ATOM) with stage-gate dependencies
- **Consensus gate** (two independent Opus agents must agree before proceeding)
- **Parallel MODULE workers** (one epic per extracted module, run simultaneously)
- **Durable plan artifact** (`arch_plan.json` in `.mini-ork/runs/`)
- **Resume-from-checkpoint** (if Stage 2 fails, Stage 1 artifacts survive)
- **Import-site discovery** via grep + structured plan
- **Barrel re-export** for backward compatibility

## Pipeline Architecture

```
kickoff.md
    │
    ▼
┌─────────────────────────────────┐
│  Stage 1 — ARCH                 │
│  e-001  Opus planner            │
│         → arch_plan.json        │
│  e-002  Opus consensus reviewer │
│         → score ≥ 0.8 required  │
└───────────────┬─────────────────┘
                │ plan approved
                ▼
┌─────────────────────────────────┐
│  Stage 2 — MODULE (parallel)    │
│  e-003  dateUtils    (Sonnet)   │
│  e-004  stringUtils  (Sonnet)   │──▶ each creates file + tests
│  e-005  currencyUtils(Sonnet)   │
│  e-006  pagUtils     (Sonnet)   │
└───────────────┬─────────────────┘
                │ all PASS
                ▼
┌─────────────────────────────────┐
│  Stage 3 — ATOM                 │
│  e-007  Sonnet integrate        │
│         update import sites     │
│         reduce barrel           │
│         npm test                │
└─────────────────────────────────┘
                │
                ▼
           auto-merge
```

## Adapting for Your Project

### Different source file

Change the file path in kickoff `## Problem` and `## Scope`. The ARCH agent
will propose its own module breakdown — you don't need to enumerate them
in the kickoff, the planner fills in the detail.

### Non-TypeScript project

Replace `.ts` / `.tsx` references with `.py` / `.go` / `.rs` etc. Adjust the
`npm test` command in `## Success Criteria` to your test runner.

### Tighter consensus threshold

Change `score ≥ 0.8` in the kickoff to `score ≥ 0.9` for a stricter gate.
Useful when the source file has ambiguous module boundaries.

### Skip the barrel

If you want to force all callers to update rather than leaving a re-export
barrel, add to kickoff `## Success Criteria`:
```
- `src/utils/dataHelpers.ts` is DELETED (no barrel allowed).
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Consensus score < 0.8 | ARCH agents disagree on boundaries | Add `## Domain Notes` to kickoff explaining the groupings you expect |
| MODULE worker fails scope-overlap gate | Two workers claiming same function | Review `arch_plan.json` for duplicate exports — edit plan and resume |
| ATOM breaks import in a test file | Grep missed a test import | Check `src/**/*.test.ts` imports — ATOM scope includes test files by default |
| npm test fails after ATOM | Circular import via barrel | Set `"no-cycle"` in ESLint and let ATOM self-correct, or remove barrel |
| Pipeline hangs at Stage 2 | One MODULE worker stalled | `mini-ork inspect <epic-id>` to see iter trace; `mini-ork resume <run-id>` to continue |
