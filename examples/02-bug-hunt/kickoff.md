# Kickoff: Find and Fix Empty catch{} Blocks in src/

## Problem

The codebase has accumulated empty `catch {}` blocks that silently swallow
errors. These are dangerous: failures become invisible, debugging becomes
guesswork, and production incidents go unreported. All empty catch blocks
in `src/` must be found, evaluated, and replaced with meaningful error
handling.

## Definition of Done

Every `catch` block under `src/` that previously had no body (or only a
comment) now either:
1. re-throws the error, OR
2. logs it via the project's logger (`logger.warn` / `logger.error`), OR
3. has an inline comment explaining why silencing is intentional (rare, must
   be justified).

A regression test exists that asserts each changed catch block is exercised
and the error is not silently dropped.

## Scope

- Read access: entire `src/` tree for discovery.
- Write access: any `.ts` / `.tsx` / `.js` file under `src/` that contains
  an empty catch block.
- New test files may be added under `src/__tests__/` or co-located `*.test.ts`.
- No changes outside `src/` (no package.json, no config files, no docs).

## Success Criteria

- `grep -rn "catch\s*{[[:space:]]*}" src/` returns 0 matches after the fix.
- All new/modified test files pass (`npm test` or equivalent).
- No existing passing test is broken.
- Each changed catch site has a comment or log call that makes the failure
  observable.

## Agents

Use 3 hunter agents scanning in parallel (one per directory shard if `src/`
has sub-directories, otherwise by file batch):

- **Hunter-A**: `src/components/` and `src/pages/`
- **Hunter-B**: `src/services/` and `src/hooks/`
- **Hunter-C**: `src/utils/`, `src/lib/`, and remaining

Each hunter emits NDJSON findings. A deduplication pass merges results.
A single worker agent applies all fixes and writes regression tests.

## Model Preference

- Hunters: `glm-4` (fast, cheap for grep + pattern scan)
- Dedup + fix worker: `claude-sonnet-4-5`
- Reviewer: `claude-opus-4` (adversarial review of error-handling changes)

## Notes

The 3-hunter pattern here demonstrates mini-ork's parallel-scan capability.
Real projects often have hundreds of catch sites; the fan-out + dedup model
keeps total cost under $0.50 while covering the full tree.
