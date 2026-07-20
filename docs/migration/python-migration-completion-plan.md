# Python migration — completion plan (the honest remaining map)

_Written 2026-07-18 after a full audit and updated 2026-07-20 after the
execute-fork closure. Ports were built ~Jul 3–4; the remaining work is
**un-shelling the rest of the engine**, not creating ports._

## State

- **Ports exist** for nearly every `lib/*.sh` (`mini_ork/ported/*.py`), merged to main weeks ago.
- **Done (PR #179):** 16 leaf libs retired, BYO providers (`config/providers.yaml` read by the
  Python core), and the **routing brain is native** — `learning_governed_lane` calls
  `decision_service.decide()` in-process (byte-parity verified, EPSILON=0).
- **Top-level migration cycle complete:** verify, reflect, classify, plan, CLI,
  and execute are Python-owned. `mini_ork/ported/mini_ork_execute.py` is the
  sole executor, the CLI routes to it in-process, and `bin/mini-ork-execute` is
  retired. Provider, git, and executable verifier subprocesses remain
  intentional external boundaries.
- **Still not done globally:** other runtime modules and libraries retain Bash
  seams and must be migrated fork-by-fork before the whole engine is pure
  Python.

`flip/runtime-default-python` is **stale** (last commit Jul-10, 43 behind main) — abandon it;
build from main.

## The remaining libs, by category (this is the whole job)

### 1. Native ports → just rewire the caller (cheap, but verify parity first)
The port is already native; the engine just calls the bash instead. Rewire = replace the
`subprocess` with an `import`, **after** proving byte-parity deterministically.

| lib | port native? | engine shell-out site | note |
|---|---|---|---|
| `decision_service` | ✅ | execute.py | **DONE** (PR #179) |
| `llm-dispatch` | ✅ (native on execute, invoke-prompt, profile_answerer.py, pre_push_review.py, and comparative-opinions) | Bash review/profile libraries, reflection/gradient and fixtures | Four Python callers plus the comparative research script are rewired; migrate remaining callers one at a time with real-provider evidence |
| `gate_bootstrap` | ✅ | non-execute callers | Execute now uses native bootstrap/registry behavior; retire the Bash lib only after every other caller moves |
| `gradient_extractor` | partial | reflection_pipeline.py | LLM-backed `gradient_extract` intentionally not ported (shells to claude) — needs porting that too |

### 2. Wrapper ports → native-ize first (real porting), then rewire
The "port" still shells to its own bash. Reimplement the shelled functions in Python.

| lib | shell-outs in port | lines |
|---|---|---|
| `gate_registry` | execute seam closed; other callers remain | 450 |
| `lane-helpers` | execute seam closed; other callers remain | 332 |
| `config_resolve` | 1 | 94 |
| `rho_aggregator` | 1 | 179 |

### 3. No port → port from scratch
| lib | lines |
|---|---|
| `context_assembler` | 786 |
| `trace_store` | — |
| `lane_router` | — |
| `intervention_gate` | native execute extension point added; no shipped Bash hook existed |

### 4. Retirement blockers (even after the port is native)
A lib can only be `git rm`'d when **all** of these are clear — check by basename across the tree:
- no runtime shell-out from `mini_ork/` (grep `subprocess|bash -c|source` + `X.sh`, incl. `$LIB/X.sh` variable paths)
- no `source`/`. `-include in any `tests/**/*.sh` (bash integration tests source libs via `$LIB/X.sh`)
- not pinned by `benchmark/tasks/*` (~31 libs are gold/parity fixtures there)
- no `.py` parity test `shutil.copy`ing the bash as a fixture

Coupling hubs to break first (each unblocks a group):
- `tests/integration/test_meta_orchestrator_loop.sh` → epic_graph, topology, role_evolver (Python port `test_meta_orchestrator_loop_py.py` already written this session)
- `tests/integration/test_gate_grounded_rejection.sh` → the 5 gates + gates_common (Python port already written)
- `tests/integration/test_autonomous_epic_pipeline.sh` → epic_graph, context_assembler
- the e2e suites → promotion_gate, version_registry, reflection_pipeline, trace_store, benchmark_suite

## Execution pattern (proven this session)

Per subsystem, in this order — **never a blind sweep** (a wrong port breaks every dispatch and the parity tests are being removed):
1. **Verify byte-parity deterministically** — native fn vs bash fn, exploration/randomness disabled, over representative inputs. Only proceed at 0 mismatches.
2. **Rewire the caller** — replace `subprocess.run(["bash","-c","source X.sh; fn …"])` with `from mini_ork.ported import X; X.fn(…)`.
3. **Verify** — pytest (parity + affected) + `pyright: 0 errors` + a **real run** (`MINI_ORK_DRY_RUN=0`) confirming the flow, not just dry-run for LLM-boundary changes.
4. **Retire** — once §4 blockers clear, port the bash tests to standalone Python, drop benchmark fixture refs, `git rm` the bash. Explicit pathspec.

### Completed caller unit: `mini-ork-invoke-prompt` — 2026-07-20

- The stable public path is retained as a thin Python launcher.
- All three BDD-first recipe callers use the utility's documented
  `MINI_ORK_PROMPT_FILE` input contract.
- `mini_ork.ported.mini_ork_invoke_prompt` calls the native dispatcher
  in-process and temporarily overlays the invocation environment so provider,
  routing, timeout, telemetry, and budget settings retain subprocess semantics.
- Its golden-contract suite does not create `lib/llm-dispatch.sh`; therefore a
  passing test proves this caller cannot silently fall back to Bash.
- The public launcher completed a real GLM 5.2 prompt with process-local
  credentials and a temporary provider registry; the focused invoke/dispatcher
  suite passed 19 tests and focused Pyright reported zero errors.
- The BDD-first dry-run E2E passed 18 assertions; validation passed and garden
  retained only the pre-existing missing operator env-var-document warning.
- The trace-store subprocess remains below the migration frontier as a separate
  ownership seam.
- This unit does **not** authorize deleting `lib/llm-dispatch.sh`; the remaining
  caller and fixture blockers above still have to close.

### Completed caller unit: `profile_answerer.py` — 2026-07-20

- The planner-injected path was already native; the standalone/default path
  now calls `mini_ork.ported.llm_dispatch` in-process too.
- Commit `00176709` is the latest provider contract: Kimi primary plus one Kimi
  retry on failure or whitespace. The retirement audit corrected a native-port
  regression that had restored the older, banned DeepSeek primary.
- Standalone golden contracts now preserve prompt bytes, validation, fence and
  balanced-object recovery, completeness checks, exact JSON persistence, and
  native Kimi retry behavior without sourcing a Bash oracle.
- The planner was already the only production inbound caller. The web smoke
  assertion now verifies native ownership and `lib/profile_answerer.sh` is
  retired, closing this fork and one more `llm-dispatch.sh` blocker.

### Completed caller unit: `pre_push_review.py` — 2026-07-20

- The sequential LLM panel calls native `mo_llm_dispatch` in-process and no
  longer checks for or sources `lib/llm-dispatch.sh`.
- Panel order, Gemini exclusion, timeout, four-turn limit, fail-open behavior,
  JSON recovery, normalization, and eight-issues-per-lens cap are preserved.
- Five focused tests and focused Pyright passed. A no-Bash fixture proves
  ownership closure for this Python caller.
- A real single-lens GLM 5.2 panel probe passed with process-local credentials
  and the temporary provider registry; no MiniMax request ran.
- `bin/mini-ork-review` plus `lib/pre_push_review.sh` remain a separate fork;
  this caller unit does not authorize their deletion or dispatcher retirement.

### Completed caller unit: `scripts/comparative-opinions.sh` — 2026-07-20

- The Bash orchestration script now invokes the native dispatcher module and
  no longer sources `lib/llm-dispatch.sh`.
- Ten-lens background execution, output/error files, status markers, manifest,
  and summary behavior remain unchanged.
- A deterministic acceptance test exercised all ten default calls with no Bash
  dispatcher library; shell syntax and focused Pyright passed.
- A real script-level probe ran two `glm_current` lenses, produced substantive
  opinions and a valid manifest, and made no MiniMax request.
- `MO_COMPARATIVE_FAMILIES`, `MO_COMPARISON_DOC`, and `MO_IMPROVEMENT_DOC`
  permit bounded/operator-selected runs; defaults retain the historical five
  families and canonical research documents.

### Completed integration fork: `mini-ork-scheduler` — 2026-07-20

- `bin/mini-ork-scheduler` is a stable direct Python launcher for the canonical
  `mini_ork.scheduler` implementation.
- The canonical owner activates the bounded concurrent epic pool controlled by
  `MO_SCHED_MAX_PARALLEL`; a CLI-main timing contract proves three independent
  epics do not silently fall back to serial execution.
- The duplicate serial `mini_ork.ported.mini_ork_scheduler`, the legacy Bash
  scheduler body, and their Bash-oracle test were retired.
- Conductor and autonomous-pipeline callers keep the public executable path.
  Generated verification hints compile the launcher as Python instead of
  applying `bash -n` to it.
- Fourteen focused contracts, 26 combined scheduler/conductor/epics caller
  tests, and the 13-assertion autonomous epic pipeline passed; Pyright reported
  zero errors, validation passed, and garden reported zero errors with the
  pre-existing missing env-var documentation warning.

### Completed caller unit: native reflection gradient boundary — 2026-07-20

- The default Python gradient extractor calls the native dispatcher in process
  with the established `gradient-extract` node, 120-second timeout, five-turn
  cap, and `MINI_ORK_GRADIENT_MODEL` (`codex` by default).
- Fenced, prose-wrapped, complete, and truncated JSON arrays retain recovery;
  missing evidence/confidence fields default to trace id and `0.5`.
- The native reflection pipeline now owns extract, store, and schema defaults;
  injection remains only as a deterministic extension/test seam.
- Twenty-nine focused tests and focused Pyright passed. A real GLM 5.2 probe
  extracted three valid gradients from a persisted failed-verifier trace; no
  MiniMax or DeepSeek request ran.
- This closes the production caller edge. The Bash gradient/reflection libraries
  and their Bash tests remain until the independent retirement fork closes.

## Tooling
- **framework-edit** must use a dedicated temporary runtime home with only
  Kimi, Codex, and GLM lanes; do not edit the user's `.mini-ork` policy and do
  not route migration work through MiniMax. Drive a subsystem with
  `MO_ALLOW_FRAMEWORK_CWD=1 MINI_ORK_PROFILE_GATE=0 bin/mini-ork run framework-edit <kickoff>`;
  **harvest from the implementer worktree**, not `review-diff.patch` (capture
  is unreliable).
- `scripts/recursive-migrate.sh` — safety-gated per-lib driver (skips runtime-shelled/bash-test-sourced/benchmark-pinned; halts on engine break). Good for the leaf-ish tail, not the core.

## Recommended order (ROI × safety)
1. `llm-dispatch` boundary — biggest lever; do one caller at a time with real-LLM parity.
2. Wrappers: `gate_registry` → then `gate_bootstrap` rewire; `lane-helpers`; `config_resolve`; `rho_aggregator`.
3. No-ports: `context_assembler`, `trace_store`, `lane_router`, `intervention_gate`.
4. Retirement cleanup: port the bash integration/e2e tests + repoint benchmark fixtures, then delete the freed bash.

Realistic size: ~24K lines of remaining bash, most load-bearing. This is a multi-week, per-subsystem
project — drive it deliberately, verify each with a real run, and it converges to zero `lib/*.sh`.
