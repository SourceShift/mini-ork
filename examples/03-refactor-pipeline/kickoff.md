# Kickoff: Extract Shared Helpers via V2 ARCH→MODULE→ATOM Pipeline

## Problem

`src/utils/dataHelpers.ts` has grown to 800+ lines. It mixes date formatting,
string sanitization, currency conversion, and pagination utilities into a
single flat file. Other files import everything from it, making tree-shaking
impossible and tests slow (the whole file loads for each test that needs one
formatter).

Extract each logical group of helpers into its own focused module under
`src/utils/` and update all import sites.

## Definition of Done

- `src/utils/dataHelpers.ts` is removed (or reduced to a re-export barrel
  that imports from the new focused modules).
- Each extracted module has no more than 5 exported functions.
- All existing tests still pass.
- Import sites across the codebase are updated to point at the new modules.
- Each new module has at least one unit test covering its happy path.

## Scope

- Read: entire `src/` tree (import-site discovery).
- Write: `src/utils/` (new modules), `src/utils/dataHelpers.ts` (reduce or
  remove), any file in `src/` that imports from `dataHelpers`.
- No changes outside `src/`.

## Pipeline

This delivery uses the 3-stage V2 pipeline:

### Stage 1 — ARCH (Architecture)

An Opus agent reads `src/utils/dataHelpers.ts` and proposes a module split:

- List candidate modules with names, exported symbols, and rationale.
- Identify all import sites via `grep -rn "from.*dataHelpers"`.
- Output a structured JSON `arch_plan.json` into `.mini-ork/runs/<run_id>/`.

**Consensus gate**: A second Opus agent reviews the plan and must agree
(score ≥ 0.8) before Stage 2 starts. If disagreement, iterate (max 2 rounds).

### Stage 2 — MODULE (Implementation)

One Sonnet worker per proposed module (parallel):

- Creates the new file under `src/utils/<module-name>.ts`.
- Moves the relevant functions from `dataHelpers.ts`.
- Writes unit tests in `src/utils/__tests__/<module-name>.test.ts`.

Each worker claims exactly one module from `arch_plan.json`.

### Stage 3 — ATOM (Cleanup + Integration)

A single Sonnet agent:

- Updates all import sites discovered by ARCH.
- Reduces `dataHelpers.ts` to a re-export barrel (or removes it).
- Runs the full test suite and applies any minor fix-ups.

## Success Criteria

- `grep -rn "from.*dataHelpers" src/ | grep -v "re-export"` returns 0 matches
  (direct imports gone — only barrel re-export remains, if kept).
- `npm test` exits 0.
- Each new module file < 150 lines.
- No function appears in more than one new module.

## Model Preference

- Stage 1 ARCH: `claude-opus-4` (structural reasoning)
- Consensus reviewer: `claude-opus-4`
- Stage 2 MODULE workers: `claude-sonnet-4-5` (parallel, implementation)
- Stage 3 ATOM: `claude-sonnet-4-5`

## Notes

The ARCH→MODULE→ATOM pipeline is mini-ork's standard pattern for large
refactors. ARCH produces a durable plan artifact; MODULE workers execute in
parallel against it; ATOM stitches everything together. This example uses
`dataHelpers.ts` as a stand-in — replace with any large utility file in your
project.
