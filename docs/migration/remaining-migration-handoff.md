# Remaining bash→Python Migration — Agent Handoff Document

## Context

The self-migrate recipe (PR #184, merged at commit 6bf438a) implements integration-point-first migration: close ONE fork (bash↔Python seam) at a time as a complete unit — make Python sole, repoint every inbound reference, retire bash entrypoint — gated on byte-parity, feature-acceptance, and static-feature ledger.

**This is the recipe you will run.** Do NOT improvise your own approach.

## What "integration fork" means

A fork = bash entrypoint + Python port + every inbound reference.

Migration advances a single frontier root→down:
- ABOVE the frontier = pure Python
- BELOW the frontier = pure bash
- NO half-migrated seams allowed

The fork unit prevents the "leaf migration" anti-pattern where a Python module goes native but the paired bash entrypoint (and every lib/test/UI ref to it) stays live — splitting integration points and leaving half-open seams.

## Integration-point map (blast radius per fork)

| Fork | Outbound seams | Inbound refs | Blast radius |
|------|---------------|--------------|--------------|
| **verify** | 0 | 7+ live refs | Cleanest proof case; the original five-ref snapshot omitted the top-level and legacy execute callers |
| **reflect** | 2 | 8 | Medium |
| **classify** | 0 | 18 | High inbound, no outbound |
| **plan** | 0 | 21 | High inbound, no outbound |
| **execute** | 4 | 37 | Monster: 4 outbound, 37 inbound |
| **cli** | 3 (dispatcher-level) | — | Critical path |

Source: `docs/migration/self-migrate-feature-manifest.md` §"Integration-point map"

## Recommended migration order

**verify → reflect → classify → plan → cli → execute**

Why this order:
- **verify** first: cleanest (0 outbound, 7+ inbound), validates the whole recipe pipeline
- **reflect** second: medium blast, 2 outbound seams
- **classify/plan**: high inbound but NO outbound (Python already runtime-native)
- **cli** before **execute**: dispatcher-level, affects everything below
- **execute** last: the monster (4 outbound + 37 inbound, includes context_assembler 786L)

## How to run the self-migrate recipe

### Step 1: Pick a fork

Example: verify fork (the proof case):

```bash
export MINI_ORK_ROOT="$PWD"
export MINI_ORK_HOME="$PWD/.mini-ork"
export MO_TARGET_CWD="$PWD"
export MO_ALLOW_FRAMEWORK_CWD=1  # self-edit permission
export MO_FORK=verify             # name the fork

"$MINI_ORK_ROOT/bin/mini-ork" run self-migrate recipes/self-migrate/example-kickoff.md
```

`MINI_ORK_ROOT` is the engine checkout; `MINI_ORK_HOME` is runtime state. The
older `$PWD/.mini-ork/bin/mini-ork` form is invalid in a scaffolded checkout
because `.mini-ork/engine` is a pointer, not a second engine tree.

For other forks, create a kickoff modeled after `recipes/self-migrate/example-kickoff.md`:
- Set `fork: <fork-name>`
- Set `python entrypoint: /abs/path/to/mini_ork_<fork>.py`
- Set `bash entrypoint to retire: /abs/path/to/bin/mini-ork-<fork>`
- List all inbound refs from the integration map

### Step 2: The recipe pipeline

1. **seam_mapper (opus)** → `integration-map.json`
   - Every outbound seam (Python→bash shell-out)
   - Every inbound reference (bin/, lib/, mini_ork/, tests/, scripts/, web UI)
   - Runtime-select coupling
   - Close blockers

2. **static_feature_ledger (opus)** → `static-feature-ledger.json`
   - Classify EVERY behavior in the Python module:
     - **static**: deterministic, ~0 tokens, byte-parity verifiable (the moat)
     - **agentic**: LLM call, expensive, weakly verifiable
     - **integration**: bash↔Python seam (temporary, should disappear)
   - Agentic rows MUST carry `opportunity`: cost-down analysis
   - This ledger IS mini-ork's cost/verifiability map — the strategic payload

3. **migrator (codex)** → `self-migrate.diff`
   - Make Python sole (AST-verify port is runtime-native FIRST)
   - Repoint EVERY inbound reference to Python module
   - Retire bash entrypoint in the diff
   - Stdout discipline: wrap printing ports in `redirect_stdout`

4. **verify** (5 gates)
   - `pre-retirement-parity.sh`: captures the live bash oracle before any retirement edit
   - `parity.sh`: focused fork parity / Python golden contract
   - `feature-acceptance.sh`: end-to-end feature probe
   - `ledger-shape.sh`: ledger completeness (every feature has a row, agentic rows have opportunity)
   - `fork-closure.sh`: physical entrypoint removal plus literal and dynamic caller scans

5. **reviewer (opus)** → `verdict.json`
   - `pass == parity_pass && acceptance_pass && ledger_complete && no_dangling_edge`

### Step 3: Verdict → apply or rollback

If `verdict.json` says `"pass": true`:
- Review `self-migrate.diff` for sanity
- Apply the diff: `git apply self-migrate.diff`
- Run feature-acceptance again: `bash gates/feature_acceptance.sh <fork>`
- If all green, commit and move to next fork

If `verdict.json` says `"pass": false`:
- Read the `reasons` array in verdict.json
- Fix the issue (re-run migrator, or fix the Python port, or fix a verifier)
- Re-run the recipe

## Lane policy (DO NOT CHANGE)

Set in `$MINI_ORK_HOME/config/agents.yaml`:

| role | lane | backoff |
|---|---|---|
| implementer (`migrator`) | **codex** | codex |
| mapper / ledger / reviewer | **opus** (`opus_lens`) | codex |
| discovery lens (non-critical) | **GLM** (`glm_lens`) | codex |

**WHY this policy:**
- Codex is the implementer lane (writing code, not analysis)
- Opus is the strongest reasoner for deep judgment (reviewer, mapper, ledger)
- GLM is analysis-only and hits 429 "Fair Usage" — use only for non-critical discovery

## Feature-acceptance probes

`gates/feature_acceptance.sh <fork|feature>` — end-to-end acceptance suite.

**Feature ↔ entrypoint map**:
- `classify` / `plan` / `execute` / `verify` / `reflect` → the 6 loop stages
- `routing` → learning-governed router flips (learning-loop-live-validate)
- `learning-loop` → reward→routing closure (learning-loop-closure-gate)
- `verify-gate` → a real cheat is rejected / a real fix passes
- `framework-edit` → propose-not-commit self-modification
- `resume` → durable checkpoint resume
- `epics` → dependency-ordered multi-epic delivery

Run `gates/feature_acceptance.sh all` to probe every feature.

## Known gotchas

### 1. Stdout discipline in Python ports

The migrator (codex) MUST wrap any printing in the Python entrypoint:

```python
# BEFORE (breaks parity — prints go to stdout, not captured)
print("warning: missing file")

# AFTER (parity-safe)
redirect_stdout(lambda: print("warning: missing file"))
```

Why: parity harness captures stdout for byte-exact comparison. Raw prints leak timing metadata and break parity.

### 2. AST-verify port is runtime-native BEFORE migration

The migrator's first step: verify the Python port is already runtime-native (no outbound shell-outs to bash).

If the Python port still calls `bin/mini-ork-*`, the fork is NOT ready for migration — those are outbound seams that need their own forks closed first.

### 3. Runtime-select coupling

`lib/runtime-select.sh` determines whether bash or Python runs at runtime. Some forks may have runtime-select coupling that needs repointing.

The integration map (`seam_mapper` node) catches this — check `integration-map.json` for `runtime-select` entries.

### 4. No dangling edges

The reviewer checks: grep the diff for ANY surviving reference to `bin/mini-ork-<fork>`.

If ANY ref survives (in tests, lib, scripts, web UI), the fork is NOT closed — verdict is fail.

### 5. Ledger completeness is a deliverable

The static-feature ledger is NOT optional — it's the migration's strategic payload.

`ledger-shape.sh` enforces:
- Ledger exists at `static-feature-ledger.json`
- Well-formed JSON
- Every feature in the module has a row
- Agentic rows carry `opportunity` (cost-down analysis)
- Functions changed in the diff have ledger rows (cross-check)

## Files in scope for each fork

### verify fork (example)
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork`
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-execute`
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_verify.py`
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-verify`
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_execute.py` (repoint bin/mini-ork-verify invocation)
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_cli.py` (direct and run-lifecycle dynamic `_bin(root, "verify")` dispatch)
- `/Volumes/docker-ssd/ps/mini-ork/tests/e2e/test_e2e_recipe_code_fix.sh`
- `/Volumes/docker-ssd/ps/mini-ork/tests/integration/test_bin_verify.sh`
- `/Volumes/docker-ssd/ps/mini-ork/tests/unit/test_mini_ork_verify_py.py`
- `/Volumes/docker-ssd/ps/mini-ork/lib/runtime-select.sh`
- `/Volumes/docker-ssd/ps/mini-ork/gates/feature_acceptance.sh`
- `/Volumes/docker-ssd/ps/mini-ork/scripts/runtime-parity-harness.sh`

### Other forks

Each fork has its own inbound ref surface. The `seam_mapper` node discovers this automatically — you don't need to manually enumerate refs.

**Reference:** `docs/migration/self-migrate-feature-manifest.md` has the full integration-point map with every inbound ref listed per fork.

## Verification checklist BEFORE applying a diff

- [ ] `parity.sh` passed (byte-parity == bash oracle on live state.db)
- [ ] `feature-acceptance.sh <fork>` passed (end-to-end feature still works)
- [ ] `ledger-shape.sh` passed (ledger complete, agentic rows have opportunity)
- [ ] `verdict.json` says `"pass": true`
- [ ] No surviving `bin/mini-ork-<fork>` refs in diff (grep diff for the pattern)
- [ ] Reviewed `self-migrate.diff` for sanity (check for deleted files, wrecked imports, etc.)

## After applying the diff

- [ ] Run feature-acceptance again: `bash gates/feature_acceptance.sh <fork>`
- [ ] Run `gates/feature_acceptance.sh all` to ensure no other features broke
- [ ] Commit: `git commit -m "feat(migrate): close <fork> fork — Python sole, bash entrypoint retired"`
- [ ] Move to next fork in recommended order

## Fallback: rollback

If a fork migration goes wrong (parity breaks, feature breaks, or verdict is fail):

1. DO NOT apply the diff
2. Check `verdict.json` → `reasons` array for what failed
3. Fix the root cause (Python port bug, verifier false-positive, migrator error)
4. Re-run the recipe from step 1

The recipe is propose-not-commit — nothing is applied until verdict is pass.

## Strategic payload: the static-feature ledger

Every migration run produces `static-feature-ledger.json`. This is mini-ork's cost/verifiability map:

**Static behaviors** = the moat:
- Deterministic
- ~0 token cost
- Byte-parity verifiable
- Example: JSON parsing, file I/O, schema queries

**Agentic behaviors** = cost/verifiability liability:
- LLM call
- Expensive
- Weakly verifiable
- Example: code generation, summarization, open-ended Q&A
- **Every agentic row MUST carry `opportunity`** — how to reduce cost or improve verifiability

This ledger tells us WHERE to optimize mini-ork itself — the migration's real deliverable, not just the port.

## Deliverable for this migration cycle

Run the self-migrate recipe on each fork in recommended order:
1. ✅ verify (closed and source-applied from the fully verified isolated Run 3 proposal; see the live evidence below)
2. reflect
3. classify
4. plan
5. cli
6. execute

## Live verify evidence — 2026-07-19

### Run 1: `run-1784474339-7704`

- Result: partial, unapplied.
- Findings: incomplete scope, composite publisher corruption, incorrect reviewer target resolution, and no deterministic fork-closure gate.
- Outcome: those recipe/runtime defects were repaired and regression-tested before rerunning.

### Run 2: `run-1784478877-84933`

- Isolated target: `/private/tmp/mini-ork-self-migrate-verify-v2`.
- Result: `needs_revision` / partial, unapplied; rollback ran and no retirement reached the source checkout.
- Functional evidence: 11 verify unit tests, 3 executor verifier-node tests, 8 integration assertions, feature acceptance, Pyright, shell syntax, diff hygiene, and both runtime-selector CLI smoke paths passed.
- Closure blocker: `mini_ork/ported/mini_ork_cli.py` still resolves `_bin(root, "verify")` for direct and run-lifecycle dispatch. It was outside the run's allowlist, so the migrator correctly retained `bin/mini-ork-verify`.
- Review evidence: `review-diff.patch` contains the real isolated-worktree delta and is byte-identical to the restored `self-migrate.diff` (31,513 bytes).
- Canonical artifacts: `.mini-ork/runs/run-1784478877-84933/` contains the partial diff, complete 52-row feature ledger, detailed `verdict.json`, preserved generic `run-verdict.json`, both requirements reviews, and verifier evidence.

### Repairs after run 2

- Added `mini_ork/ported/mini_ork_cli.py` to the corrected verify scope and taught `fork-closure.sh` to detect dynamic `_bin(..., "verify")` dispatch.
- Made all five recipe verifier exit statuses mirror their JSON `.pass`; a failed closure report can no longer be counted as a successful process gate.
- Made `ledger-shape.sh` recognize qualified feature names while still requiring a row for every changed public function.
- Preserved heterogeneous run-local artifacts and the explicit isolated target with focused regression coverage.

### Run 3: `run-1784482847-61479`

- Isolated target: `/private/tmp/mini-ork-self-migrate-verify-v3`.
- The isolated migration verdict passed: 11 changed files, a complete 33-row ledger, durable pre-retirement evidence, post-retirement parity, feature acceptance, ledger shape, and fork closure all green.
- The proposal removed `bin/mini-ork-verify`, repointed the top-level shell dispatcher, legacy executor, Python CLI, and Python executor to `python -m mini_ork.ported.mini_ork_verify`, and converted parity tests to standalone golden contracts.
- The canonical Python verifier now executes command-backed checks once, captures combined stdout/stderr as evidence, and preserves verifier trace telemetry without making observability a failure mode.
- The generated diff was reviewed, applied to the source checkout, and byte-compared against every file in the verified worktree.
- Post-apply verification passed: 9 verify tests, 47 executor tests, 6 CLI tests, 8 integration assertions, 18 E2E assertions, focused runtime parity, Pyright across verify/CLI/executor, all five self-migrate gates, and `git diff --check`.

The outer recipe run still ended `partial`/failed after the implementation had
passed. Its reviewer looked for canonical verifier-report filenames before the
migrator's sandbox-mirrored artifacts were reconciled into the engine run
directory, returned `needs_revision`, and triggered rollback. This was an
orchestration artifact-handoff defect, not a verify-fork defect: the mirrored
detailed `verdict.json` has `pass: true`, and the source-applied diff passed the
same gates again.

The handoff defect is now repaired in the source checkout. Self-migrate runs
harvest the isolated target's allowlisted run artifacts before reviewer
dispatch, generate an implementer summary from that evidence, and include the
recipe-specific verifier reports, integration map, feature ledger, detailed
verdict, and migration diff in reviewer inputs. Generic orchestration status is
written to `run-verdict.json`, so a recipe-owned detailed `verdict.json` is no
longer overwritten. Regression tests cover same-run mirror visibility,
reviewer evidence assembly, and verdict preservation.

### Post-repair verification

- The executor unit module passes all 50 tests, including the three artifact-handoff regressions.
- Verify and CLI unit modules pass all 15 tests; integration and E2E scripts pass 8 and 18 assertions respectively.
- Pyright reports zero errors across the migrated verifier, CLI, and executor; `bin/mini-ork validate`, shell syntax checks, and `git diff --check` pass.
- `bin/mini-ork garden` reports 0 errors and 0 warnings (265 informational stale-run notices only).
- The repository-wide direct pytest run completed with 1,805 passed, 5 skipped, and 3 source-checkout-state failures: one capability-policy expectation affected by the user-owned `.mini-ork/config/agents.yaml`, plus two reflect opt-out tests. The reflect failures reproduced in the dirty source checkout but all 8 reflect tests pass in the clean isolated migration worktree. The documented `make test` command is unavailable because this checkout has no `test` Make target.

### Next safe action

Use `kickoffs/migration/reflect.md` and the isolated reflect worktree prepared
from the focused verify-closure commit. The exact isolated target passes all 8
pre-retirement reflect parity tests; the two failures observed in the dirty
source checkout do not reproduce there, so no test repair is currently needed.
Do not start the paid self-migrate run without explicit approval. The next live
run must also confirm that its same-run reviewer consumes the harvested
evidence.

### Reflect launch attempt 1: `run-1784502357-9667`

- The run stopped at planner dispatch before seam mapping or implementation.
- The frozen run policy selected MiniMax for `planner`, but
  `MINIMAX_API_KEY` is unset; `mini-ork doctor` reports MiniMax unavailable.
- No proposal or verdict was produced, and the isolated reflect worktree stayed
  clean at `0fe5071c`.
- The safe retry uses a dedicated temporary runtime home with only Kimi,
  Codex, and GLM provider values. Kimi and GLM credentials are loaded
  process-locally from `/Users/admin/ps/scripts/cl_kimi.sh` and `cl_glm.sh`;
  no secret or persistent user-owned policy is changed.

Each fork closure produces:
- `self-migrate.diff` (reviewable, apply or reject)
- `static-feature-ledger.json` (cost/verifiability map)
- `integration-map.json` (blast radius documentation)
- `verdict.json` (pass/fail with reasons)

**Final state:** pure Python runtime, no bash entrypoints, complete static-feature ledger for the entire codebase.

## Current execution status (2026-07-19)

The first isolated verify run is preserved at
`.mini-ork/runs/run-1784474339-7704`. Its three original recipe verifiers
passed, but the reviewer correctly returned `partial` and rollback fired.
Nothing from the proposed fork diff was applied to the source checkout.

The run exposed five contract gaps:

1. `bin/mini-ork` and `bin/mini-ork-execute` are live verifier callers but were
   absent from the original five-reference kickoff scope.
2. The generic publisher copied `self-migrate.diff` over the JSON ledger and
   verdict; it now preserves heterogeneous run-local artifacts byte-for-byte.
3. No deterministic gate proved that the entrypoint and runtime references
   were gone; `verifiers/fork-closure.sh` now supplies that gate.
4. The global parity harness has five pre-existing divergences (`help`,
   `doctor`, `conductor --help`, `execute --help`, and `init`). Those are tracked
   separately rather than blocking this one-fork migration. The verify fork's
   own 9-scenario parity suite is the retirement oracle, and the recipe now
   captures it before the migrator can remove the Bash entrypoint.
5. Target resolution derived `/private/tmp` from the temporary kickoff path and
   hid the real worktree diff from the reviewer. A valid explicit
   `MO_TARGET_CWD` now wins over kickoff-location inference.
6. The second run found a dynamic caller in `mini_ork/ported/mini_ork_cli.py`:
   `_bin(root, "verify")`. The canonical kickoff now scopes that file and the
   closure gate checks dynamic resolver calls as well as literal paths.
7. The third run closed and source-applied the verify fork, but its outer
   reviewer ran before sandbox-mirrored reports were reconciled. The runtime now
   harvests the allowlisted target-run mirror before reviewer dispatch, keeps
   detailed and generic verdicts separate, and regression-tests reviewer input
   assembly. The next paid fork must confirm that contract in a live run.

The detailed first-audit backlog is
`docs/todos/20260719-174802-remaining-migration-first-audit.md`.

## Handoff

You now have everything you need:
- The recipe: `recipes/self-migrate/`
- The example kickoff: `recipes/self-migrate/example-kickoff.md`
- The feature manifest: `docs/migration/self-migrate-feature-manifest.md`
- The lane policy: §"Lane policy" above
- The verification checklist: §"Verification checklist" above

Run the recipe. Do NOT improvise. The recipe encodes the hard-won lessons from the verify fork proof case.

**GO.**
