# Audit — bash surface inventory (for the bash-removal plan)

*2026-07-26 · source: full-repo inventory agent (file:line verified).
Companion to `docs/plans/2026-07-26-bash-removal-plan.md`.*

## 1. `bin/` wrappers — mode per file

- **Pure Python launchers**: `mini-ork`, `mini-ork-invoke-prompt`,
  `mini-ork-review`, `mini-ork-scheduler`, `mini-ork-mcp-steering`.
- **Shim + exec python** (bash tail is env setup only): `mini-ork-recover`,
  `mini-ork-serve`.
- **Shim + real bash fallback** (runtime-select delegates by default):
  `mini-ork-{bug-collector,bugs,conductor,coord,epics,eval,improve,init,
  inject,lifetime,metrics,promote,resume,rollback,self-improve,spawn,
  topology,traceotter,update,usage-report,watchdog}` (21).
- **Real bash, NO shim (production-bash today)**: `mini-ork-apply`
  (+`lib/apply.sh`), `mini-ork-garden`, `mini-ork-recipe-eval`,
  `mini-ork-validate`, `mo-check-claude-invocations` (python port exists),
  `_worker-launcher.sh` (no tracked production caller).

**Trampoline**: `cli/main.py` `_bash_entrypoint_handler` routes 21
subcommands through the bash bin wrappers even in default python mode.

## 2. `lib/*.sh` (73) + `gates/*.sh` (6) — production reachability

**P — called from production Python (blockers):**
`cw_por.sh` (promotion_gate.py:370), `gate_registry.sh`
(artifact_contract.py:192), `trace_store.sh` (invoke_prompt.py:122,
reflect.py:141), `cache.sh`/`finalize.sh`/`auto-merge.sh`/`pr-create.sh`
(recovery/finalize.py:41-44,503-538), `utility_function.sh`
(benchmark_suite.py:190), `cleaner.sh`/`healer.sh`
(healer_bridge.py:257,377), `providers/cl_*.sh` ×7 (providers.py:159-536,
cl_codex.sh→pricing_strategy.sh internally), `runtime/contract.sh`
(agent/minimal.py:123), `migrate.sh` via `db/init.sh` (cli/init.py:189,
cli/update.py:291), `gates/{coalition,liveness,panel-health,stability,
synthesis-promote}.sh` (gate_registry.py:228 `_evaluate_external`, seeded
by gate_bootstrap.py) + their sourced libs (`coalition_gate.sh`,
`circuit_breaker.sh`, `adaptive_stability.sh`, `promotion_gate.sh`,
`gates_common.sh`, `topology_metrics.sh`, `benchmark_suite.sh`,
`version_registry.sh`), and 56 `recipes/*/verifiers/*.sh`
(cli/verify.py:183,194; cli/execute.py:1119).

**B — only via MINI_ORK_RUNTIME=bash fallback:** `apply.sh`,
`bug_report.sh`, `budget_config.sh`, `epic_graph.sh`, `coord_gate.sh`,
`coord_registry.sh`, `group_evolver.sh`, `operator_steering.sh`,
`cost_pause.sh`, `recursive_orchestration.sh`, `llm-dispatch.sh`,
`throttle-guard.sh`, `lane-helpers.sh`, `config_resolve.sh`,
`decision_service.sh`, `lane_router.sh`, `policy_store.sh`,
`process_reward.sh`, `db_open.sh`, `blame_attributor.sh`,
`rubric-prescreen.sh`, `pricing_strategy.sh`,
`krippendorff_alpha_gate.sh`, `honest_ci_gate.sh`,
`citation_verifier_mechanical.sh`, `mid_node_injector.sh`,
`steering_checkpoint.sh`, `checkpoint.sh`, `mo_node_events.sh`,
`mo_otel.sh`, `cn_client.sh`, `context_role_packs.sh`, `safety_events.sh`.

**T — test/doc references only:** `active_state_index.sh`,
`agent_registry.sh`, `anchor_corpus.sh`, `artifact_contract.sh`,
`auto-merge-pr.sh`, `branch-quarantine.sh`, `cross_epic_gradient.sh`,
`deadline_budget.sh`, `gate_bootstrap.sh`, `harness_wrapper.sh`,
`memory.sh`, `mo-healer-bridge.sh`, `pattern_store.sh`,
`rho_aggregator.sh`, `role_evolver.sh`, `scaffold_tier.sh`, `topology.sh`,
`verifier_rubric.sh`, `gates/feature_acceptance.sh`.

**X — fully orphaned:** none strictly; every lib has ≥ a parity test or
docstring reference.

## 3. The 22 Python→bash blocker sites

1. `cli/main.py` `_bash_entrypoint_handler` (21 subs, 4 unported)
2. `cli/init.py:181-196` → `db/init.sh`
3. `cli/update.py:291` → `db/init.sh`; :125 sqlite-grep
4. `cli/invoke_prompt.py:122-127` → `trace_store.sh`
5. `cli/reflect.py:141-161` → `trace_store.sh`
6. `cli/verify.py:183,194` → verifier `.sh` + `bash -lc`
7. `cli/execute.py:1119` → recipe verifier `.sh`
8. `gates/gate_registry.py:228-256` → 5 gate-condition scripts
9. `gates/promotion_gate.py:370-430` → `cw_por.sh`
10. `gates/artifact_contract.py:192-202` → `gate_registry.sh`
11. `recovery/finalize.py:41-596` → cache/finalize/auto-merge/pr-create
12. `recovery/healer_bridge.py:257-390` → cleaner/healer
13. `recovery/cleaner.py:101,260` → `lib/gauntlet.sh` (absent; soft dep)
14. `learning/benchmark_suite.py:190-200` → `utility_function.sh`
15. `dispatch/providers.py:159-536` → `cl_*.sh` ×7
16. `agent/minimal.py:123-128` → `runtime/contract.sh`
17. `recovery/healer.py:286-289` → 4 nonexistent libs (dead paths)
18. `cli/main.py:595-601` doctor lib-presence checks
19. `cli/main.py:161-165` kickoff-lint `.sh` regexes
20. `runtime/engine.py:333,338` `bash -lc` inside sandboxes (generic)
21. `review/lenses.py:66-69` `bash -n` on reviewed shell files (generic)
22. `scheduler.py:248`, `client.py:54-79`, `gepa/miniork_adapter.py:199`,
    `web/control.py:614`, `orchestration/conductor.py:247` → bin layer
    (python launcher, but traverses bin/)

## 4. Test surface

- 82 `.sh` test files + `tests/run-all.sh` + `tests/smoke.sh` +
  `tests/lib/setup_state_db.sh`.
- ~90 `*_py.py` parity tests shell out to bash twins (grouped mapping in
  the agent report; 17 already point at deleted twins and pass anyway —
  the parity layer is partially decoupled).
- ~40 tests pin `db/init.sh`.

## 5. CI / hooks / scripts

- `ci.yml`: shellcheck job, bash-tests matrix job, readme-claim-check,
  mo-check-claude-invocations advisory.
- `.githooks/`: pre-push, post-commit, reference-transaction (bash).
- `hooks/*.sh` ×4 (Claude Code glue, sources `lib/cn_client.sh`).
- `scripts/*.sh` ×16 (worktree helper + readme-drift are the valuable ones).
- `Makefile`: worktree/readme/serve/test-obs targets → bash.
- `db/init.sh` (→ `lib/migrate.sh`).

## 6. Config/docs coupling

`AGENTS.md` (MINI_ORK_RUNTIME, paths.sh, shellcheck, gate_registry.sh),
`config/providers.yaml` (cl_*.sh contract comments), 106 docs files with
`.sh` references, `.github/CODEOWNERS` (3 lib/*.sh), migrations with `.sh`
in comments/data.
