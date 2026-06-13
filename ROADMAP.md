# Roadmap

This is a living document. Items move between buckets as priorities shift.
See [GOVERNANCE.md](./GOVERNANCE.md) for how decisions get made.

## Released

### v0.0.0-extract — 2026-05-30
Literal port of an internal multi-agent orchestrator into a standalone repo.
Preserved at git SHA `0ec2bf1` for diff/reference.

### v0.1.0-redesign — 2026-05-30
Architectural inversion: framework ships primitives (universal task loop, 8
node types, 6 edge types, 6 gates, 8 memory namespaces); pipeline shapes
live in `recipes/`. Two reference recipes ship: `code-fix` (minimal) and
`bdd-first-delivery` (multi-stage migration target for the literal port).

### v0.2.0 — 2026-06-01

Dogfood-converged + self-publishing + positioning-grounded. The framework
now audits itself, publishes its own synthesis to a canonical path under a
`mini-ork@local` git identity, and routes lens nodes to **4 distinct model
families** by configuration (meeting the heterogeneity precondition for
[Rajan 2025](https://arxiv.org/abs/2511.16708)'s submodularity proof).

**Phase A — Dogfood convergence + self-publishing** ✓
- 3 mini-ork@local auto-commits across DF10/11/12
- Publisher node reads `artifact_contract.outputs[]`, copies synthesis to
  canonical paths, `git commit` under framework identity (D-037)
- Conditional rollback honors `edge_type: escalates_to` (D-038)

**Phase B — Substrate + rich content** ✓ (pipeline + content; gradient
extraction prompt-tuning is D-048, deferred)
- Migration 0014 relaxes `execution_traces.run_id NOT NULL` (D-039) +
  widens status check to include `pending`
- `trace_store.sh` INSERT realigned to migration 0010's actual column
  schema (was using `prompt_version` instead of `prompt_version_hash`)
- `_trace_write_node_rich` helper populates `files_written` +
  `cost_usd` per dispatch (D-042); reflect pipeline traverses 6 stages
  clean

**Phase C — Measurable improvement** ✓ scaffold
- `mini-ork metrics --recipe <name>` emits markdown/JSON cross-cycle
  trajectory: cost trend, wall-time trend, trace density, gradient yield
- 12 dogfood cycles tracked, $24.31 cumulative cost

**Phase D — Scale-ready primitives** ✓
- WAL + busy_timeout per-connection (R1 / F-10 / F-11 / K-01)
- Concurrency cap on parallel dispatch via `MINI_ORK_MAX_PARALLEL` (R3 / F-28)
- Reflection batch LIMIT (R6 / F-15 / F-17 / F-18 / K-04)
- Cost circuit breaker `MO_DAILY_BUDGET_USD` (R10 / G-016)
- `execution_traces` indexes (R4 / F-27)
- 8 `_ensure_table` DDL session guards (G-003 ★★ consensus)
- `--output-format json` real cost extraction with `.last-llm-cost`
  sidecar (D-04 / D-29)
- 36 audit-flagged P1s closed across 30 commits

**Phase F — OSS publish** ✓
- Public at https://github.com/SourceShift/mini-ork (Apache 2.0)
- CI green: shellcheck (`--severity=error` blocks, `--severity=warning`
  advisory) + smoke test on every push

**Phase G — Positioning lock-in** ✓
- 4-distinct-family lens routing in `recipes/refactor-audit/workflow.yaml`
  (D-047): glm_lens→glm, kimi_lens→kimi, codex_lens→codex, opus_lens→opus
- `docs/positioning/why-mini-ork.md` captures 6-paper literature grounding
  ([Nasser 2026](https://arxiv.org/abs/2601.05114) + [Rajan 2025](https://arxiv.org/abs/2511.16708) + [Karanam 2025](https://arxiv.org/abs/2512.21352) + [Zietsman 2026](https://arxiv.org/abs/2603.25773) + [Shehata 2026](https://arxiv.org/abs/2604.27274) + [Song 2026](https://arxiv.org/abs/2603.21454))
- README top-of-fold positioning section + 7-axis comparison vs
  single-vendor agent SDKs (Claude Code / OpenAI Agents SDK / LangGraph)

**Phase E — Evolution + promotion** ✓ in v0.3-rc1
- LIVE-validated on 2026-06-07 with `PHASE_E_PROVIDER=codex`:
  `benchmark_run` dispatched two real model-scored tasks, persisted
  `benchmark_results`, then `promotion_evaluate` persisted the resulting
  `promotion_records` decision. Evidence:
  `docs/_meta/phase-e-live-validation-20260607-125311.md`.
- The candidate was rejected because one benchmark failed, which is the
  expected promotion-gate behavior; the validated contract is the live
  improve → benchmark → eval → promote chain and its DB writes.

### v0.3.0-rc1 — 2026-06-08 (current release candidate)

**Oracle Hardening, Wave 1 + Wave 2 partial.** Shipped as self-contained
primitives in `lib/` plus a positioning honesty patch. The central publisher
wire-up now lives behind `lib/gate_bootstrap.sh` and the publisher branch in
`bin/mini-ork-execute`; recipe-level shims remain available for explicit
opt-in and testing.

Grounded in 9-paper research brief synthesizing the self-evolution oracle
question:
[Zenil 2026](https://arxiv.org/abs/2601.05280) +
[DeVilling 2025](https://arxiv.org/abs/2510.21861) +
[Adapala 2025](https://arxiv.org/abs/2509.10509) +
[Wang 2026](https://arxiv.org/abs/2601.05184) +
[Hu et al 2025](https://arxiv.org/abs/2510.12697) +
[Bertalanič 2026](https://arxiv.org/abs/2605.00914) +
[Agarwal & Khanna 2025](https://arxiv.org/abs/2504.00374) +
[Sistla 2025](https://arxiv.org/abs/2509.26546) +
[Setlur 2025](https://arxiv.org/abs/2502.12118).

| Sub-epic | Status | Commit | Deliverable |
|---|---|---|---|
| W1-A docs/positioning honesty patch | ✅ | `615d899` | `docs/positioning/why-mini-ork.md` "Self-evolution is class-restricted" section + 2-row taxonomy table + Zenil/Setlur/DeVilling citations |
| W1-B coalition gate primitive | ✅ | `f7890a7` | `lib/coalition_gate.sh::mo_check_panel_coalition` — emits COALITION_ABORT when ρ ≥ MO_RHO_THRESHOLD (default 0.25) OR family_count < lens_count. Rajan 2025 + Bertalanič 2026 grounded |
| W1-C CW-POR diagnostic primitive | ✅ | `33ba189` | `lib/cw_por.sh::mo_compute_cw_por` — orthogonal panel-health metric to Krippendorff α (Agarwal & Khanna 2025) |
| W1-D selective-feedback conjunction | ✅ | `94d3cfe` | `lib/promotion_gate.sh::mo_promote_synthesis_gate` — synthesis-class auto-promote requires panel_score + CW-POR + structural signal ALL three (Adapala 2025) |
| W2-B adaptive stability detection | ✅ | `3dc65ca` | `lib/adaptive_stability.sh::mo_check_panel_stability` — round-over-round verdict drift drives HALT/CONTINUE between debate rounds (Hu et al 2025) |
| W2-C behavioral circuit breaker | ✅ | `fa93340` | `lib/circuit_breaker.sh::mo_check_liveness_breaker` — three orthogonal stagnation signals (artifact-hash invariance / verdict-stuck / cost-burn-without-write) with CLOSED→OPEN→HALF_OPEN state machine. Behavioral complement to v0.2 Phase D cost-CB (`MO_DAILY_BUDGET_USD`). Registered as 7th gate type `liveness_gate` in `gate_registry.sh`. Closes the failure mode where spend is under the cap but the recipe is making zero forward progress (reviewer rejecting the same patch every cycle). Ralph-equivalent of `CB_NO_PROGRESS_THRESHOLD` / `CB_SAME_ERROR_THRESHOLD` / `CB_COOLDOWN_MINUTES` (ralph-claude-code v0.11.5). Covered by `tests/unit/test_circuit_breaker.sh` (10 assertions, all green). |
| Phase E LIVE validation | ✅ | pending | `tests/live/phase_e_live_validation.sh` — on-demand live harness for improve → benchmark → eval → promote. Run `PHASE_E_PROVIDER=codex bash tests/live/phase_e_live_validation.sh`; 2026-06-07 report: `docs/_meta/phase-e-live-validation-20260607-125311.md` (8 OK / 0 FAIL). |
| W2-A held-out anchor corpus | ⏸ | — | Hand-author per recipe — judgment-heavy corpus selection (Wang 2026) |
| W3 mechanical citation+coverage verifier | ⏸ | — | 2-3 week sub-decomposition into 5-8 atoms (Sistla 2025 + Ficek 2025) |

All 6 shipped primitives include inline self-test fixtures (4 each, 24 total) that pass on first run. Run any of them directly to see the verdicts:

```
$ bash lib/cw_por.sh
$ bash lib/promotion_gate.sh
$ bash lib/coalition_gate.sh
$ bash lib/adaptive_stability.sh
$ bash lib/circuit_breaker.sh
```

Two new framework phases added by this work:

- **Phase N — Promotion-class taxonomy enforced.** The positioning doc now makes the deterministic-oracle vs LLM-judged-only split explicit. `mo_promote_synthesis_gate` is the executable form of that split.
- **Phase O — Panel-failure detection.** Three orthogonal diagnostics now exist: ρ + family-diversity (coalition_gate), CW-POR (cw_por), round-stability drift (adaptive_stability). Each fail-opens when it can't measure — no silent blocking.

Tracking epic: `kickoffs/oracle-hardening-v03.md`.

Dispatch path findings filed at `docs/fixes/20260604-dispatch-classifier-overrides-explicit-recipe.md` are now mostly closed: `bin/mini-ork run <recipe>` honors explicit recipe args and `recipes/docs/` exists. Remaining release cleanup is documentation parity: the docs recipe still needs recipe-local README/example coverage, and the schema/docs examples need to be aligned with the live workflow dialect.

## Next (v0.3 final + v0.4 — Q3-Q4 2026 target)

Wire-up + remaining oracle-hardening gaps:

- **Wave 1 wire-up** —
  - ✅ **Light-touch recipe opt-in** (2026-06-05): 4 thin gate shims
    shipped under `gates/{coalition,panel-health,stability,
    synthesis-promote}.sh`. Recipes register them via `gate_register
    custom <path> <task_class>` and list `custom` in node `gates: []`
    in workflow.yaml. Full guide at
    [`docs/architecture/oracle-gates-wiring.md`](docs/architecture/oracle-gates-wiring.md).
    Smoke-verified: coalition shim against 4-same-family fixture
    returns rc=1 + COALITION_ABORT JSON.
  - ✅ **Central dispatcher wire-up** (2026-06-05): `lib/gate_bootstrap.sh`
    auto-registers all 4 oracle gates with stable gate_ids
    (`oracle-{coalition,panel-health,synthesis-promote,stability}`) +
    task_class_filter=NULL (framework-wide). `bin/mini-ork-execute`'s
    publisher case-branch now sources gate_bootstrap + invokes
    `gate_run_all` BEFORE the artifact-publish loop fires. Any safety
    gate that returns `fail` flips `safety_violation=true` →
    publisher returns rc=1 with `[BLOCK] oracle-gates: safety_violation`
    log → no artifact escapes the framework boundary. Escape hatch:
    `MO_ORACLE_GATES_AUTO=0`.
    Decision tree: 3-subagent consensus (Security / Reliability /
    Maintainability) UNANIMOUS on path (b) — single chokepoint at
    measure_topology slot. Decision doc embedded in commit message of
    the wire-up commit. Integration test
    `tests/integration/test_oracle_gates_auto_wire.sh` pins the
    contract (2 fixtures green, 2 deferred placeholders for follow-up
    coverage).
- **Wave 2-A** — per-recipe held-out anchor corpus (Wang 2026). Hand-author per synthesis recipe; corpus selection is judgment-heavy.
- **Wave 3** ✅ landed 2026-06-13 in [`lib/citation_verifier_mechanical.sh`](lib/citation_verifier_mechanical.sh): recall-floor oracle for synthesis-style findings (Sistla 2025 + Ficek 2025). Mechanical citation coverage + wireheading check in one gate (commit `31f7808`).

### Calibration + adversarial gates (the positioning-doc honest-gaps list)

All four calibration-list items shipped 2026-06-13. The list is closed.

- ✅ **Krippendorff α calibration gate** ([`lib/krippendorff_alpha_gate.sh`](lib/krippendorff_alpha_gate.sh), commit `3d1e815`) — α<0.4 across panel lens scores escalates to human review per Nasser 2026.
- ✅ **Adversarial fabricated-bug injection** ([`lib/refute_or_promote_gate.sh`](lib/refute_or_promote_gate.sh), commit `ad48ef3`) — two leaf primitives (generate N fabrications + check FP-survival) per [Agarwal 2026 *Refute-or-Promote*](https://arxiv.org/abs/2604.19049). FP-survival > 10% triggers REFUTE_FAILED.
- ✅ **Wireheading check on validators** ([`lib/citation_verifier_mechanical.sh`](lib/citation_verifier_mechanical.sh), commit `31f7808`) — same gate as Wave 3. Mechanically resolves each citation against repo root; coverage < 80% triggers CITATION_UNDERCOVERED.
- ✅ **Honest confidence intervals on every claim** ([`lib/honest_ci_gate.sh`](lib/honest_ci_gate.sh), commit `91eba3d`) — per-finding CI from lens votes via t-dist with df=n-1 per [Dai 2025 *Semantic Triangulation*](https://arxiv.org/abs/2511.12288). wide_ratio > 30% triggers CI_TOO_WIDE.

### Evolution + promotion layer (deferred from v0.2)

- `lib/group_evolver.sh` proposes workflow candidates based on accumulated
  trace + gradient data; `mini-ork improve` materialises them
- `lib/promotion_gate.sh` enforces utility-delta + benchmark-pass + safety
  checks before promoting a candidate to the active workflow
- `lib/version_registry.sh` exposes rollback as a first-class CLI verb:
  `mini-ork rollback <workflow|agent> <name>`

### Substrate ✓ closed in v0.2-pt24..pt36

- ✅ D-048 (v0.2-pt36, 2026-06-05): gradient_extract prompt reframed
  from "what algorithm needs fixing?" → "what about this RECIPE'S
  design would have made the OUTCOME better?" with a 5-target taxonomy
  covering both algorithmic (per-node) AND coordination (recipe-level)
  shapes. Audit-recipe traces now produce signal instead of `[]`.
- ✅ D-045 (v0.2-pt24, 2026-06-01): `task_runs.ended_at` now set by
  `_d021_set_status` helper when transitioning to terminal status
  (published / rolled_back / failed). Metric trajectory wall-times
  accurate again.

### Drift detection — operator follow-ups (v0.3.x)

- INDETERMINATE verdict for the L2b drift panel (closed 2026-06-05):
  panel arbiter now emits 3-valued `NO_DRIFT | DRIFT | INDETERMINATE`
  with a post-process safety net that overrides arbiter NO_DRIFT to
  INDETERMINATE when all 4 lenses returned confidence=0 OR errored.
  Pre-push hook treats INDETERMINATE as fail-open by default,
  block-on `MO_README_PANEL_INDETERMINATE=block`.
- providers-doctor pre-flight (closed 2026-06-05):
  `scripts/readme-drift-providers-doctor.sh` probes each lens provider
  with a 1-token prompt before the panel fires. If <
  `MO_DRIFT_MIN_RESPONSIVE_LENSES` (default 2) respond within
  `MO_DRIFT_PROBE_TIMEOUT_SEC` (default 20s), panel is skipped with
  `reason=panel_unavailable`. Cuts ~360s wasted-timeout wall to ~30s.
- Provider triage playbook at
  [`docs/operator/provider-triage.md`](docs/operator/provider-triage.md)
  documents 6-step diagnosis + remediation for the
  "kimi/glm/minimax/codex all silent-failing" environment-side issues
  surfaced during the 2026-06-05 live-smoke session.

### Agent-ops hardening — LobeHub-informed (2026-06-10 deep review)

Source: deep review of [lobehub/lobehub](https://github.com/lobehub/lobehub)
(local checkout `~/ps/lobehub`), an agent-operations platform whose
mechanisms map closely onto mini-ork's loop. Ordered by dependency: each
phase consumes the previous one's outputs. LobeHub file references point
at the mechanism to port, not code to copy.

**Phase 1 — truthful run telemetry** (fixes observed lies; everything
downstream depends on it)

1. **Dispatch-time config snapshot.** Freeze resolved lane→family/model
   onto the run record at dispatch (new column or `run_events` payload)
   instead of re-resolving from `config/agents.yaml` at view time. Root
   cause of the 2026-06-10 sonnet-vs-codex badge bug; LobeHub pattern:
   denormalized `appContext` / `verifyPlan` snapshots
   (`packages/database/src/schemas/agentOperations.ts`).
2. **Error taxonomy on `llm_calls`.** Add `error_category`
   (auth/quota/capacity/request/safety/network/stream/provider/config) +
   retryable-vs-fatal classification in `lib/llm-dispatch.sh`, extending
   `lib/throttle-guard.sh`'s provider-throttle classification to the full
   taxonomy (`packages/model-runtime/src/errors/taxonomy.ts`,
   `utils/isNonRetryableRequestError.ts`).
3. **Finish reasons on node lifecycle.** `node_end` events carry *why*
   (`done | error | interrupted | max_steps | cost_limit | timeout`)
   distinct from status, fed by item 2. Kills the "node done, dispatch
   failed with zero output" class observed on run-1781081895-31571
   (`agentOperations.ts:11-29` lifecycle + completion reasons).
4. **Heartbeat watchdog + failure fuse for nodes.** Extend the legacy
   `runs.last_heartbeat_at` pattern to task-loop nodes: `heartbeatTimeout`
   kills silent hangs (the 25-min dead codex dispatch); fuse = halt the
   lane after 3 consecutive failures and surface a briefing instead of
   retrying forever (`packages/database/src/schemas/task.ts:70-79`,
   `agentCronJob.ts`).

**Phase 2 — cost + capability accuracy** (consumes Phase 1's per-call
truth)

5. **Cache-aware cost accounting.** Track `cached_input_tokens` /
   cache-write tokens in `llm_calls`; bill cache reads at cache rate,
   subtract from input. Largest current cost error on Anthropic-heavy
   lanes (`packages/model-runtime/src/core/usageConverters/utils/computeChatCost.ts:35-209`).
6. ✅ **Pricing strategy table** landed 2026-06-13 in [`lib/pricing_strategy.sh`](lib/pricing_strategy.sh) + [`.mini-ork/config/pricing.yaml`](.mini-ork/config/pricing.yaml) (commit `13ea509`). `pricing_lookup <provider> <model> <token_kind>` reads YAML rates; six default lanes covered (anthropic, openai, moonshot, deepseek, zhipu, minimax). Wiring into `lib/llm-dispatch.sh` is a deliberate follow-up.
7. **Capability flags in `agents.yaml`.** Per-family
   `capabilities: {vision, tools, reasoning, search}` so gates reject
   impossible lane assignments *before* dispatch
   (`packages/model-bank/src/types/aiModel.ts:28-61`).

**Phase 3 — feedback loops** (consumes Phases 1-2 telemetry)

8. **Langfuse score mapping.** Extend the planned exporter
   (docs/architecture/otel-langfuse.md): verdicts/rollbacks/promotions
   become trace scores (APPROVE +, REQUEST_CHANGES −, rollback −1.0) so
   traces self-rank (`src/libs/traces/event.ts:16-20`).
9. ✅ **Verifier rubrics with ground-truth feedback** landed 2026-06-13 in [`db/migrations/0025_verifier_rubrics.sql`](db/migrations/0025_verifier_rubrics.sql) + [`lib/verifier_rubric.sh`](lib/verifier_rubric.sh) (commit `53d6ad0`). Three correlated tables (`verifier_rubrics`, `verifier_criteria`, `verifier_results`) with operator-set `is_false_positive` / `is_false_negative` flags + `repair_run_id` chaining. CRUD primitives: `rubric_register`, `verifier_result_record`, `verifier_result_annotate`, `verifier_chain_repair`, `verifier_fp_rate`.
10. ✅ **Checkpoint/resume primitive** landed 2026-06-13 in [`lib/checkpoint.sh`](lib/checkpoint.sh) (commit `843eca2`). Four primitives (`checkpoint_write`, `checkpoint_can_resume`, `checkpoint_clear`, `checkpoint_summary`) backed by `${MINI_ORK_RUN_DIR}/.checkpoint.json`. Wiring into `bin/mini-ork-execute` is a deliberate follow-up.

**Phase 4 — operator control + UX polish**

11. **Intervention policies as a gate.** `never | required | always`
    per tool + parameter-pattern rules + always-wins security blacklist +
    confirmed-call memo (no duplicate prompts). The mature form of
    scope_gate; UI already has the kill path
    (`packages/agent-runtime/src/core/InterventionChecker.ts`).
12. **Throttled streaming UI updates.** Batch SSE transcript/log chunks,
    flush ≤1×/300ms to stop re-render storms on chatty agents
    (`src/store/chat/agents/StreamingHandler.ts:74-88`).
13. **Processing-state card.** Glanceable elapsed/steps/tool-calls/cost
    badges + progress shimmer beside the dispatch-tree DAG
    (`src/features/Conversation/Messages/Tasks/shared/ProcessingState.tsx`).
14. **Operation trees with cascading cancel.** Parent/child op hierarchy
    so killing a node aborts its children — completes the UI-kill work
    (`src/store/chat/slices/operation/types.ts`).

### Recipe portfolio

- `recipes/research-synthesis/` — multi-source paper synthesis
- `recipes/blog-post/` — long-form writing with iterative review
- `recipes/ui-audit/` — design-doc + screenshot lens triangulation
- `recipes/db-migration/` — schema-change with safety-gate
- `recipes/ops-runbook/` — incident-response playbook generator

### Web dashboard (separate repo)

Reads `state.db` read-only for visualisation: task_runs by status, agent
performance trends, candidate utility deltas, pending promotions awaiting
human gate.

## Eventually (v1.0)

- Hardened multi-machine state (Postgres backend as an alternative to
  sqlite for teams; same schema)
- A standard plugin protocol so third-party verifier scripts can be installed
  via `mini-ork plugin install <name>`
- Optional remote LLM-call telemetry (opt-in only) for cross-project
  benchmark sharing
- Stability guarantees: SemVer with documented breaking-change policy; every
  v1.x is backward-compatible with all prior v1.y

## Out of scope

These have been considered and intentionally excluded:

- Hosted SaaS version — keep the runtime local-first; users can build their
  own hosted layer on top
- Built-in LLM provider — the framework is provider-neutral; new providers
  plug in via `lib/providers/cl_<name>.sh`
- GUI bundled in the same repo — separate concern; the dashboard repo will
  consume state.db as a read-only contract
- Anything that breaks the bounded-autonomy axioms in [docs/SAFETY.md](./docs/SAFETY.md):
  silent self-mutation, hidden rollback, promotion without measurable utility

## How to influence the roadmap

1. Open a Discussion describing the use case you want to unlock
2. If there's interest, propose it as an Issue with a draft RFC
3. Build a recipe that demonstrates the pattern before asking for framework
   primitives — recipes can graduate into the framework once 2+ use the same
   abstraction

## Last updated

2026-06-13 — Calibration-list closed (Krippendorff α + citation coverage + Refute-or-Promote + honest CIs gates shipped); Wave 3 mechanical citation verifier landed; Phase 2 item 6 (pricing strategy table) + Phase 3 items 9 (verifier rubrics + ground-truth) + 10 (checkpoint/resume primitive) shipped; mini-ork rollback CLI verb wired; 3 session bugs closed (publisher dict-shape, child-implementer artifact path, defensive verdict-write); OSS hygiene pass

2026-06-10 — Agent-ops hardening track added (LobeHub deep-review, 14 items
across 4 dependency-ordered phases)
