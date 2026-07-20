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
| `llm-dispatch` | ✅ (native on execute) | profile_answerer.py, cli.py and other callers | Execute is rewired; migrate remaining callers one at a time with real-provider evidence |
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

## Tooling
- **framework-edit** works now (kimi lane was dead → repointed to minimax in `.mini-ork/config/agents.yaml`; verify gate confirmed intact via #66/#174). Drive a subsystem: `MO_ALLOW_FRAMEWORK_CWD=1 MINI_ORK_PROFILE_GATE=0 bin/mini-ork run framework-edit <kickoff>`; **harvest from the implementer worktree**, not `review-diff.patch` (capture is unreliable).
- `scripts/recursive-migrate.sh` — safety-gated per-lib driver (skips runtime-shelled/bash-test-sourced/benchmark-pinned; halts on engine break). Good for the leaf-ish tail, not the core.

## Recommended order (ROI × safety)
1. `llm-dispatch` boundary — biggest lever; do one caller at a time with real-LLM parity.
2. Wrappers: `gate_registry` → then `gate_bootstrap` rewire; `lane-helpers`; `config_resolve`; `rho_aggregator`.
3. No-ports: `context_assembler`, `trace_store`, `lane_router`, `intervention_gate`.
4. Retirement cleanup: port the bash integration/e2e tests + repoint benchmark fixtures, then delete the freed bash.

Realistic size: ~24K lines of remaining bash, most load-bearing. This is a multi-week, per-subsystem
project — drive it deliberately, verify each with a real run, and it converges to zero `lib/*.sh`.
