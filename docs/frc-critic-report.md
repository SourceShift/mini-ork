# Harsh Critic Panel — frc/roadmap implementation

**Panel:** kimi (k2) · codex (gpt-5.2) · opus (4.x). Each given the full
implementation diff + per-epic acceptance criteria, instructed to be hostile.
**Unanimous verdict: REJECT.** Raw critiques: `scratchpad/critic_{codex,kimi,opus}.md`.

The 12 epics all *committed* and passed framework-edit's own gates, but the panel
shows those gates were theater — the two headline features (side-effect credit
A4+A5) and two coordination-safety features (B4 wound-wait, B6 strict deny) are
broken in production, and several tests pass only by coincidence.

## Cross-confirmed CRITICAL (≥2 critics agree)

1. **A5 penalty fold is dead in production.** `lane_router.sh` parses the trace
   timestamp with `strptime(..., '%Y-%m-%dT%H:%M:%fZ')` — **missing `%S`** — so
   every real `…:SS.mmmZ` row raises and is `continue`-skipped; the fallback
   also fails (fraction never stripped). No penalty ever folds. (opus)
   Independently, codex: penalty is added to `adv_sum` then divided by `n`, so a
   `-0.5` penalty dilutes to `-0.05` at 10 runs — magnitude depends on sample
   count, not severity. **A5 NOT MET.**
2. **A4 blame attribution is a chain of independent breaks** (codex + opus):
   - git trailer extraction uses `--format="%(Mini-ork-run-id)"` — not a valid
     placeholder (must be `%(trailers:key=…,valueonly)`) → always empty.
   - `git log -1 … -- "$sha"` treats the SHA as a **pathspec**, not a revision → empty.
   - `_ba_group_by_run` consumes stdin in pass 1; pass 2 reads exhausted stdin → zero groups.
   - attribution rows stamped `unknown`/`medium` (env vars exported after `row` built).
   - smoke test runs `--dry-run` only → never exercises the DB write path. **A4 NOT MET.**
3. **B6 strict-deny logic is inverted** (codex + opus + kimi): strict mode
   *allows* conflicts **inside** the scope and *denies* **outside** it — backwards.
   With `COORD_GATE_SCOPE=/src`, a conflicting write to `/src/api/x` proceeds.
   The integration test passes only because it uses scope `/` and the
   `//*` glob never matches. **B6 NOT MET.**
4. **B4 wound-wait only resolves cycles when the requester is the lowest-priority
   participant** (codex + opus + kimi). A higher-priority requester completing a
   cycle falls through to a normal block → deadlock persists. kimi adds: the DFS
   `seen` set is global → misses branch cycles; `<=` tie-compare → equal-priority
   livelock. **B4 PARTIAL/NOT MET.**

## Cross-confirmed HIGH

5. **B5 priority inheritance effectively absent** (kimi: zero inheritance code in
   `coord_registry.sh`; codex: NOT MET; opus: test reimplements the pick CTE
   inline so the scheduler path is unverified). **B5 NOT MET / unverified.**
6. **`overlaps("/", …)` returns False** (opus + kimi) — a root-scoped lease
   conflicts with nothing (`"//"` prefix bug). Masks B2/B6 test passes.
7. **Tests are theater** (all three): A4 dry-run-only, B5 inline-CTE, B6 deadlock
   metric asserted by manually calling the recorder. Several "pass" via the
   coincidence of broken helpers.

## Other notable
- **0044 migration** `.read "|sh -c '…'"` only works through the sqlite3 CLI with
  `MINI_ORK_DB` in env; via any Python/library path the column is silently absent
  while the index still runs (opus). A1 fragile.
- audit JSON built by unescaped `printf` interpolation → invalid JSON on quotes (codex + kimi).
- `coord_queue_depth` undercounts pure waiters (codex).
- registry I/O errors treated as "no conflict" → fail-open in strict mode (kimi).
- root-level files stamped `code_region="."` pollute the region table (opus).

## Per-epic consensus
| Epic | Verdict |
|---|---|
| A1 | PARTIAL (fragile migration, `.` region) |
| A2 | MET (segment↔region conflation caveat) |
| A3 | MET |
| A4 | **NOT MET** |
| A5 | **NOT MET** (dead timestamp parse + dilution) |
| A6 | MET |
| B1 | MET |
| B2 | PARTIAL (root overlap bug) |
| B3 | MET |
| B4 | **PARTIAL/NOT MET** (cycle resolution) |
| B5 | **NOT MET** (no inheritance / unverified) |
| B6 | **NOT MET** (inverted strict deny) |

**Overall: REJECT.** The implementation needs a remediation pass that (a) fixes
the A4/A5 credit core, (b) corrects B4/B5/B6 coordination logic + the `/`-overlap
bug, and (c) replaces theater tests with ones that drive the real code paths.
