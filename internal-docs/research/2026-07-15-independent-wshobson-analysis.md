# Independent Analysis: What mini-ork Should Borrow from wshobson/agents

This is a fresh, independent read of [wshobson/agents](https://github.com/wshobson/agents)
(cloned 2026-07-15, shallow) mapped against the actual mini-ork codebase. It is not a
rehash of the 2026-07-02 research note; I re-verified every structural claim below
against live files in `/Volumes/docker-ssd/ps/mini-ork`.

## The fundamental difference

wshobson/agents is a **cross-harness content marketplace**: one markdown source is
transpiled into Claude Code / Codex / Cursor / OpenCode / Gemini native formats.
mini-ork is a **heterogeneous execution orchestrator**: it dispatches recipes across
multiple provider families, verifies artifacts, and learns from outcomes.

Because the jobs differ, mini-ork should not copy wshobson's transpilation model.
But wshobson's *engineering discipline* — source/generated separation, capability
matrix as code, mechanical gates with remediation hints, progressive disclosure,
declarative contracts, and a layered eval framework — is exactly the static
discipline mini-ork lacks. mini-ork has stronger runtime machinery; it needs the
pre-runtime and structural machinery to match.

## Verified findings

### 1. Path resolution is fragmented and self-inconsistent

wshobson's invariant: **one `WORKTREE` constant** in `tools/adapters/base.py`;
every path resolves from it. Generated artifacts are produced by adapters and
gitignored; only thin pointer registries are committed.

mini-ork's reality (verified):

| Entrypoint | `MINI_ORK_ROOT` default | `MINI_ORK_HOME` default |
|---|---|---|
| `bin/mini-ork` | `readlink -f "${BASH_SOURCE[0]}"` → repo root | set late inside `run`: `$(pwd)/.mini-ork` |
| `bin/mini-ork-epics` | `readlink -f "${BASH_SOURCE[0]}"` → repo root | `$MINI_ORK_ROOT/.mini-ork` (framework tree) |
| `bin/mini-ork-scheduler` | `readlink -f "${BASH_SOURCE[0]}"` → repo root | `$MINI_ORK_ROOT/.mini-ork` (framework tree) |
| `bin/mini-ork-plan` | `dirname "${BASH_SOURCE[0]}"` without `readlink` | `$(pwd)/.mini-ork` |
| `bin/mini-ork-execute` | `dirname "${BASH_SOURCE[0]}"` without `readlink` | inherited |
| `bin/mini-ork-verify` | `dirname "${BASH_SOURCE[0]}"` without `readlink` | `$(pwd)/.mini-ork` |
| `lib/providers/cl_codex.sh` | none | uses `MO_TARGET_CWD`/`PWD` for codex `-C` |

The result: depending on which entrypoint you run, state and runs land in the
target repo's `.mini-ork/` *or* the engine repo's `.mini-ork/`. `cl_codex.sh` ignores
both and roots its sandbox at the kickoff directory. This is the root cause of the
"foreign db writes / wrong-tree verify / codex sandbox rooted in kickoff dir"
failure pattern described in the TraceOtter run.

**Borrow from wshobson:** a single `lib/paths.sh` that exports `ENGINE_ROOT`,
`PROJECT_HOME`, and `TARGET_REPO`. Every script sources it. `mini-ork init` writes a
`.mini-ork/engine` pointer file in the target repo so external repos are first-class.

### 2. Provider config is partly structured, partly folklore

wshobson keeps a frozen `Capability` dataclass table in
`tools/adapters/capabilities.py`. It is consumed by every adapter, the docs
generator, and the eval scorer. Graceful degradation is mechanical.

mini-ork has:

- `config/agents.yaml` lines 59–94: a real capability map (`vision`, `tools`,
  `reasoning`, `search`) per family.
- `lib/providers/registry.sh`: a `providers.yaml` resolver.
- `lib/lane-helpers.sh::mo_assert_lane_capability`: runtime capability assertion.

But it also has:

- `lib/lane-helpers.sh::mo_lane_is_free` hard-coding `glm|kimi|minimax` as free.
- `config/agents.yaml` comments carrying policy ("glm is analysis-only", "gemini
  is banned").
- Recipes hard-coding lane names like `glm_lens`, `kimi_lens`, `codex_lens` in
  `workflow.yaml`.
- No preflight auth probe. The 402/429 class of provider failures is discovered
  mid-run.

**Borrow from wshobson:** make the capability table the single source of truth.
Add `code_capable`, `cost_tier`, and `auth_probe_endpoint` per provider. Replace
hard-coded lane names in recipes with model tiers (`tier: judgment|code|cheap-structured|`
executor) resolved through named lane profiles. Add a doctor preflight that does a
1-token live auth check per active lane.

### 3. There is no `validate` / `garden` command

wshobson ships three make targets wired into CI:

- `make validate` — structural checks on every generated artifact.
- `make garden` — drift detection (stale artifacts, oversize context files, dead
  links, marketplace orphans, skills over Codex's 8 KB cap).
- `make test` — 386 tests including real-CLI smoke.
Every finding includes a concrete `Fix:` string.

mini-ork has deep runtime verifiers but no pre-run/static validator. I verified
there is no `mini-ork-doctor`, `mini-ork validate`, or `mini-ork garden` command.
Checks are scattered:

- `bin/mini-ork-classify` enforces `MO_MAX_KICKOFF_BYTES=1MB`.
- `bin/mini-ork-plan` requires a verifier contract.
- `bin/mini-ork-execute` runs oracle gates before publish.
- `.pre-commit-config.yaml` optionally runs `check-jsonschema` on task_class and
  workflow YAML.

There is no unified place to catch: junk epics ingested from README headings,
oversized recipe prompts, orphaned `wip-pre-implementer-*` worktrees, stale
`state.db` entries whose kickoff path no longer exists, or missing env vars for
active lanes.

**Borrow from wshobson:** add `mini-ork validate` and `mini-ork garden` as
first-class commands, with `Fix:` hints on every finding, and wire them into CI.

### 4. Generated artifacts leak into target repos

wshobson commits only thin pointer registries. Generated trees are gitignored and
regenerated on demand.

mini-ork's publisher copies run artifacts into the target repo when a recipe's
`artifact_contract.yaml` declares `outputs[]`. I verified that four recipes all
publish to the same path:

- `recipes/bug-audit-cmgk/artifact_contract.yaml`
- `recipes/bug-audit-fe-be/artifact_contract.yaml`
- `recipes/feature-inventory-cmgk/artifact_contract.yaml`
- `recipes/refactor-audit/artifact_contract.yaml`

All target `docs/refactor/synthesis-latest.md`. This is a collision bug by itself,
and it caused the documented revert of a hallucinated finding into a target
repo's main branch.

**Borrow from wshobson:** default policy — run artifacts stay under
`PROJECT_HOME/runs/<run_id>/`. The target repo receives only the code diff the
run was asked to produce. Publishing any artifact into the target tree requires
an explicit `publish: docs/...` opt-in in the kickoff.

### 5. Progressive disclosure is not enforced

wshobson caps `AGENTS.md` at ~150 lines, skill bodies at 8 KB, and offloads detail
to `references/` loaded on demand. The doc gardener enforces these as CI rules.

mini-ork:

- Has no `AGENTS.md` at repo root at all.
- `lib/*.sh` totals **26,898 lines** of pre-injected bash context.
- Recipe markdown prompts total ~15,158 lines; YAML ~3,975 lines.
- Only one cap is enforced: kickoff ≤ 1 MB in `bin/mini-ork-classify`.

The "oversized kickoff silently truncates planner JSON" footgun is documented
but not mechanically prevented.

**Borrow from wshobson:** add size caps as `validate` rules (kickoff, recipe
prompt, workflow YAML, artifact size). Create a root `AGENTS.md` as a short map
pointing into `docs/`. Move long recipe guidance into per-recipe `references/`
loaded only by the node that needs it.

### 6. Recipe contracts exist but are not the extension API

wshobson's portable content contract is simple frontmatter:
`name`, `description` with a trigger phrase, `model:` as an alias tier, optional
`tools:` allowlist. Adapters map aliases to native IDs.

mini-ork has richer schemas — `task_class.schema.json`, `workflow.schema.json`,
`artifact_contract.schema.json`, `verifier_contract.schema.json` — but:

- Runtime parsing in `bin/mini-ork-execute` is ad-hoc, not schema-driven.
- Recipes hard-code lane names (`glm_lens`, `kimi_lens`, etc.) in `workflow.yaml`.
- `recipes/recipe-creator/verifiers/recipe-validator.sh` validates recipes, but
  validation is peripheral, not a first-class `mini-ork validate` command.
- mini-ork does not read a target repo's `AGENTS.md` / `CLAUDE.md` into context,
  even though every other harness treats that file as the repo's contract.

**Borrow from wshobson:** publish the recipe contract as the official extension
API. Replace concrete lane names with model tiers resolved via profiles. Add a
`mini-ork validate` schema check. When driving a target repo, load its
`AGENTS.md`/`CLAUDE.md` into planner/implementer context.

### 7. Eval pieces exist but are not integrated

wshobson's `plugin-eval` has three layers: static (<2s), LLM judge (~4 calls),
Monte Carlo (~50 calls), with confidence intervals, Elo corpus, and letter grades.

mini-ork has genuinely advanced runtime learning:

- Process Reward Model (`mini_ork/learning/process_reward.py`, `lib/process_reward.sh`).
- GRPO lane router (`lib/lane_router.sh`, `mini_ork/lane_router.py`).
- Budget/cost governance.

But:

- `evals/heldout/manifest.json` contains only **3 toy tasks** (word-count, flatten,
  fizzbuzz) with deliberately buggy `solution.py` files.
- The held-out set is isolated from `lib/benchmark_suite.sh` and the lane router.
- There is no static/CI-time scoring of recipe definitions.
- Judge verdicts feed reward with no calibration layer.

**Borrow from wshobson:** create `recipe-eval` with a static dimension
(manifest completeness, verifier coverage, prompt size, example kickoff present)
and reconnect `evals/heldout` to the benchmark suite so GRPO/PRM learn from real
outcomes, not just cost/success signals. Add confidence intervals before the
router acts on small samples.

## What mini-ork should not copy

- **Do not turn recipes into markdown transpilation targets.** mini-ork's YAML/
  JSON workflow model is appropriate for an execution engine. The lesson is the
  *discipline* of source/generated separation and declarative contracts, not the
  specific markdown format.
- **Do not weaken runtime verification.** wshobson has no equivalent to mini-ork's
  artifact contracts, verifier scripts, oracle gates, or rollback. These are
  strengths.
- **Do not replace the GRPO loop with a static tier table.** wshobson's model
  tiers are static aliases; mini-ork's per-task-class lane advantage is more
  sophisticated. The gap is calibration and CI-time scoring, not the learning
  mechanism itself.

## Concrete suggestions

### Immediate (one framework-edit kickoff each)

1. **`lib/paths.sh` single-resolution contract + `mini-ork init` for external repos**
   - Define `ENGINE_ROOT`, `PROJECT_HOME`, `TARGET_REPO`.
   - Replace scattered `BASH_SOURCE` defaults in every entrypoint.
   - `init` writes `.mini-ork/engine` pointer; external repos become first-class.

2. **`providers/capabilities.yaml` + doctor preflight + named lane profiles**
   - Move provider economics/failure modes out of comments and hard-coded helpers.
   - Add `code_capable`, `cost_tier`, `auth_probe_endpoint`.
   - Replace sed-editing of `agents.yaml` with `profiles/codex-minimax.yaml`,
     `profiles/full-panel.yaml`, etc.
   - `mini-ork doctor` runs a 1-token auth probe per active lane.

3. **`mini-ork validate` + `mini-ork garden` with `Fix:` hints, wired into CI**
   - Validate: kickoff shape/size, recipe schema, lane profile capability rules,
     secrets present, verifier contract present.
   - Garden: stale runs, orphaned worktrees, missing env-var docs, output-path
     collisions, oversize prompts, dead links.
   - Every finding ships `Fix: <command>`.

4. **Publisher policy: run artifacts never committed to target repo without opt-in**
   - Default `outputs: []` for audit/synthesis recipes.
   - Require explicit `publish: docs/...` in kickoff frontmatter to copy out of
     `PROJECT_HOME/runs/`.
   - Fix the four-recipe collision on `docs/refactor/synthesis-latest.md`.

5. **Model tiers in workflow.yaml + target-repo `AGENTS.md` ingestion**
   - `tier: judgment|code|cheap-structured|executor` resolved through profiles.
   - GRPO router remaps tiers without touching recipes.
   - Planner/implementer read target repo's `AGENTS.md`/`CLAUDE.md`.

6. **`recipe-eval` static dimension + held-out benchmark integration**
   - Static: manifest completeness, verifier coverage, prompt size, example kickoff.
   - Statistical: reconnect `evals/heldout` to `lib/benchmark_suite.sh` and feed
     outcomes into PRM/GRPO with confidence intervals.

### Suggested sequence

```
FE-1  lib/paths.sh + init                 (fixes targeting failures)
FE-2  capabilities table + doctor         (fixes silent provider deaths)
FE-3  validate + garden + CI              (fixes junk ingest / size footguns)
FE-4  publisher policy                    (fixes artifact leak + collision)
FE-5  model tiers + AGENTS.md ingestion   (fixes hard-coded lanes, adds integratability)
FE-6  recipe-eval + heldout integration   (fixes uncalibrated GRPO inputs)
```

## Sources

- wshobson/agents cloned 2026-07-15 to `/tmp/wshobson-agents-analysis` (shallow).
- Key files read independently: `ARCHITECTURE.md`, `AGENTS.md`, `Makefile`,
  `tools/adapters/base.py`, `tools/adapters/capabilities.py`,
  `tools/doc_gardener.py`, `tools/validate_generated.py`, `docs/harnesses.md`,
  `docs/plugin-eval.md`, and sample plugins.
- mini-ork files verified live: `bin/mini-ork*`, `lib/config_resolve.sh`,
  `lib/lane-helpers.sh`, `lib/providers/registry.sh`, `lib/providers/cl_codex.sh`,
  `config/agents.yaml`, `recipes/*/workflow.yaml`, `recipes/*/artifact_contract.yaml`,
  `schemas/*.schema.json`, `evals/heldout/manifest.json`, `bin/mini-ork-execute`.
