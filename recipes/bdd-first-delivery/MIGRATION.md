# Migration guide — from internal mini-orch / agentflow

If you previously used an internal `deliver.sh`-based pipeline (often found under `.agentflow/lib/` or `mini-orch/`), this guide maps the old components to their recipe equivalents.

## Component mapping

| Old component | New equivalent | Notes |
|---|---|---|
| `deliver.sh` (entry point) | `mini-ork run bdd-first-delivery <kickoff.md>` | CLI invocation replaced by framework `run` command |
| `.agentflow/lib/dispatch.sh` | `recipes/bdd-first-delivery/lib/dispatch.sh` | Recipe-internal only; not framework lib |
| `.agentflow/lib/bdd-runner.sh` | `recipes/bdd-first-delivery/verifiers/playwright_runner.sh` | Rewritten as a verifier; emits `bdd-verdict.json` |
| `.agentflow/lib/spec-author.sh` | Framework invokes `prompts/spec_author.md` via `model_lane: spec_author` | Shell glue removed; prompt is the spec |
| `.agentflow/lib/spec-reviewer.sh` | Framework invokes `prompts/spec_reviewer.md` via `model_lane: spec_reviewer` | Same — shell glue removed |
| `.agentflow/lib/self-correction.sh` | Framework invokes `prompts/self_correction.md` via `model_lane: worker` | Shell glue removed |
| `.agentflow/lib/contract.sh` | Framework `scope_gate` (see `workflow.yaml` `gates: [scope_gate]`) | Scope enforcement is now a named gate |
| `.agentflow/prompts/decomposer.md` | `recipes/bdd-first-delivery/prompts/decomposer.md` | Rewritten; the host application references removed |
| `.agentflow/prompts/spec-author.md` | `recipes/bdd-first-delivery/prompts/spec_author.md` | Rewritten generically |
| `.agentflow/prompts/spec-reviewer.md` | `recipes/bdd-first-delivery/prompts/spec_reviewer.md` | Rewritten generically |
| `.agentflow/prompts/self-correction.md` | `recipes/bdd-first-delivery/prompts/self_correction.md` | Rewritten generically |
| `.agentflow/prompts/mutation-adversary.md` | `recipes/bdd-first-delivery/prompts/mutation_adversary.md` | Rewritten generically |
| `AGENTFLOW_DIR` env var | `MINI_ORK_HOME` | Framework env var replaces project-specific one |
| `.agentflow/INBOX/` path | `${MINI_ORK_HOME}/INBOX/` | Same concept, framework-rooted path |
| `MO_AGENTFLOW_DIR` / `AGENTFLOW_DIR` | `MINI_ORK_HOME` | All internal path references unified |
| `agents.yaml` worker bindings | `workflow.yaml` `node.model_lane` | Declarative node-to-model mapping replaces YAML agent registry |
| `state.db` (sqlite epics table) | Framework state store (see `MINI_ORK_HOME/state/`) | Schema maintained by framework; recipe reads via framework API |
| Resume-on-timeout (`MO_MAX_RESUME_PER_EPIC`) | Framework `--resume` flag on node invocation | Session resume is handled by framework worker launcher |

## What changed conceptually

**Before:** the internal pipeline was a set of bash scripts that tightly coupled decomposer logic, worker spawn, BDD running, and review into one `deliver.sh` monolith. Database state, agent registry, and prompt paths were all relative to a single project's `.agentflow/` directory.

**After:** the same pipeline shape lives as a user-land recipe under `recipes/bdd-first-delivery/`. The framework (`lib/`, `bin/`) handles state management, model routing, worker lifecycle, and result aggregation. The recipe owns only the prompts, the verifier script, and the recipe-internal dispatch helper.

## Preserved behaviors

These behaviors from the internal pipeline are preserved in the recipe:

- Sub-epic parallel dispatch (spec_author + implementer in parallel per sub-epic)
- Spec synthesis sub-loop (spec_author → spec_reviewer, max 2 sub-iters before escalate)
- Mutation adversary gate (optional; enabled via workflow flag)
- Self-correction on REQUEST_CHANGES (max 3 iters, `max_self_correction_iterations`)
- BE-only short-circuit (spec_author emits `SPEC_SKIPPED` when no UI surface detected)
- `bdd_role: leaf` gating (BDD runner skips leaf sub-epics until integration deps are merged)

## Behaviors NOT ported (internal-only)

These behaviors were specific to the original project and are not in the generic recipe:

- `mockApiCatchAll` / `seedAuth` spec helper requirements (project-specific test infra)
- `/lw/*` protected-route detection (project-specific URL convention)
- TypeScript-specific governance gates (G1: no `.js` files, G2: no `console.log`)
- InsForge memory grounding in prompts (project-specific MCP integration)
- OTel trace-spec contract assertions (project-specific observability contract)
- VLM judge on BDD failure (experimental; not stable enough for generic use)

These can be re-added as project-local overrides by forking the recipe into your project's `recipes/` directory and extending the relevant prompts.
