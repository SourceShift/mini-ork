# Changelog

All notable changes to mini-ork are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
  table distinct from the libwit-shape `runs` table (which requires `epic_id` FK)
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
- 0 domain leaks (`libwit`, `jisawru`, `100.74.239.22`, etc.)
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
- `recipes/_legacy_libwit_prompts/` — archived v0.0 prompts (read-only reference)

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
