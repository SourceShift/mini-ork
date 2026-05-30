# v0.0 → v0.1 Redesign Guide

This document explains why v0.1 breaks compatibility with v0.0, what changed, and how to migrate.

The v0.0 baseline is preserved at git SHA `0ec2bf1`. You can diff any file against it or reset a branch to that SHA if you need the old behavior.

---

## Why the Redesign Happened

v0.0 was a literal port of an internal orchestrator. It worked, but it shipped structural coupling that placeholder substitution cannot fix:

- **48% of prompts were named after specific pipeline stages** (`refactor-arch-struct.md`, `bdd-spec-author.md`, `scope-overlap-check.md`). These are opinions about one particular pipeline shape, baked into the framework layer.
- **`lib/` scripts assumed BDD delivery** — `lib/spec-author.sh`, `lib/bdd-runner.sh`, `lib/spec-reviewer.sh` had no abstraction boundary. There was no way to run a `code-fix` workflow without also pulling in BDD machinery.
- **No memory model** — execution experience was not stored. Every run started cold. Self-improvement was impossible.
- **No evaluation layer** — there was no way to test whether a workflow change was better or worse before promoting it.

The redesign inverts the architecture:

```
v0.0                            v0.1
────────────────────────        ────────────────────────────────────
lib/ = delivery opinions        lib/ = universal primitives
bin/ = deliver command          bin/ = 8 loop stage entrypoints
no recipes dir                  recipes/ = delivery opinions
no memory namespaces            8 memory namespaces in state.db
no benchmark suite              lib/benchmark_suite.sh
no promotion gate               lib/promotion_gate.sh + version_registry.sh
```

---

## Architecture Inversion

| Layer | v0.0 | v0.1 |
|---|---|---|
| Framework | `lib/` — BDD pipeline hardcoded | `lib/` — universal loop primitives only |
| Opinions | Baked into lib scripts | `recipes/<recipe>/` — user-land |
| Entrypoint | `mini-ork deliver <kickoff.md>` | `mini-ork run <recipe> <kickoff.md>` |
| Memory | None | 8 namespaces: task / workflow / agent_performance / failure / recovery / user_preference / artifact / benchmark |
| Evaluation | None | `lib/benchmark_suite.sh` + `lib/utility_function.sh` |
| Governance | None | `lib/promotion_gate.sh` + `lib/version_registry.sh` + `audit_log` |
| Schema | 14 tables | ~45 tables across 7 migrations |

---

## Mapping Table: Old → New Home

| v0.0 path | v0.1 home | Notes |
|---|---|---|
| `lib/dispatch.sh` | `recipes/bdd-first-delivery/lib/dispatch.sh` | BDD-specific epic claim + lane fork |
| `lib/bdd-runner.sh` | `recipes/bdd-first-delivery/lib/bdd-runner.sh` | Gherkin executor belongs to recipe |
| `lib/spec-author.sh` | `recipes/bdd-first-delivery/lib/spec-author.sh` | BDD spec generation belongs to recipe |
| `lib/spec-reviewer.sh` | `recipes/bdd-first-delivery/lib/spec-reviewer.sh` | BDD-gated review belongs to recipe |
| `lib/self-correction.sh` | `recipes/bdd-first-delivery/lib/self-correction.sh` | BDD re-prompt loop belongs to recipe |
| `lib/auto-merge.sh` | `recipes/bdd-first-delivery/lib/auto-merge.sh` | Merge behavior is recipe policy |
| `lib/memory.sh` | `lib/trace_store.sh` + framework tables | Generalized; 8 namespaces instead of 14-table bespoke |
| `lib/llm-dispatch.sh` | `lib/agent_registry.sh` + `config/agents/*.yaml` | Model bindings now declarative |
| `lib/contract.sh` | `lib/artifact_contract.sh` | Generalized artifact contract shape |
| `lib/healer.sh` | `lib/healer.sh` (kept) + `retries` edge type | Still in framework; now edge-typed |
| `lib/cache.sh` | `lib/cache.sh` (kept) | Unchanged |
| `lib/scope-overlap.sh` | `lib/gate_registry.sh` (`scope_gate` type) | Gate, not a lib script |
| `lib/rebase-guard.sh` | `recipes/bdd-first-delivery/lib/rebase-guard.sh` | Merge guard belongs to recipe |
| `lib/finalize.sh` | `publisher` node type in `workflow.yaml` | Node, not a lib script |
| `prompts/refactor-arch-struct.md` | `recipes/_legacy_host_app_prompts/` | Archived; read-only reference |
| `prompts/bdd-spec-author.md` | `recipes/_legacy_host_app_prompts/` | Archived |
| `skills/` | `recipes/<recipe>/verifiers/` | Domain-specific verifiers live in recipe |

---

## Migration Steps

**Step 1 — Replace the command**

```bash
# v0.0
mini-ork deliver kickoff.md

# v0.1
mini-ork run bdd-first-delivery kickoff.md
```

The `bdd-first-delivery` recipe is a direct port of the v0.0 pipeline. Workflow shape is identical. Config (max_iters, model overrides, lane cap) maps 1:1 from `agents.yaml`.

**Step 2 — Re-initialize the state.db**

v0.1 adds 4 new migrations. If you have an existing `state.db`, run:

```bash
mini-ork init --migrate
```

This is additive — existing runs/epics/events are preserved.

**Step 3 — Move custom prompts to a recipe**

If you had custom prompts under the old `prompts/` dir, move them to a recipe:

```
recipes/my-workflow/prompts/my-prompt.md
recipes/my-workflow/workflow.yaml
```

Reference the prompt via the `prompt_file` field on a node in `workflow.yaml`.

**Step 4 — Optional: switch to the minimal `code-fix` recipe**

If you were using `mini-ork deliver` for simple single-file patches rather than full BDD delivery, the `code-fix` recipe is faster:

```bash
mini-ork run code-fix kickoff.md
```

---

## Future Recipes

The book's task-class table suggests natural recipe candidates for v1.0:

| Recipe (planned) | Task class | Shape |
|---|---|---|
| `research-synthesis` | `research_synthesis` | sources → researcher → synthesis → citation-check |
| `blog-post` | `blog_post` | brief → outline → draft → voice-rubric → human-gate |
| `ui-audit` | `ui_audit` | route-list → screenshot → a11y-check → issue-list |
| `db-migration` | `db_migration` | schema-diff → migration-file → dry-run → rollback-proof |
| `ops-runbook` | `ops_runbook` | intent → runbook → health-probe → blast-radius-review |

Each will be a `workflow.yaml` + optional `verifiers/` + optional `prompts/`. Zero changes to `lib/`.

---

## Rollback to v0.0

```bash
git checkout 0ec2bf1
```

The v0.0 baseline is intact. The v0.1 state.db schema is a superset; rolling back to v0.0 code against a v0.1 state.db will work as long as you only query the original 14 tables.
