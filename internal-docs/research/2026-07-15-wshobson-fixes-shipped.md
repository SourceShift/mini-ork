# Fixes shipped: wshobson/agents discipline transplants into mini-ork

Implementation of the six framework-edit kickoffs from
`2026-07-15-independent-wshobson-analysis.md`. All changes preserve the existing
Python runtime parity gates.

## FE-1: Single path-resolution contract + engine pointer

- New file: `lib/paths.sh`
  - Exports `MINI_ORK_ENGINE_ROOT`, `MINI_ORK_PROJECT_HOME`, `MINI_ORK_TARGET_REPO`.
  - Legacy aliases `MINI_ORK_ROOT` / `MINI_ORK_HOME` preserved.
  - Supports `.mini-ork/engine` pointer file for external-repo installs.
- Updated entrypoints to source `lib/paths.sh` instead of computing their own
  defaults:
  - `bin/mini-ork`, `bin/mini-ork-epics`, `bin/mini-ork-scheduler`,
    `bin/mini-ork-plan`, `bin/mini-ork-execute`, `bin/mini-ork-verify`
- `bin/mini-ork-init` + `mini_ork/ported/mini_ork_init.py` now write a relative
  `.mini-ork/engine` pointer and gitignore the generated state while committing
  the pointer.
- `lib/llm-dispatch.sh` now sources `lib/paths.sh` and pins `MO_TARGET_CWD` for
  executable wrappers.
- `lib/providers/cl_codex.sh` falls back to `MINI_ORK_TARGET_REPO`.

**Verification:**
- `bash -n` passes for all touched scripts.
- `tests/unit/test_mini_ork_init_py.py` passes (8/8).
- `tests/unit/test_mini_ork_cli_py.py` passes (6/6).
- Fresh `mini-ork init` in `/tmp/mini-ork-test-project` produced a working engine
  pointer and correct path resolution.

## FE-2: Provider preflight in doctor

- `bin/ork doctor` and `mini_ork_cli.py doctor` now run a provider preflight:
  - `claude` CLI presence for anthropic lanes.
  - `codex` CLI presence for codex lanes.
  - API-key env vars for glm/kimi/minimax/deepseek.
- Added `lib/paths.sh` to lib-presence check.

**Verification:** doctor parity test passes; direct `mini-ork doctor` shows the
new provider section.

## FE-3: `mini-ork validate` + `mini-ork garden`

- New commands:
  - `bin/mini-ork-validate` — kickoff Goal/Done-When, size cap, recipe schema,
    output-path collision, agents.yaml YAML validity, provider secrets.
  - `bin/mini-ork-garden` — output-path collisions, oversize prompts/workflows,
    stale runs (>30d), orphaned implementer stashes, env-var docs drift.
- Both wired into `bin/mini-ork` dispatch and `mini_ork_cli.py` `_EXEC_SUBS`.
- New doc: `docs/operator/env-vars.md` so garden stays clean.

**Verification:**
- `mini-ork validate examples/01-hello-world/kickoff.md` → OK.
- `mini-ork validate --recipe code-fix` → OK.
- `mini-ork garden` → 0 errors, 0 warnings.

## FE-4: Publisher output collision fix

- Changed recipe-specific output paths in:
  - `recipes/bug-audit-cmgk/artifact_contract.yaml`
  - `recipes/bug-audit-fe-be/artifact_contract.yaml`
  - `recipes/feature-inventory-cmgk/artifact_contract.yaml`
  - `recipes/refactor-audit/artifact_contract.yaml`
- `validate` and `garden` now detect output-path collisions across recipes.

## FE-5: AGENTS.md / CLAUDE.md map

- New root `AGENTS.md` (44 lines, harness-engineering style map).
- `CLAUDE.md` symlink → `AGENTS.md`.

**Note:** ingesting the target repo's `AGENTS.md`/`CLAUDE.md` into
planner/implementer context is not yet implemented; it requires changes to the
context assembler and is left as a follow-up framework-edit kickoff.

## FE-6: `mini-ork recipe-eval` (static layer)

- New command: `bin/mini-ork-recipe-eval` — static scoring of recipe definitions
  (manifest completeness, workflow nodes, verifiers, prompt/workflow size,
  example kickoff).
- Wired into `bin/mini-ork` and `mini_ork_cli.py`.

**Verification:**
- `mini-ork recipe-eval code-fix` → 95/100 (A).
- Full scan: 33 recipes, average 93.2/100.

## Tests updated

- `tests/unit/test_mini_ork_init_py.py` — updated gitignore expectation and
  task-class seeding count (34 recipes now provide `task_class.yaml`).

## Remaining work

- **Model tiers in workflow.yaml:** replace hard-coded `model_lane` names with
  tier aliases (`judgment|code|cheap-structured|executor`) resolved via named
  lane profiles. Requires workflow parser + recipe migration.
- **Target-repo AGENTS.md ingestion:** load `AGENTS.md`/`CLAUDE.md` into
  planner/implementer context via `lib/context_assembler.sh`.
- **Held-out eval integration:** connect `evals/heldout/` to
  `lib/benchmark_suite.sh` and feed outcomes into PRM/GRPO with confidence
  intervals.
