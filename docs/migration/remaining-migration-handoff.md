# Remaining bash→Python Migration — Agent Handoff Document

## Lower-level port ownership plan

Before retiring more lower-level libraries, run the report-only inventory in
`docs/migration/ported-module-ownership-recursive-plan.md`. It closes the gap
between "a Python port exists" and "the supported runtime owns and uses that
port," and defines safe `integrate`, `retain`, `delete`, and `defer` outcomes.
The execution kickoff is
`kickoffs/migration/ported-module-ownership.md`; its first unit is the duplicated
similarity implementation in the context assembler.

The Phase 0 inventory completed on 2026-07-20 at `c7ccc512`: all 126 ported
modules were covered, no deletion was authorized, and the report-only verifier
passed 38/38 checks after Kimi synthesis and GLM 5.2 review. The first bounded
unit then moved pure ranking to `mini_ork/similarity.py` and rewired the context
assembler to its raw-score API while preserving database access, the `0.15`
threshold, top-three-per-source policy, lesson shape, raw ordering, and
missing-table behavior. Its focused suite passed 30 tests, Pyright was clean,
and GLM 5.2 required no repairs.

The next inventory-selected unit closed the first remaining `llm-dispatch`
caller: `bin/mini-ork-invoke-prompt` is now a thin Python launcher and
`mini_ork.ported.mini_ork_invoke_prompt` calls
`mini_ork.ported.llm_dispatch` in-process. Its provider boundary remains
injectable; combined stdout/stderr ordering, environment overlays,
placeholder substitution, role-pack injection, trace writes, and exit behavior
have standalone golden-contract coverage. The tests deliberately run without
`lib/llm-dispatch.sh`, proving that this caller no longer owns a Bash edge.
The three BDD-first recipe callers now pass the documented
`MINI_ORK_PROMPT_FILE` variable instead of the stale `MINI_ORK_PROMPT` name.
The focused invoke/dispatcher suite passed 19 tests, focused Pyright reported
zero errors, and the unchanged public executable completed a real GLM 5.2
prompt through a run-local provider registry. Kimi's configured model codes
were rejected by its gateway, so they were not counted as migration evidence;
no MiniMax provider was used. The BDD-first dry-run E2E passed 18 assertions,
`mini-ork validate` passed, and `mini-ork garden` reported zero errors with the
same missing operator env-var-document warning reproduced on unchanged main.
Do not retire the library yet: `profile_answerer`, pre-push review, reflection/
gradient paths, scripts, and Bash fixtures still depend on it. Refresh the
inventory from the promoted main before selecting the next caller.

The subsequent caller unit made `mini_ork.ported.profile_answerer` native on
its standalone/default path as well as its already-native planner path. The
default preserves the DeepSeek-first, Kimi-on-failure-or-whitespace contract,
captures provider stdout without feeding diagnostics into the JSON parser, and
keeps the provider boundary injectable. Its focused profile/planner suite
passed 27 tests with one optional replay fixture skipped, focused Pyright
reported zero errors, and a process-local GLM 5.2 probe passed through the
native seam. No DeepSeek or MiniMax request was made for migration validation.
`lib/profile_answerer.sh` remains below the frontier because Bash callers and
its pre-retirement oracle still exist; this unit closes only the Python
module's `llm-dispatch.sh` edge. Refresh from promoted main again before the
next caller.

The next caller unit removed `mini_ork/pre_push_review.py`'s Bash dispatcher
edge. Its sequential panel now calls native `mo_llm_dispatch` in-process while
preserving configured panel order, Gemini exclusion, per-lens timeout and
four-turn cap, fail-open behavior, prose-wrapped JSON recovery, issue limits,
and normalization. Five focused tests and focused Pyright passed, including a
fixture that proves the panel works without `lib/llm-dispatch.sh`; a real
single-lens GLM 5.2 probe returned a valid result. No MiniMax request ran.
`bin/mini-ork-review` and `lib/pre_push_review.sh` remain a separate
entrypoint/library fork below the frontier and still block dispatcher
retirement. Refresh promoted main before selecting the next caller.

The next direct caller unit rewired `scripts/comparative-opinions.sh` from
sourcing `lib/llm-dispatch.sh` to invoking
`python3 -m mini_ork.ported.llm_dispatch`. The ten-lens background fan-out,
per-lens files, failure markers, manifest, and summary remain Bash-owned. A
deterministic acceptance test exercised all ten default calls without a Bash
dispatcher library. A real script-level probe narrowed the panel to two
`glm_current` lenses, used minimal temporary input documents, produced two
substantive opinions and a valid manifest, and made no MiniMax request. The
historical five-family and canonical-document defaults remain unchanged;
environment overrides only make bounded validation and operator-selected runs
possible. Refresh promoted main before continuing to the Bash library forks.

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
| **plan** | 1 | 25+ | High inbound; native dispatcher plus profile/context/trace contracts had to close together |
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

1. **seam_mapper (GLM)** → `integration-map.json`
   - Every outbound seam (Python→bash shell-out)
   - Every inbound reference (bin/, lib/, mini_ork/, tests/, scripts/, web UI)
   - Runtime-select coupling
   - Close blockers

2. **static_feature_ledger (GLM)** → `static-feature-ledger.json`
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

5. **reviewer (GLM)** → `verdict.json`
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

## Lane policy for the remaining forks

Set in `$MINI_ORK_HOME/config/agents.yaml`:

| role | lane | backoff |
|---|---|---|
| implementer (`migrator`) | **codex** | codex |
| planner / general research | **Kimi** (`kimi_current`) | codex |
| mapper / ledger / reviewer | **GLM 5.2** (`glm_current`) | codex |

**WHY this policy:**
- Codex is the implementer lane (writing code, not analysis)
- Kimi provides planning and broad discovery through the authenticated coding API
- GLM 5.2 owns seam analysis, ledger judgment, and review
- MiniMax and Opus are not provider values for the remaining migration runs

Use a dedicated temporary `MINI_ORK_HOME` rather than editing the user's
`.mini-ork/config/agents.yaml`. Load Kimi and GLM credentials process-locally
from `/Users/admin/ps/scripts/cl_kimi.sh` and `cl_glm.sh`; never persist their
values in repository files or run artifacts. The local Kimi Claude wrapper's
hard-coded model is stale, so the reflect run used a run-local executable
adapter against the authenticated Anthropic-compatible messages endpoint.

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
2. ✅ reflect (closed and source-applied from `run-1784503045-70610`; see the live evidence below)
3. ✅ classify (closed and source-applied from `run-1784528328-42404`; see the live evidence below)
4. ✅ plan (closed by the completion audit after partial `run-1784532524-76798`; see live evidence below)
5. ✅ cli (closed, merged, and pushed; see live evidence below)
6. ✅ execute (closed by the completion audit after the one authorized provider run; see live evidence below)

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

## Live reflect evidence — 2026-07-20

### Passing proposal: `run-1784503045-70610`

- Isolated target: `/private/tmp/mini-ork-self-migrate-reflect`.
- Provider policy used only Kimi, Codex, and GLM: Kimi planned, Codex migrated,
  and GLM 5.2 mapped seams, built the authoritative ledger, and reviewed.
- The proposal deletes `bin/mini-ork-reflect` and repoints the top-level CLI,
  legacy executor, Python CLI, Python executor, GEPA wiring, and integration
  coverage to `python -m mini_ork.ported.mini_ork_reflect`.
- All five migration reports pass: durable pre-retirement parity,
  post-retirement parity, feature acceptance, the 27-row static-feature ledger,
  and deterministic fork closure. The GLM reviewer and detailed
  `verdict.json` also pass.
- The preserved proposal and isolated target diff were byte-identical
  (`sha256: 173793e43fb3d24355b4d0683101c3d9578fa536157ceb7d51ed4efaecfb8f03`)
  before promotion.
- Post-apply checks pass: 11 reflect/GEPA tests, 11 integration assertions, 8
  parity cases, reflect feature acceptance, Pyright, ledger shape, closure,
  Bash syntax, and diff hygiene.

### Executor repairs discovered by the completion audit

- Reviewer assembly now includes the standalone
  `pre-retirement-parity.json`, not only `verifier_*.json`, with focused
  regression coverage.
- The run's outer `failed_nodes=1` was traced to the generic hollow-artifact
  guard running on `pre_retirement_parity` before the migrator could create
  final artifacts. The executor now derives verifier phase from workflow order:
  baseline verifiers before the first implementer may run, while every later
  verifier remains fail-closed on missing artifacts.
- The executor/CLI suite passes all 57 tests after both repairs; Pyright reports
  zero errors on execute, CLI, and reflect.

### Historical plan preflight

The canonical `kickoffs/migration/plan.md` and clean isolated target
`/private/tmp/mini-ork-self-migrate-plan` are prepared at the focused
classify-closure commit `928db915`. Its no-cost baseline passes 9 unit tests,
17 integration assertions, and plan feature acceptance. Focused Pyright has
three documented baseline errors that the plan migration had to close. This
preflight led to `run-1784532524-76798` and the completion audit documented in
the live plan evidence below.

## Live classify evidence — 2026-07-20

### Passing proposal: `run-1784528328-42404`

- Isolated target: `/private/tmp/mini-ork-self-migrate-classify`.
- Provider policy used only Kimi, Codex, and GLM: Kimi planned, Codex migrated,
  and GLM 5.2 mapped seams, built the authoritative ledger, and reviewed.
- The proposal deletes `bin/mini-ork-classify` and repoints the top-level Bash
  dispatcher, Python CLI lifecycle, validation, integration, E2E, security,
  parity-harness, and user-facing references to
  `python -m mini_ork.ported.mini_ork_classify`.
- The Python runtime now preserves Bash trace start/success side effects through
  a best-effort native trace-store call while keeping dry-run side-effect free.
- All five migration reports pass: durable pre-retirement parity,
  post-retirement parity, feature acceptance, the 29-row static-feature ledger,
  and deterministic fork closure. The GLM reviewer and detailed
  `verdict.json` also pass.
- Independent replay passed 15 classify/CLI unit tests, 9 classify integration
  assertions, 16 post-MVP integration assertions, 43 security assertions, 53
  E2E assertions, feature acceptance, focused Pyright, all post-retirement
  migration gates, and diff hygiene.
- The outer command exited non-zero after the passing workflow because the
  generic Python verifier invokes globally registered oracle gates without
  their required `recipe`, `verdict_file`, or `current_round` context and then
  treats `defer` as failure. This was diagnosed without a paid retry and did not
  invalidate the green fork-specific evidence.

## Live plan evidence — 2026-07-20

### Partial proposal and blocker discovery: `run-1784532524-76798`

- Isolated target: `/private/tmp/mini-ork-self-migrate-plan`, baseline
  `928db915`.
- Provider policy used only Kimi, Codex, and GLM: Kimi planned, Codex migrated,
  and GLM 5.2 mapped seams, built the ledger, and reviewed. MiniMax and DeepSeek
  gateway variables were unset for the run.
- The pre-retirement Bash/Python oracle was captured green before Codex edited
  the target. Post-change parity, feature acceptance, and ledger shape also
  passed.
- Codex's second requirements audit found four Bash-owned contracts missing
  from the original integration map: profile normalization/question handling,
  planner context injection, context-pack persistence, and planner trace
  lifecycle writes. It restored `bin/mini-ork-plan`; fork closure failed; GLM
  correctly rejected the proposal. No paid retry ran.

### Completion audit and closure

- The Python planner now calls the native dispatcher in-process while preserving
  the injectable `(returncode, combined_output)` contract and merged stream
  capture.
- Native profile handling preserves zero-question normalization,
  non-interactive auto-answering, `/dev/tty` interactive answers, confidence
  updates, profile answer artifacts, and fail-closed blocking.
- Native context orchestration preserves learned failure modes, prior runs,
  the ContextNest planner role pack with generic fallback, recent-file sessions,
  active-state injection, and the auditable `context-pack.json` artifact.
- Planner running, blocked, failure, fallback, and success traces use
  `mini_ork.trace_store`. Migration `0054_execution_traces_status_blocked.sql`
  fixes the pre-existing schema contradiction that silently rejected the Bash
  planner's `blocked` status; applying it to a pre-0054 database preserved the
  existing trace and accepted a blocked trace.
- Every executable caller and test was repointed to
  `python3 -m mini_ork.ported.mini_ork_plan`; `bin/mini-ork-plan` was deleted.
- Independent post-retirement evidence is green: 39 focused planner/context
  tests, 25 CLI/context tests, 10 plan integration assertions, 7 given-plan
  assertions, the recursive profile-gate verifier, 5 path-traversal assertions,
  29 web-smoke passes (25 environment skips), focused Pyright, post-retirement
  parity, feature acceptance, the 58-row completion ledger, deterministic fork
  closure, and database-migration preservation.
- The model-authored detailed verdict remains the honest rejected partial-run
  record. Promotion is based on the later deterministic five-gate evidence plus
  two source-requirements audits; the rejected verdict was not rewritten and no
  paid reviewer replay was hidden.

### CLI closure — 2026-07-20

- The isolated `migration/cli` implementation was completed after three
  requirements audits, promoted to `main`, and pushed.
- `bin/mini-ork` remains the executable public path but is now a thin Python
  launcher. It resolves symlinks, imports `mini_ork_cli.main`, and never reads
  `MINI_ORK_RUNTIME` or sources `runtime-select.sh`.
- Direct `classify`, `plan`, `verify`, and `reflect` commands route to native
  Python modules. The missing `apply` dispatch was restored; execute was then
  closed as the final top-level fork.
- Deadline state, per-run config snapshots, repository healing, rubric
  scoring, and reward grading use native Python ports with their prior
  best-effort or return-code contracts preserved.
- The public-path inbound callers remain unchanged because the path, executable
  bit, argv, stdout/stderr, and exit-code contracts remain stable. A stale web
  line-number comment was updated.
- A third audit found that the original parity helper did not force
  `MINI_ORK_RUNTIME=bash` and could therefore compare Python with Python. The
  untouched pushed-main dispatcher was rerun in explicit Bash mode: all 40
  legacy dispatcher assertions passed, and exact version/help/doctor/error
  parity now passes against the Python launcher. The corrected evidence also
  drove native `lib/paths.sh`-equivalent environment and engine-pointer
  resolution into the launcher.
- Evidence is green: the durable pre-retirement oracle, 8 standalone CLI tests,
  46 dispatcher assertions, focused Pyright, post-retirement parity, feature
  acceptance, the 48-row completion ledger, deterministic CLI closure, diff
  hygiene, and three requirements audits.

### Execute closure — 2026-07-20

- Exactly one authorized paid run used Kimi, Codex, and GLM. Its model-authored
  review returned `needs_revision`; no paid retry was made. The completion
  audit repaired the PRM, minimal-scaffold, resolved-model routing, and plan
  task-class gaps deterministically.
- `mini_ork/ported/mini_ork_execute.py` owns the complete executor lifecycle,
  including bounded process-isolated concurrency. Direct `execute` and the
  full run lifecycle route to it in-process.
- Dispatch, capability checks, learned context, operator steering,
  intervention gating, gate bootstrap, and liveness are native. Provider,
  git, and executable verifier calls remain explicit external boundaries.
- Every executable inbound edge is repointed and `bin/mini-ork-execute` is
  deleted. Durable pre-retirement evidence replaces the old live-Bash parity
  dependency.
- Final evidence is green: 57 execute tests, 11 dispatcher tests, 88 adjacent
  native-port/CLI tests, 10 execute integration assertions, broad E2E and
  recursive integration, a non-zero duration trace, isolated observability,
  focused Pyright, the 76-row ledger, and all five self-migrate gates.
- Two requirements audits found no remaining execute-fork requirement. Secret
  and scope checks exclude credential values, local adapters, runtime state,
  logs, and the user's `.mini-ork/config/agents.yaml`.

### Migration-cycle result

All six top-level task-loop forks are closed. Future work in the completion
plan concerns lower-level Bash libraries, scheduler/utility entrypoints, and
historical fixture retirement; it is not an open execute-fork requirement.

Each fork closure produces:
- `self-migrate.diff` (reviewable, apply or reject)
- `static-feature-ledger.json` (cost/verifiability map)
- `integration-map.json` (blast radius documentation)
- `verdict.json` (pass/fail with reasons)

**Final state for this cycle:** the public task loop and all six named forks are
Python-owned. Remaining lower-level Bash units stay tracked in
`docs/migration/python-migration-completion-plan.md` rather than being folded
into this execute commit.

## Historical verify execution findings (2026-07-19)

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
   assembly. The reflect run subsequently confirmed the artifact-handoff
   contract and closed the final standalone-report omission.

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
