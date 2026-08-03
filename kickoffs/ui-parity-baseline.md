# Framework Edit: capture UI parity baseline before wholesale OpenHands fork

## Goal

Plan `quirky-giggling-lynx.md` replaces `ui/` wholesale with OpenHands'
`agent-canvas` frontend (SE-3 → SE-12). Before any destructive UI work, capture
a behavioral baseline: a Playwright spec that walks every route in the current
`ui/`, asserts load-bearing elements, and saves screenshot snapshots. Plus a
`make web-snapshot` target so SE-11 can prove the fork preserves the bits that
matter.

## Scope Hint

- `tests/ui/snapshots/` (NEW dir; PNG per route)
- `tests/ui/parity.spec.ts` (NEW; Playwright spec, walks 10 routes)
- `tests/ui/README.md` (NEW; documents which assertions are load-bearing vs cosmetic)
- `Makefile` (add `web-snapshot` target only; do not reorder existing targets)

## Expected Edit

1. **Add `tests/ui/parity.spec.ts`** — a Playwright spec that walks the 10
   routes from `ui/src/routeTree.tsx`:
   - `/` (FleetPage), `/new` (NewRunPage), `/recipes` (RecipesPage)
   - `/runs/$taskRunId` (RunDetailPage), `/runs/$taskRunId/agents/$nodeId`,
     `/runs/$taskRunId/inputs/$inputKey`
   - `/trajectory`, `/trajectory/self-improve/$runId`
   - `/fingerprint`, `/terminal`

   For each route, assert at least: page renders without console errors;
   load-bearing element visible (Fleet table has rows, Run Detail has DAG,
   Terminal has xterm container). Shell-only behaviors: ⌘K palette opens
   (`Cmd+K`), `?ws=foo` URL pin applies. No mutations (no `dispatch`,
   no `stop`).

2. **Save screenshots** to `tests/ui/snapshots/<route-slug>.png`.

3. **Write `tests/ui/README.md`** documenting which assertions are
   load-bearing (must survive the fork) vs cosmetic (expected to change).

4. **Append `web-snapshot` target to `Makefile`** — assumes `make web-up`
   is already running; runs `cd ui && npx playwright test ../tests/ui/parity.spec.ts`.

5. **Add Playwright as a dev dep** to `ui/package.json` (`@playwright/test`),
   and run `npx playwright install chromium` (one-time). Add `playwright`
   section to `ui/package.json` if not present (use `ui/playwright.config.ts`
   pointing at `../tests/ui/parity.spec.ts`).

## Requirements

- Do NOT modify any code in `ui/src/` (only add the dev dep + playwright config).
- Do NOT touch any backend route in `mini_ork/web/`.
- Do NOT add a CI workflow (that's SE-11's job).
- Parity test must NOT mutate state — pure observation. If a route requires a
  real run to render meaningfully, document it as "skipped-when-empty" rather
  than seed one.
- Match the module style of existing `tests/unit/` files (type hints, docstrings,
  pytest-style names where applicable — except this is Playwright JS, so
  TypeScript with `import type` per the `ui/` style).
- Anti-pattern: do NOT use visual-diff assertions (too brittle for a fork that
  will deliberately change visuals in SE-3+). Use `expect(locator).toBeVisible()`
  + element-role assertions only.

## Done When

- `cd /Volumes/docker-ssd/ps/mini-ork-worktrees/ui-parity-baseline && make web-snapshot`
  exits 0.
- All 10 route specs in `tests/ui/parity.spec.ts` pass.
- 10 PNG screenshots exist under `tests/ui/snapshots/`.
- `tests/ui/README.md` exists and enumerates the load-bearing vs cosmetic
  assertions.
- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 4, "tests_pass": true, "static_pass": true, "pass": true }`.
- `bash scripts/learning-loop-closure-gate.sh` (or equivalent) still exits 0 if it
  exists; otherwise `make lint` and `make test` are green.

## Why this kickoff exists

The fork-recovery plan in `quirky-giggling-lynx.md` (section E) requires the
fork to either preserve load-bearing behavior OR be abandoned. Without a
baseline, "load-bearing" is undefined. This kickoff makes it concrete.