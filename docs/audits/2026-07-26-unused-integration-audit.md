# Audit — unused / improperly-integrated code in `mini_ork/`

*2026-07-26 · method: codegraph call-graph (zero-incoming-edge symbols) +
full-repo grep verification (bash/lib/bin/recipes/dynamic wiring), two
independent auditors cross-checked.*

## Verdicts and actions taken (integrate-or-remove rule)

| Finding | Verdict | Action |
|---|---|---|
| `gepa/mock_validate.py`, `gepa/run_gepa_codex.py` | Dead (sibling `run_gepa.py` is live) | **Removed** (`97001f3b`) |
| `llm_dispatch.glm_backoff_seconds` | Dead, superseded by throttle_guard | **Removed** |
| `lane_router.log_propensity` | Dead duplicate — propensity stamping belongs to `decision_service` and is deliberately unwired until the router owns it (columns exist, migration 0049) | **Removed**; roadmap debt noted below |
| `lane_router.z_score_advantage` | Dead read-only convenience; column path lives in `learning/advantage_store.py` | **Removed** |
| `checkpoints.sha256_bytes`, `web/artifacts.list_run_dirs`, `web/auth.auth_configured`, `runtime/engine.docker_available` | Dead | **Removed** |
| `bin/mini-ork-conductor` sourcing missing `lib/budget_config.sh` | Real break (bash runtime aborted) | **Integrated**: recreated `lib/budget_config.sh` (`mo_daily_budget_cap`) per its documented contract (`9c6e7998`) |
| 72 live-module env flags read-but-undocumented | Features exist but unreachable by operators | **Integrated**: cataloged with defaults in `docs/operator/feature-flags.md` |
| 51 test-only parity ports | Migration debt at audit time; the final Bash cutover converted their remaining assertions to native unit tests | **Resolved**: `docs/migration/parity-ports.md` is now a retired-counterpart registry |

## Verified clean

- All 9 recipe node types have handlers (no silent `(0,'done')` fall-through).
- All 8 gate types have evaluators (no silent `defer`).
- `SUBCOMMAND_REGISTRY` ↔ `bin/` wrappers consistent.
- 123 `MO_*`/`MINI_ORK_*` env vars properly wired (set + documented).

## Remaining debt (roadmap, not cleanup)

- **Propensity stamping** (`route_source/route_explore/route_score`,
  migration 0049): required for unbiased off-policy bandit evaluation
  (roadmap Step 4). Owner should be `mini_ork/steering/decision_service.py`
  — the bash twin (`decision_service_log_propensity`) is only exercised by
  `tests/unit/test_router_followup.sh`.
- **Env flags in test-only modules** (9): die with their parity-port module
  on cutover (see `docs/migration/parity-ports.md`).
- `MO_REFLECTION_BATCH` is set internally by the run loop AND read as an
  operator knob — document the precedence when the loop is refactored.
