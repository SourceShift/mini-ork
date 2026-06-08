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

### v0.2.0 — 2026-06-01 (current)

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
  (D-047): glm_lens→glm, kimi_lens→kimi, codex_lens→codex, minimax_lens→minimax
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

### v0.3.0-rc1 — 2026-06-05 (in flight)

**Oracle Hardening, Wave 1 + Wave 2 partial.** Shipped as 5 self-contained
primitives in `lib/` plus a positioning honesty patch. Wire-up into
`bin/mini-ork-execute` is deferred; recipes can opt-in at their own pace
by sourcing the libraries from a verifier node.

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

Dispatch path findings filed at `docs/fixes/20260604-dispatch-classifier-overrides-explicit-recipe.md` — the `bin/mini-ork run <recipe>` path needs to honor the explicit recipe arg + a new `recipes/docs/` task class needs to ship before pure-docs kickoffs can dispatch through the canonical dogfood loop.

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
- **Wave 3** — `lib/citation-verifier-mechanical.sh` recall-floor oracle for `refactor_audit` findings (Sistla 2025 + Ficek 2025). 2-3 week sub-decomposition.

### Calibration + adversarial gates (the positioning-doc honest-gaps list)

- **Krippendorff α calibration gate** per Nasser 2026: compute α across
  deliberators' first-round proposals; below 0.4 → escalate to human review
  rather than vote on it
- **Adversarial fabricated-bug injection** per [Agarwal 2026 *Refute-or-Promote*](https://arxiv.org/abs/2604.19049):
  plant N known-fake bugs in the audit input; measure validator
  false-positive survival rate as the quality signal
- **Wireheading check on validators**: verify the validator actually read
  the cited file (Read/Grep tool calls in trace) before upholding severity.
  Already partly there via D-042 rich `files_read` capture — the gate isn't
  enforced yet
- **Honest confidence intervals on every claim** per [Dai 2025 *Semantic Triangulation*](https://arxiv.org/abs/2511.12288):
  not "P1" but "P1 ± 1 (95% CI: [P0, P2]) per N=4 validators with κ=0.3"

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

2026-06-01 — v0.2.0 ship + Phase G positioning lock-in
