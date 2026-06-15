# Changelog

All notable changes to mini-ork are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

No unreleased changes yet.

---

## [0.3.0.1] - 2026-06-12

**Phase 1 of Agent-ops hardening complete.** Substrate-only patch release
ships the truthful-run-telemetry layer the v0.3 roadmap names as blocking
everything downstream (LobeHub-informed deep review, 2026-06-10). All four
items shipped as small, verifier-gated framework-edit dispatches in a
single session (~$10 LLM spend total, ~3 hours wall clock).

### Added

- **A2 + A3 — error taxonomy on `llm_calls` + finish reasons on `node_end`**
  (`db/migrations/0021_error_taxonomy_finish_reasons.sql`, commit
  `ca4a165`). New `llm_calls.error_category` (9-category enum: auth /
  quota / capacity / request / safety / network / stream / provider /
  config / unknown) + `retryable` (0/1) classified inside `lib/llm-
  dispatch.sh`. New `run_events.finish_reason` (8-value enum: done /
  error / interrupted / max_steps / cost_limit / timeout /
  verdict_revise / verdict_fail) emitted by `bin/mini-ork-execute`.
  Reviewer node now reads VERDICT and returns non-zero on revise / fail
  / REQUEST_CHANGES / ESCALATE so `escalates_to → rollback` edges fire
  as recipe authors expect. Closes the framework-edit v1 (2026-06-11)
  failure mode where a recipe published despite arbiter `needs_revision`.

- **A1 — dispatch-time config snapshot**
  (`db/migrations/0022_dispatch_config_snapshot.sql`, commit `fbb59f5`).
  New `task_runs.dispatch_config_json` (full resolved lane → {family,
  model_id, provider, base_url} map frozen at dispatch start) +
  `task_runs.agents_yaml_sha`. The UI's `mini_ork/web/agents.py` now
  prefers the snapshot over re-resolving from current `config/agents.yaml`,
  closing the 2026-06-10 sonnet-vs-codex badge bug.

- **A4 — heartbeat watchdog + failure fuse for nodes**
  (`db/migrations/0023_node_heartbeat_fuse.sql`, commit `d8d2417`).
  Background per-node heartbeat loop emits `node_heartbeat` events
  every `MO_HEARTBEAT_INTERVAL_S` (default 30s). Inline watchdog
  SIGTERMs nodes whose last heartbeat is older than
  `MO_HEARTBEAT_TIMEOUT_S` (default 300s = 5 min), marking
  `finish_reason=timeout`. Failure fuse halts a lane after 3
  consecutive same-category retryable failures — depends on A2's
  classifier. Closes the 25-min dead codex dispatch class observed
  2026-06-10.

### Fixed

- **Heartbeat loop noise** (`bin/mini-ork-execute`): silence stderr from
  the background heartbeat subshell to keep `execute.log` clean.

### Notes

- Backward-compatible with v0.3.0. All new columns NULL-able for legacy
  rows; legacy code paths read NULL as "snapshot not available, fall
  back".
- Four follow-up items deferred to v0.3.1, blocked by intermittent
  planner-output parser intolerance (D-011/D-016 class) and the
  framework-edit verifier-methodology bug discovered by A4's reviewer
  on 2026-06-11:
  1. `recipes/framework-edit/verifiers/{static-check,test}.sh` —
     throwaway copy is not its own git root; verifiers measure HEAD
     instead of the patched diff. Discovered by opus reviewer; local
     re-verify after `git apply` has been the operator-side gate.
  2. `recipes/framework-edit` — implementer writes `verdict.json` with
     `pass=false` defensively before verifiers run; reviewer reads
     stale verdict and emits `needs_revision`. Verifier-checks.tsv is
     the source of truth.
  3. `bin/mini-ork-plan` parser — opus markdown-wrap + codex streaming
     envelope both produce parse_error / missing-field rejections on
     long kickoffs. Tolerant extraction works for short kickoffs.
  4. Phase 2 (cache-aware cost accounting, capability flags, pricing
     strategy table), Phase 3 (checkpoint/resume), Phase 4 (UX polish),
     Track B (calibration gates), Arbor plan days 3-5 — all blocked
     on a working framework-edit dispatch.

### Dispatch artifacts (audit trail)

- A2+A3: run-1781192890-33995 ($4.60, 18 min, 23/23 verifier-checks pass)
- A1: run-1781194771-50446 ($2.64, 16 min, 23/23 verifier-checks pass)
- A4: run-1781195887-21821 ($2.79, 18 min, 23/23 verifier-checks pass; first
  reviewer to surface the framework-edit verifier-methodology bug)

---

## [0.3.0] - 2026-06-11

### Added

- **Arbor-style Idea Tree primitive** (`db/migrations/0020_idea_tree.sql`):
  generic `idea_tree_nodes` table for hypothesis-tree exploration patterns,
  status enum (`pending` / `running` / `harvested` / `pruned` / `rejected`),
  insights JSON column for future upward propagation. Idempotent backfill
  script (`scripts/backfill_idea_tree.py`) materializes existing
  `self_improve_runs` history as tree nodes chronologically per day-cluster.
- **Idea Tree read API** under `/api/v1/idea-tree/*` — `roots`, `{root_node_id}`
  subtree, single-node, ancestor-chain endpoints. Loopback-only.
- **Idea Tree visualization** on the Trajectory UI page — `@xyflow/react`
  panel with status-color-coded nodes, click-through to per-run forensics or
  self-improve iter detail.
- **`recipe-creator` meta-recipe** (`recipes/recipe-creator/`) — authors new
  recipes from natural-language epics via a 3-family drafter panel
  (glm/kimi/codex) → opus arbiter → verifier_smith → HARD heterogeneity-floor
  validator. The framework dogfooding itself on small-N recipe authoring.
- **`silent-catch-audit` recipe** (`recipes/silent-catch-audit/`) — first
  recipe authored end-to-end by `recipe-creator`. 3-lens audit of TS/JS
  codebases for silent `.catch(() => {})` anti-patterns.
- **`framework-edit` recipe** (`recipes/framework-edit/`) — recipe-creator-
  authored. Routine mini-ork self-modification with verifier-gated discipline
  (`bash -n`, `py_compile`, `tsc --noEmit`, `pytest tests/test_web_smoke.py`
  against a throwaway patched copy). Emits a unified diff for operator review;
  does NOT auto-apply.
- **Three locally-authored recipes**: `blog-cohesion`, `bug-audit-cmgk`,
  `feature-inventory-cmgk`.
- **Routing policy primitive** (`MO_ROUTING_POLICY` env): five policies
  (`workflow_default`, `frontier_only`, `cheap_only`, `static_hybrid`,
  `trace_governed`) swap workflow-declared lanes at dispatch time without
  recipe edits.
- **Agent in-flight banner** on the Run Detail Agent transcript: pulsing
  amber indicator + explanatory copy when an agent dispatch is still running.

### Fixed

- **`recipes/recipe-creator/verifiers/recipe-validator.sh`** — Python
  `NameError: name 'true' is not defined` when bash `pass=true` was
  interpolated into a Python heredoc. Translate at the bash→Python boundary
  with `True`/`False` literals.
- **`lib/lane-helpers.sh`** — feature-detect
  `--exclude-dynamic-system-prompt-sections` on the `claude` CLI before
  emitting it. Older CLIs (≤2.1.47 observed) hard-fail every dispatch on
  unknown options; probe once per process and degrade to cache-miss instead.
- **`bin/mini-ork-execute` publisher branch** (shipped 2026-06-10 in
  `f11f380`, included here for v0.3.0 finalization): resolves `${VAR}`
  substitutions in `artifact_contract.yaml` `source_artifact` + `outputs[]`
  via envsubst, and copies directory sources via `cp -R src/. dst/`. Required
  for the meta-recipe pattern to auto-publish derivative recipes.
- **`bin/mini-ork-plan`** (shipped 2026-06-10 in `ad2ea05`): dispatcher
  stderr now persisted to `$RUN_DIR/plan-dispatch.err.log` and echoed on
  dispatch failure. Closes the opaque "LLM dispatch failed for planner node"
  failure mode.

### Notes

- Backward-compatible with v0.3.0-rc2. All additions are opt-in.
- `framework-edit` recipe's `verdict.json` has a known write-ordering bug
  (implementer writes `pass=false` defensively before verifiers run). The
  authoritative source is each verifier's `verifier-<name>.checks.tsv` file,
  not `verdict.json`. Fix tracked for v0.3.1.

---

## [0.3.0-rc2] - 2026-06-10

### Added

- Bounded recursive orchestration primitive: `mini-ork spawn` can approve and
  execute child mini-ork runs under an isolated child workspace while recording
  lineage, event-log, artifact-edge, and merge-decision tables.
- Python facade support for recursive delegation via `SpawnRequest` and
  `MiniOrk.spawn(...)`.
- Recursive validation coverage: integration, security, e2e, and live Python
  validation scenarios for parent -> child -> grandchild delegation without
  Anthropic-family provider calls.
- CI release gates for README mechanical claim checks, ShellCheck, bash test
  layers, Python web tests, UI typecheck/build, CodeQL, GitGuardian, and
  dependency review.
- Observability web smoke CI stage that exercises `make web-test`.

### Fixed

- UI typecheck in clean GitHub runners by adding explicit Node 20 type
  declarations for `vite.config.ts`.
- ShellCheck parsing of the optional-check comment in `tests/smoke.sh`.
- Linux/GNU portability for empty plan discovery in `mini-ork-execute` and
  `mini-ork-verify`.
- Linux/macOS `stat` portability in the symlink-attack security test.
- Dependency Review no longer keeps every PR red when GitHub Dependency graph is
  unavailable; the workflow still runs the strict gate when the graph is
  enabled.

### Verified

- GitHub CI for PR #8: README claim check, ShellCheck, bash smoke/unit/
  integration/e2e/security, Python 3.11/3.12, UI typecheck/build, web smoke,
  CodeQL, GitGuardian, and Dependency Review all passed.
- Full test pyramid: 60 files, 543 assertions, 0 failures.
- Recursive focused tests: `test_bin_spawn.sh` 9 OK, recursive spawn security 3
  OK, recursive e2e 6 OK.
- Live recursive validation: `PYTHONPATH=. python3 tests/live/recursive_live_validation.py`
  passed with root -> child -> grandchild lineage and 2 completed child events.

---

## [0.3.0-rc1] - 2026-06-08

### Added

- Python framework facade: importable `mini_ork` package with typed run,
  workflow, recipe, provider-policy, and extension contracts.
- Production scenario framework for real markdown kickoffs across the shipped
  recipe catalog.
- Dispatcher run-profile enrichment: `mini-ork run` now writes
  `run_profile.json`, emits profile questions before planning, and supports
  `MINI_ORK_PROFILE_STRICT=1` for high-risk incomplete profiles.
- Live Phase E Codex validation report for improve -> benchmark -> eval ->
  promote.

### Verified

- Full test pyramid: 57 files, 525 assertions, 0 failures.
- Production scenario sweeps: 9/9 explicit recipe and 9/9 markdown-only
  dispatcher dry-runs passed with Codex-only provider policy.
- Phase E live harness: 8 OK / 0 FAIL using `PHASE_E_PROVIDER=codex`.

---

## [0.1.1] - 2026-05-30

### Added — OSS-readiness deltas

- `GOVERNANCE.md` — lazy-consensus single-maintainer model + reviewer/lead-maintainer path
- `MAINTAINERS.md` — current maintainers list (template; replace handle when forking)
- `SUPPORT.md` — help-channels priority list + scope-of-support clarity
- `ROADMAP.md` — v0.2 / v0.3 / v1.0 buckets + explicit out-of-scope list
- `RELEASING.md` — SemVer policy + cut-release recipe + deprecation policy
- `.github/CODEOWNERS` — safety-critical paths require explicit lead approval
- `.pre-commit-config.yaml` — shellcheck + bash-syntax + schema-validate hooks
- `examples/00-demo.sh` — 60-second runnable demo (dry-run, no API keys needed)
- README badges (license / version / CI / status) + "60-second demo" callout

### Fixed — universal-loop integration bugs surfaced by end-to-end smoke

- (schema) Added migration `0013_task_runs.sql` — universal-loop runtime needs a
  table distinct from the the host application-shape `runs` table (which requires `epic_id` FK)
- (classify) Now writes to `task_runs` (was: `runs`, which failed FK + column mismatch)
- (classify) Matchers tolerate BOTH flat `matches: [kw...]` AND structured
  `matches: { keywords, regex }` shapes — recipes had inconsistent schemas
- (plan) Replaced sed-based `{{KICKOFF_CONTENT}}` substitution (crashed on
  multi-line markdown) with python `str.replace` — hermetic
- (plan) Dry-run writes placeholder JSON to `OUT_FILE` so downstream verify can
  read it; diagnostics moved to stderr (stdout reserved for `plan_path=…`)
- (dispatcher) Top-level `mini-ork run` captures classify+plan stdout to thread
  `MINI_ORK_TASK_CLASS` + `MINI_ORK_PLAN_PATH` into subsequent steps via env
- (dispatcher) Pre-allocates `MINI_ORK_RUN_ID` so all 4 steps write to the same
  `task_runs` row
- (init) Seeds `config/task_classes/` from `recipes/*/task_class.yaml` on
  bootstrap so the classifier matches on a fresh project
- (init) Updated "next steps" hints: `deliver` → `mini-ork run <recipe> <kickoff>`
- (agents.yaml) Added canonical loop-role lanes (planner, worker, reviewer,
  verifier, reflector, publisher, rollback, researcher) — workflow.yaml refs now resolve
- (bin/) Added `bin/mini-ork-invoke-prompt` — single-prompt LLM helper for
  recipe-internal sub-steps (was referenced by bdd-first-delivery but never built)
- (bdd-first) Fixed `lib/dispatch.sh` invoke-prompt path: `MINI_ORK_HOME/bin` →
  `MINI_ORK_ROOT/bin` (helper lives in framework dir, not user's project home)

### Known issues (deferred to v0.1.2)

- `mini-ork-verify` exits 1 in dry-run when no artifact is produced — harmless
  but makes CI green-state ambiguous; needs explicit dry-run code path
- `recipes/bdd-first-delivery/task_class.yaml` uses top-level `keywords:` instead
  of the canonical `matches: { keywords: [...] }` shape; classifier handles both
  but should be normalized

### Verified

- 72 sqlite tables across 13 migrations apply cleanly (idempotent)
- 61/61 bash -n syntax check across all bin/lib/hooks/tests/recipe scripts
- `tests/smoke.sh`: 97/97 OK
- `bash examples/00-demo.sh` runs end-to-end in dry-run; produces a `task_runs`
  row with `task_class=code-fix`, `recipe=code-fix`, `status=classified`
- 0 domain leaks (`the host application`, `jisawru`, `100.74.239.22`, etc.)
- 0 legacy env-var refs (`AGENTFLOW_*`, `MO_AGENTFLOW_*`)

---

## [0.1.0-redesign] - 2026-05-30

### BREAKING — full architectural rewrite

Replaces the literal port from internal mini-orch (v0.0-extract) with a
framework-and-recipes architecture per
`ideal-mini-orch-self-evolving-system-book.md`.

The v0.0 baseline is preserved at git SHA `0ec2bf1`. See
[docs/REDESIGN.md](docs/REDESIGN.md) for the migration guide.

### Removed

- 42 pipeline-specific prompts — relocated to `recipes/` (domain coupling removed from framework)
- 6 BDD-specific lib scripts — relocated to `recipes/bdd-first-delivery/lib/`
- `skills/` dir (domain-coupled, replaced by `recipes/<recipe>/verifiers/`)
- `bin/mini-ork-deliver` as top-level command (now a recipe; use `mini-ork run bdd-first-delivery <kickoff.md>`)

### Added — framework primitives

13 new `lib/` scripts implementing the universal loop primitives:

| Script | Role |
|---|---|
| `lib/trace_store.sh` | Execution trace writes + reads |
| `lib/gradient_extractor.sh` | TextualGradient extraction from traces |
| `lib/reflection_pipeline.sh` | Reflection worker orchestration |
| `lib/context_assembler.sh` | Relevance-bounded context packing |
| `lib/pattern_store.sh` | Pattern emergence + deduplication |
| `lib/benchmark_suite.sh` | Benchmark task runner |
| `lib/utility_function.sh` | Default utility scorer + override hook |
| `lib/promotion_gate.sh` | Utility delta + benchmark gate enforcement |
| `lib/version_registry.sh` | Promote / quarantine / rollback + audit log |
| `lib/gate_registry.sh` | Gate type registration + dispatch |
| `lib/group_evolver.sh` | Workflow candidate generation (parent selection) |
| `lib/artifact_contract.sh` | Per-class artifact contract validation |
| `lib/agent_registry.sh` | Agent role + model version registration |

### Added — entrypoints

8 new `bin/` entrypoints for the universal loop stages:

```
bin/mini-ork-classify
bin/mini-ork-plan
bin/mini-ork-execute
bin/mini-ork-verify
bin/mini-ork-reflect
bin/mini-ork-improve
bin/mini-ork-eval
bin/mini-ork-promote
```

### Added — schemas

4 new JSON schemas:

- `schemas/task_class.schema.json` — task class YAML validation
- `schemas/workflow.schema.json` — workflow DAG validation (nodes, edges, gates)
- `schemas/agent_version.schema.json` — agent version metadata
- `schemas/artifact_contract.schema.json` — artifact contract shape

### Added — migrations

4 new DB migrations:

- `db/migrations/004_memory_namespaces.sql` — 8 memory namespace tables
- `db/migrations/005_benchmarks.sql` — benchmark tasks + results
- `db/migrations/006_evolution.sql` — workflow candidates, versions, promotion log
- `db/migrations/007_safety.sql` — audit_log (append-only), quarantine registry

### Added — recipes

- `recipes/code-fix/` — minimal reference recipe (classify → plan → implement → verify)
- `recipes/bdd-first-delivery/` — full BDD pipeline ported as a user-land recipe
- `recipes/_legacy_host_app_prompts/` — archived v0.0 prompts (read-only reference)

### Migration path

Replace:
```bash
mini-ork deliver kickoff.md
```
With:
```bash
mini-ork run bdd-first-delivery kickoff.md
```

The workflow shape is identical. The implementation now lives in
`recipes/bdd-first-delivery/` instead of hard-coded in `lib/`.

---

## [0.0.0-extract] - 2026-05-30

Initial extraction from internal mini-orch (literal port; baseline preserved at
git SHA `0ec2bf1`).

### Added

- `mini-ork deliver <kickoff.md>` — end-to-end decompose → workers → review → BDD → merge
- `mini-ork init` — scaffold `.mini-ork/` config directory in any repo
- `lib/dispatch` — epic claim + lane subprocess manager
- `lib/memory` — sqlite WAL read/write helpers
- `lib/auto-merge` — rebase-guard + git merge with audit metadata
- `lib/bdd-runner` — Gherkin scenario executor
- `lib/spec-author` — LLM-backed BDD spec generation
- `lib/spec-reviewer` — adversarial diff reviewer
- `lib/rebase-guard` — conflict detection before merge
- `lib/scope-overlap` — prevents two epics from claiming the same file
- `lib/llm-dispatch` — model routing by epic complexity tag
- `lib/contract` — kickoff constraint extraction
- `lib/self-correction` — structured feedback loop for failed BDD gates
- `lib/cache` — prompt + response caching keyed by content hash
- `lib/healer` — self-heal iter on BDD failure
- `lib/finalize` — post-merge cleanup + state.db verdict write
- `agents.yaml` config schema (max_iters, model overrides, lane cap)
- sqlite `state.db` schema: runs, epics, epic_reviews, bdd_runs, events, model_costs
- `.mini-ork/INBOX/` escalation for unresolvable failures
- `examples/` directory with smoke-testable kickoff fixtures
- `tests/smoke.sh` — offline smoke test with mocked claude binary
- Apache-2.0 license
