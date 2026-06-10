# Handoff: mini-ork v0.3-rc1 — Phase E LIVE harness authored, awaiting first dispatch

## Session Metadata
- Created: 2026-06-07 12:28:20
- Project: `~/ps/mini-ork/` (the SourceShift/mini-ork OSS framework)
- Working dir during session: `/Volumes/docker-ssd/Migration/Development/researcher` (the host application repo, but all work landed in mini-ork repo)
- Branch: `main`
- Local vs origin: **2 commits ahead of origin/main** (un-pushed at handoff time) + 1 untracked dir (`tests/live/`)
- Session duration: multi-day autopilot ralph (2026-06-05 through 2026-06-07)

### Recent Commits (last 8 on main)
```
2ee559b docs(audits): close 20260605-unit-test-deferred — all 3 items resolved
29a8aa9 feat(gate_bootstrap+gates/liveness): wire W2-C circuit breaker into central oracle-gate bootstrap
6c66dca feat(execute+gate_bootstrap): Wave 1 central wire-up — 4 oracle gates auto-fire pre-publish
868f06e fix+test(ralph-100pct): close last 7 e2e fails + 1 sec DoS gap → 503/0 across 54 test files
90f037e feat(gates): Phase N+O recipe-level wire-up — 4 oracle gate shims + operator guide
b10bc84 fix(pattern_store): align INSERT/UPDATE with real schema (drop cluster_id, use strftime for first_seen/last_seen)
c411b08 fix(promotion_gate)+test(unit): close 9 unit-test failures via schema-correct UPDATE + seed expansion
c6d735a test(e2e+integration): seed schema + fix schema-drift in 6 test files (ralph: -44 failures)
```

Earlier-in-session commits (this autopilot pass, oldest first):
`cb59b08 615d899 33ba189 94d3cfe f7890a7 3dc65ca 7ce45bb 2c12d9d b6d6788 ae54a3d 4e0fc1b d0aa8f4 d7fde07 1ee3603 ae9b6cb` → 21 of mine total, plus 2 concurrent (`29a8aa9` + `2ee559b`).

## Handoff Chain

- **Continues from**: none (this is the first handoff for this autopilot arc)
- **Supersedes**: none

## Current State Summary

Long-running ralph autopilot session covering 3 distinct arcs:
1. **2026-06-05**: Oracle hardening v0.3 Waves 1+2 (5 lib primitives) + dispatch-path fix (Path A) + recipes/docs/ + README audit + drift-detection 3-layer hooks.
2. **2026-06-06** (concurrent session): W2-C behavioral circuit breaker + oracle-liveness gate added to bootstrap (5 gates not 4).
3. **2026-06-07 (today)**: ralph "2 then 3" — closed last 7 e2e test fails + 1 security DoS gap (→ 505/0 suite green) THEN ran 3-subagent consensus (3/3 unanimous path-b) and shipped Wave 1 central-dispatcher wire-up in `bin/mini-ork-execute` publisher case-branch.

**Where things left off**: The Phase E LIVE validation harness (`tests/live/phase_e_live_validation.sh`) is AUTHORED + syntax-checked + UNTRACKED in working tree. It has NOT been executed yet. Running it is the next concrete step — costs ~$0.10-0.50 against opus, ~2 min wall, produces `docs/_meta/phase-e-live-validation-<ts>.md` artifact that converts Phase E from 🟡 (lib+test green only) to ✅ (live-validated end-to-end).

## Codebase Understanding

### Architecture Overview

mini-ork is a 6-stage universal task loop (`classify → plan → execute → verify → reflect → improve`) with framework primitives in `lib/`, dispatcher entrypoints in `bin/`, opinion-bearing pipeline shapes in `recipes/`, and oracle-hardening gates in `gates/` (NEW this session). The framework is heterogeneous-multi-agent by configuration: lens nodes route through distinct vendor families (glm/kimi/codex/minimax/opus) so pairwise output correlation ρ stays below Rajan-2025's submodularity ceiling.

**Phase tracker (A–O, per memory `feedback_mini_ork_phase_tracker_in_updates_2026_05_31`)**:
| Phase | Definition | Status at handoff |
|---|---|---|
| A | Dogfood convergence + self-publishing | ✅ v0.2 |
| B | Substrate + rich content | ✅ v0.2 |
| C | Measurable improvement | ✅ v0.2 scaffold |
| D | Scale-ready primitives | ✅ v0.2 |
| **E** | Evolution + promotion (improve→benchmark→eval→promote) | ✅ **lib+e2e green** (12/0 promotion_gate, 16/0 version_registry_rollback, 13/0 full_self_improvement_cycle) but **🟡 LIVE-run still pending** — harness authored at `tests/live/phase_e_live_validation.sh`, NOT YET EXECUTED |
| F | OSS publish | ✅ v0.2 |
| G | Positioning lock-in | ✅ v0.2 |
| L | Recursive self-audit | ✅ 2026-06-04 |
| N | Promotion-class taxonomy enforced | ✅ central wire-up (auto-fires pre-publish via `gates/synthesis-promote.sh`) |
| O | Panel-failure detection | ✅ central wire-up (3 diagnostics + liveness CB auto-fire pre-publish) |

### Critical Files

| File | Purpose | Relevance |
|------|---------|-----------|
| `tests/live/phase_e_live_validation.sh` | **AUTHORED + UNTRACKED** — Phase E LIVE harness. Run this next. | NEXT-STEP-#1 |
| `bin/mini-ork-execute` | 828-LOC central dispatcher. Lines 530-617 contain the publisher case-branch with new oracle-gates hook at line ~530-590 inserted by `6c66dca`. | Wave 1 wire-up landed here |
| `lib/gate_bootstrap.sh` | NEW — idempotent auto-registration of the **5** oracle gates (coalition, panel-health, synthesis-promote, stability, liveness). Concurrent session at `29a8aa9` added the 5th (liveness). | Phase N+O central wire-up |
| `gates/{coalition,panel-health,stability,synthesis-promote,liveness}.sh` | Thin shims adapting lib functions to the gate-registry custom-condition contract (rc=0/1/2 + JSON on stdout) | Recipe + central wire-up surface |
| `lib/{coalition_gate,cw_por,promotion_gate,adaptive_stability,circuit_breaker}.sh` | 5 oracle-hardening primitives. All have inline self-test fixtures (4 each, 25 total green). | Wave 1 + W2-B/C deliverables |
| `tests/run-all.sh` | Test pyramid runner. Layers: smoke → unit → integration → e2e → security. **505 OK / 0 FAIL across 55 files at handoff.** | Regression baseline |
| `tests/integration/test_oracle_gates_auto_wire.sh` | Pins the wire-up contract. Fixture 1 (collision panel → safety_violation=true) + Fixture 2 (diverse + varying verdicts → pass) green. Fixtures 3+4 deferred placeholders. | Wire-up regression net |
| `tests/lib/setup_state_db.sh` | `test_apply_migrations` helper. **All e2e + unit tests that touch state.db now use this.** Forgetting it cascades 11-44 fails per test file (root cause of pre-ralph 20-fail state). | Test-bootstrap pattern |
| `scripts/readme-drift-{claim-check,gatekeeper,panel,providers-doctor}.sh` + `.githooks/pre-push` | 3-layer drift detection. L1 mechanical fires every push (<1s); L2a minimax gatekeeper fires on push-to-main with structural diff; L2b 4-lens panel fires only when gatekeeper says PANEL_NEEDED. Caught real drift mid-push at `6c66dca`. | Drift prevention infra |
| `docs/architecture/oracle-gates-wiring.md` | Operator guide for recipe-level gate opt-in pattern. | Wire-up reference |
| `docs/operator/{drift-detection-hooks,provider-triage}.md` | Drift-detection setup + provider-key-rotation playbook. | Operator runbooks |
| `ROADMAP.md` | v0.3.0-rc1 in flight (lines 74-) | Status source-of-truth |

### Key Patterns Discovered

1. **Schema-seed cascade**: e2e tests creating isolated `mktemp` state.db must call `source tests/lib/setup_state_db.sh && test_apply_migrations` BEFORE any lib function. Forgetting it cascades into 11-44 silent failures because libs' inline `_ensure_table` helpers create FAKE schemas that no-op against real migrations.

2. **`claude --print` stdin gotcha**: `claude --print "$prompt"` reads stdin AND positional argv. If stdin is unredirected the CLI waits 3s and emits a warning. Always use `< /dev/null` in non-interactive scripts.

3. **`gate_run_all` task_class_filter NULL semantics**: registering with literal `'*'` matches ONLY context.task_class=`'*'`. To get framework-wide enforcement, register with empty string then `UPDATE ... SET task_class_filter=NULL` (the SQL clause `task_class_filter IS NULL OR task_class_filter=?` treats NULL as wildcard).

4. **Greedy regex eats trailing `<z-insight>` blocks**: `re.search(r'(\{.*\})', raw, re.DOTALL)` matches from first `{` to LAST `}` — mashing the JSON verdict + trailing telemetry blocks together as unparseable mega-string. Use `json.JSONDecoder().raw_decode()` which extracts the first complete JSON object and ignores trailing content.

5. **ρ measures verdict agreement, NOT family**: 4 distinct-family lenses with IDENTICAL reviewer_verdict still trips ρ=1.0 → COALITION_ABORT. The coalition gate is honest: "4 voices agreeing perfectly IS a coalition signal even when families differ." For "passing diverse panel" tests, fixtures must vary the verdict strings.

6. **Pre-prod posture (per project CLAUDE.md)**: aggressive cleanup, aggressive cutover, no shadow-write parallel runs. Solo-founder env has no real users; legacy code lives for hours-to-days not weeks-to-months.

## Work Completed

### Tasks Finished

- [x] Wave 1 oracle-hardening (W1-A docs + W1-B coalition_gate + W1-C cw_por + W1-D promotion_gate::mo_promote_synthesis_gate)
- [x] Wave 2-B adaptive_stability + W2-C circuit_breaker (concurrent session)
- [x] Dispatch-path Path A fix (`bin/mini-ork run <recipe>` honors explicit arg)
- [x] `recipes/docs/` task class with grep + link verifiers + live-validated docs recipe
- [x] README claims audit (10 defects closed) + 3-layer drift-detection pre-push hook
- [x] Phase N + O central wire-up (5 gates auto-fire pre-publish in `bin/mini-ork-execute`)
- [x] Test suite: 363/20 → **505/0** across 55 files (-20 failures net, +1 integration test, +1 security DoS guard)
- [x] 3-subagent consensus pass on wire-up architecture (Security + Reliability + Maintainability UNANIMOUS on path b)
- [x] DoS guard in `bin/mini-ork-classify` (`MO_MAX_KICKOFF_BYTES` default 1MB)
- [x] Phase E LIVE harness authored at `tests/live/phase_e_live_validation.sh` (syntax-clean, untracked)

### Files Modified (THIS session, my 21 commits)

| File | Changes | Rationale |
|------|---------|-----------|
| `lib/{coalition_gate,cw_por,adaptive_stability,gate_bootstrap}.sh` | NEW | Oracle-hardening primitives + central wire-up |
| `lib/{promotion_gate,pattern_store,trace_store,gradient_extractor}.sh` | Schema-correct UPDATE/INSERT + prompt reframe | D-048 + multiple schema-drift fixes |
| `bin/mini-ork-execute` | +64 LOC pre-publish oracle-gates hook | Wave 1 central wire-up |
| `bin/mini-ork-classify` | +13 LOC DoS guard + `--task-class` flag | Path A + security |
| `bin/mini-ork` | +20 LOC recipe→task_class derivation | Path A |
| `gates/{coalition,panel-health,stability,synthesis-promote}.sh` | NEW | Recipe + central opt-in surface |
| `recipes/docs/{workflow,task_class,artifact_contract}.yaml + prompts/ + verifiers/` | NEW | docs recipe with grep+link verifiers |
| `scripts/{readme-claim-check,readme-drift-gatekeeper,readme-drift-panel,readme-drift-providers-doctor}.sh` | NEW | 3-layer drift detection |
| `.githooks/pre-push` + `Makefile` | NEW | Drift hooks wiring |
| `tests/e2e/test_e2e_{trace_lifecycle,benchmark_run,promotion_gate,reflection_pipeline,full_self_improvement_cycle}.sh` | Schema-seed + column rename fixes | Test pyramid 363→505 |
| `tests/integration/{test_d008_workflow_node_dag,test_oracle_gates_auto_wire}.sh` | Path A regression + wire-up contract | Test net |
| `tests/unit/test_{promotion_gate,benchmark_suite}.sh` | workflow_candidates + WHERE column fixes | Unit-level coverage |
| `docs/operator/{drift-detection-hooks,provider-triage}.md` + `docs/architecture/oracle-gates-wiring.md` + `docs/audits/20260605-readme-claims-audit.md` + `docs/fixes/20260604-dispatch-classifier-overrides-explicit-recipe.md` | NEW operator + architecture + audit docs | Reference material |
| `kickoffs/oracle-hardening-v03.md` + `kickoffs/oracle-w1-{a,b,c,d}-*.md` | NEW kickoff epic + 4 sub-epics | Authored Wave 1 plan |
| `README.md` + `ROADMAP.md` | Refreshed counts + v0.3-rc1 section + Wave 1 wire-up status | Public-facing accuracy |
| `examples/00-demo.sh` | §5 graceful empty-table output | Demo accuracy fix |
| `tests/live/phase_e_live_validation.sh` | **NEW, UNTRACKED** | Phase E LIVE next-step |

### Decisions Made

| Decision | Options Considered | Rationale |
|----------|-------------------|-----------|
| Wave 1 central wire-up — path (b) single chokepoint | (a) per-node-type case extension / (b) single post-lens pre-publish hook / (c) lib-side auto-registration via env flag | 3/3 subagent consensus. (a) couples to node-type taxonomy + extension fragility; (c) collapses on missing env-var; (b) mirrors proven measure_topology pattern, single integration point, gate_run_all task_class_filter NULL gives wildcard for free. |
| Schema-seed fix for e2e tests | Add ensure-table to libs / Apply migrations in tests | Tests should reflect real production schema (migrations are source of truth). Adding setup helper to test fixtures = surgical + matches existing pattern that unit tests already used. |
| Light-touch recipe opt-in BEFORE central wire-up | All-or-nothing wire-up first / Both surfaces | Ship light-touch (recipes/ gate shims) first to prove the contract; THEN do central wire-up after 3-subagent consensus. Recipes can adopt at their own pace; central wire-up is the framework default. |
| Phase E LIVE = $0.10-0.50, NOT $5-15 | Original $5-15 estimate from prior session | Provider triage showed only opus reliably responds in this env. 2 small benchmark tasks × 1 opus call = ~$0.10. Real corpus + full Wave benchmark is later. |

## Pending Work

## Immediate Next Steps

1. **Run `tests/live/phase_e_live_validation.sh`** — converts Phase E from 🟡 to ✅. Costs ~$0.10-0.50 against opus (the only working provider in this env per `docs/operator/provider-triage.md`). Wall ~2 min. Produces `docs/_meta/phase-e-live-validation-<ts>.md` artifact + adds 1 row each to `benchmark_results` and `promotion_records`. This is the LAST remaining quantified gap in the Phase A–O tracker.

2. **Commit + push** the Phase E artifacts:
   - `tests/live/phase_e_live_validation.sh` (currently untracked)
   - The generated `docs/_meta/phase-e-live-validation-<ts>.md` artifact
   - The 2 commits already ahead of origin/main (concurrent session work: `29a8aa9` + `2ee559b`)
   - Pre-push will fire — the drift detector may catch a new lib count if liveness/circuit_breaker counts moved
   - Update ROADMAP.md to flip Phase E from 🟡 to ✅ with run timestamp

3. **Close the 2 deferred integration test fixtures** in `tests/integration/test_oracle_gates_auto_wire.sh` (Fixture 3 = backward-compat with `MO_ORACLE_GATES_AUTO=0`; Fixture 4 = single-node code-fix fail-open). ~30 min. Brings the test from 2-OK-2-SKIP to 4-OK-0-SKIP, fully pinning the wire-up contract.

### Blockers / Open Questions

- [ ] Provider availability — kimi/glm/minimax/codex all silent-fail or timeout in current env (per `docs/operator/provider-triage.md`). Only opus + sonnet reliable. Phase E LIVE uses opus exclusively as a workaround. Real provider triage (rotate keys / update gateway URLs) is operator-action, ~30-60 min, separate from this autopilot scope.
- [ ] Wave 3 (mechanical citation+coverage verifier) — 2-3 week sub-decomposition, intentionally out-of-session scope.
- [ ] Krippendorff α calibration gate (Nasser 2026) + adversarial fabricated-bug injection (Agarwal 2026) — positioning-doc honest-gaps list, ~4-6 hr each.

### Deferred Items

- Wave 2-A held-out anchor corpus per synthesis recipe (Wang 2026) — judgment-heavy, hand-author per recipe.
- Web dashboard (separate repo, read-only over state.db) — 3-5 days.
- Live multi-lens panel dispatch via canonical `bin/mini-ork run refactor-audit` against real corpus — $5-15 / 5-15 min wall. Phase E LIVE uses a 2-task minimum to prove the chain; the full refactor-audit panel run is a separate validation.

## Context for Resuming Agent

## Important Context

**The Phase E LIVE harness is the single biggest deliverable still un-executed.** It's already written + syntax-checked at `tests/live/phase_e_live_validation.sh`. The shape:

1. Creates isolated tmp state.db, applies migrations
2. Seeds workflow_memory + workflow_candidate + 2 benchmark_tasks (arithmetic + vowel-count, deterministic-output)
3. Defines a real runner that invokes `claude --print` via `lib/providers/cl_opus.sh` and parses model output → JSON {passed, utility_score}
4. Dispatches `benchmark_run "$CAND_ID"` — fires the runner against each task with the LIVE provider
5. Asserts `benchmark_results` table received 2 rows, summary.passed >= 1, avg_utility_score in [0,1]
6. Calls `promotion_evaluate "$CAND_ID"` — gets a real promotion decision
7. Asserts `promotion_records` row landed with valid decision
8. Emits a markdown completion report to `docs/_meta/phase-e-live-validation-<ts>.md` (the artifact that proves Phase E is live-validated)

To run: `bash tests/live/phase_e_live_validation.sh`. Requires `lib/providers/cl_opus.sh` + an OPUS-compatible Anthropic API key in `.mini-ork/config/secrets.local.sh` OR `~/.config/mini-ork/secrets.local.sh`. The script auto-discovers the secrets path and fails-open-with-SKIP if none found.

**The 5-gate auto-wire IS LIVE on `main`.** Every recipe dispatch through `bin/mini-ork run` now fires the 5 oracle gates pre-publish. Existing recipes (code-fix, bdd-first-delivery, etc.) work unchanged because all gates fail-open (rc=2 defer) on missing context. Escape hatch: `MO_ORACLE_GATES_AUTO=0`.

**Local main is 2 ahead of origin/main.** Those 2 unpushed commits are `29a8aa9` (W2-C liveness wire-up) + `2ee559b` (audit close) — both concurrent-session work I didn't author. They've passed tests on the local side; push when ready (drift hook will fire).

### Assumptions Made

- Phase E LIVE harness will run successfully on first dispatch IF opus is responsive. The arithmetic + vowel-count tasks are well within opus's capability; expect both passed=true.
- Concurrent session's `29a8aa9` + `2ee559b` are safe to push (both visible in `git log`, tests still green per local `tests/run-all.sh` 505/0 baseline).
- Drift detector's L1 mechanical check is current as of `6c66dca` (40 lib files). If concurrent session's `29a8aa9` added a new lib file, L1 will catch any mismatched README count on push.

### Potential Gotchas

1. **`tests/live/` is gitignored by default? CHECK before commit.** If `.gitignore` excludes `tests/live/` the harness needs an `!tests/live/` rule or moving to `tests/integration/live_*.sh`.
2. **Drift detector blocks pushes on README count mismatch.** If pushing the Phase E harness adds nothing to `lib/`, the count stays at 40 and L1 passes. If a new lib was added by concurrent session, README needs a bump (count it via `git ls-files 'lib/*.sh' | grep -E '^lib/[^/]+\.sh$' | wc -l`).
3. **`MO_ORACLE_GATES_AUTO` default = 1** means every recipe dispatch now hits the new pre-publish hook. Recipes that don't seed a panel will see all gates defer (rc=2) → safety_violation=false → publish proceeds. Watch for any recipe that has its own custom-named publisher node-type — the hook only fires inside the canonical `publisher)` case-branch at `bin/mini-ork-execute:530`.
4. **Test pyramid uses `tests/lib/setup_state_db.sh`.** Any new test that creates an isolated state.db MUST call `test_apply_migrations` before any lib write. Otherwise: 11-44 silent failures. This pattern is canonical now; deviation is the bug.
5. **`gate_run_all` with task_class_filter NULL** wildcard pattern works only because `lib/gate_bootstrap.sh` post-processes empty-string → NULL via direct UPDATE. The `gate_register` API has no built-in wildcard support — this is bootstrap-side translation.
6. **Coalition gate ρ ≥ 0.25 is the Rajan 2025 ceiling, not 0.5.** Initial intuition might assume 0.5 = "highly correlated"; the framework treats 0.25 as the upper bound of submodularity validity.

## Environment State

### Tools/Services Used

- `claude --print --output-format text` via `cl_<provider>.sh` wrappers (sourceable Anthropic-compatible) OR `cl_codex.sh` (executable)
- `python3` (standard library only + `pyyaml` for config) — no venv, no installed deps beyond system
- `sqlite3` for state.db (WAL mode, busy_timeout=5000)
- `jq` for JSON parsing in shell pipelines
- `git` for self-publishing under `mini-ork@local` identity + drift-prevention pre-push hook

### Active Processes

- None at handoff. No long-running background processes from this session.
- The pre-push hook is INSTALLED (`git config core.hooksPath = .githooks`) — fires on every push automatically.

### Environment Variables (names only, never values)

- `MINIMAX_API_KEY`, `GLM_API_KEY`, `KIMI_API_KEY`, `DEEPSEEK_API_KEY` — provider keys in `.mini-ork/config/secrets.local.sh` (or `.agentflow/config/secrets.local.sh` per the the host application project's pattern)
- `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` — set inside cl_*.sh subshells per provider
- `MO_ORACLE_GATES_AUTO` — default 1 (auto-fire); set to 0 to bypass
- `MO_MAX_KICKOFF_BYTES` — default 1048576 (DoS guard, classify-side)
- `MO_README_DRIFT_SKIP`, `MO_README_PANEL_SKIP`, `MO_README_PANEL_INDETERMINATE` — drift-detection escape hatches
- `MINI_ORK_ROOT`, `MINI_ORK_HOME`, `MINI_ORK_DB`, `MINI_ORK_RUN_ID`, `MINI_ORK_RECIPE`, `MINI_ORK_WORKFLOW` — framework runtime contract
- `PHASE_E_PROVIDER` (default opus), `PHASE_E_BUDGET_USD` (default 2.00) — Phase E LIVE harness knobs

## Related Resources

- ROADMAP.md `v0.3.0-rc1` section — current release lineage with per-sub-epic commit map
- `docs/architecture/oracle-gates-wiring.md` — gate-shim contract + recipe opt-in guide
- `docs/operator/drift-detection-hooks.md` + `docs/operator/provider-triage.md` — operator runbooks
- `docs/audits/20260605-readme-claims-audit.md` — methodology for future drift checks
- `docs/fixes/20260604-dispatch-classifier-overrides-explicit-recipe.md` — Path A fix-spec
- `kickoffs/oracle-hardening-v03.md` — 3-wave epic master + per-sub-epic status
- Memory: `feedback_mini_ork_phase_tracker_in_updates_2026_05_31` — standing directive to include Phase A–E tracker in every mini-ork status update
- Memory: `upstream-mini-ork-self-audit-5family-panel-2026-06-04` — proof of recursive-self-audit capability (Phase L)
- Public repo: https://github.com/SourceShift/mini-ork (Apache 2.0)

---

**Security Reminder**: Before finalizing, run `validate_handoff.py` to check for accidental secret exposure.
