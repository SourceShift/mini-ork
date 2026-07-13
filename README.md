# mini-ork

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/SourceShift/mini-ork?label=release&color=green)](https://github.com/SourceShift/mini-ork/releases/latest)
[![CI](https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml)
[![Status](https://img.shields.io/badge/status-early%20preview-orange.svg)](ROADMAP.md)

![A friendly modern red master orc coordinates peaceful mini-ork bubble worlds inside a spaceship, with tiny green mini-orc agents collaborating through connected workspaces.](assets/mini-ork-hero.jpg)

> **Motto:** Stop drawing agent graphs that let one model family grade its own homework. mini-ork turns goals into verifier-gated, stateful, cross-family agent runs, so teams get durable artifacts instead of same-vendor consensus theater.

**mini-ork is a task operating system for agents.** It receives a goal, classifies the work, dispatches specialized agents across *distinct model families*, verifies artifacts deterministically, and stores execution experience so every run starts smarter — and cheaper — than the last. Pipeline shapes live in user-land [recipes](recipes/); the framework ships the loop, not the opinions.

> ⚡ **30-second demo (no API keys):** `bash examples/00-demo.sh` — bootstraps a throwaway project, walks the classify → plan → execute → verify loop in dry-run mode (no LLM calls), prints the dispatched node sequence + the plan path that *would* be written. Set `MINI_ORK_DRY_RUN=0` to fire real LLM calls and populate `task_runs`.

📖 **Full feature catalog:** [docs/FEATURES.md](docs/FEATURES.md) · **Architecture:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## How it works

```
classify → plan → execute → verify → reflect → improve
```

A goal enters as a kickoff. The dispatcher classifies it (task class, risk, artifact + verifier contracts), plans a DAG, executes nodes by assigning each lane to a model family, gates the output through deterministic verifiers, extracts textual gradients from the trace, and promotes only workflow changes that beat a benchmark under budget.

Three load-bearing ideas:

- **Cross-family review independence.** Lenses dispatch to distinct model families (Anthropic, Zhipu, Moonshot, OpenAI, DeepSeek, MiniMax) by configuration — not prompt-level nudging. Same-family panels share blind spots; mini-ork's coalition gate hard-blocks same-family degeneration so review independence stays a structural property, not a hopeful one.
- **Executable verification before opinion.** Every recipe ships `verifiers/*.sh`. Pass/fail is an exit code, not an LLM judgment. Empty verification is marked `vacuous`, never silently "success" — what a test suite can decide shouldn't cost tokens.
- **Persistent trajectory memory.** `state.db` stores runs, gradients, lineage, agent performance, and cost. The planner sees the last N same-class runs (failures, cost, duration). Yesterday's lesson is today's context — across sessions, without vendor memory.

📖 **Deeper writeup:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — schema, lanes, gates, memory namespaces, dispatch mechanics.

---

## Quickstart

Prereqs: `bash` 4+, `sqlite3`, `jq`, `yq`, `git` 2.28+. Agents dispatch through vendor CLIs ([`claude`](https://docs.anthropic.com/en/docs/claude-code) and/or [`codex`](https://github.com/openai/codex)), not raw API keys — install + auth the CLIs your lanes need before any real run. Dry-run mode (`MINI_ORK_DRY_RUN=1`) needs neither.

```bash
# 1. Install (creates symlink in $HOME/.local/bin or /usr/local/bin)
bash install.sh

# 2. Initialize a project (creates .mini-ork/ + seeds state.db + task_classes)
cd ~/my-project
mini-ork init
mini-ork update          # refresh schema after framework updates

# 3. Write a kickoff (or copy an example)
cp <path-to-mini-ork-checkout>/examples/01-hello-world/kickoff.md ./kickoff.md

# 4. Dry-run first (no API keys needed)
MINI_ORK_DRY_RUN=1 mini-ork run ./kickoff.md

# Or pick a recipe explicitly
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md

# 5. Real run — `claude` CLI must be authenticated
mini-ork run ./kickoff.md
```

`bin/mini-ork run` exits 0 on verified artifact, 1 on gate failure or escalation. State lives in `${MINI_ORK_DB}` (default: `.mini-ork/state.db`). Inspect with:

```bash
sqlite3 .mini-ork/state.db "SELECT id, task_class, recipe, status, verdict FROM task_runs ORDER BY created_at DESC LIMIT 5;"
```

Verify your environment + lane wiring before your first real run:

```bash
./bin/mini-ork doctor      # dep probe, lane probe, secret probe
```

---

## Setup credentials

mini-ork dispatches agents through vendor CLIs; it only needs provider keys when you point a lane at a non-CLI wrapper. Secret resolution is **repo-local first**: put keys in `config/secrets.local.sh` (gitignored, see [config/secrets.example.sh](config/secrets.example.sh) for the template) — never commit them.

```bash
# Default lane config (config/agents.yaml) routes planner/worker/verifier/reviewer
# through `claude` CLI's ambient auth — no key needed for those lanes.
#
# Add keys only for lanes that point at non-Claude providers:
export GLM_API_KEY=...        # Zhipu (cl_glm.sh wrapper)
export KIMI_API_KEY=...       # Moonshot (cl_kimi.sh)
export MINIMAX_API_KEY=...    # MiniMax (cl_minimax.sh)
export DEEPSEEK_API_KEY=...   # DeepSeek (cl_deepseek.sh)
# OpenAI is handled by the `codex` CLI's own login — no env key needed.
```

**Vendoring mini-ork into an existing repo:** mini-ork keeps its own state in that repo's `.mini-ork/` directory and reads its framework root from `MINI_ORK_ROOT` (defaults to the symlink target of `bin/mini-ork`). When dispatching from a *foreign* repo that has its own `.mini-ork/` but no `config/secrets.local.sh`, export `MINI_ORK_SECRETS=<path-to-originating-repo>/config/secrets.local.sh` so the dispatch inherits the originating repo's secret file. Without this, foreign-home runs silently fail at the first non-Claude lens.

**Sandbox opt-in:** `MO_RUNTIME_BACKEND` switches the runtime exec seam between `local` and `bubblewrap` (subdir sandbox). Default is `local`; set `MO_RUNTIME_BACKEND=bubblewrap` to opt into the subprocess sandbox. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the contract.

---

## Integrate into your agentic CLI (Claude Code / Codex / others)

mini-ork is a worker, not a replacement. Compose it under whatever agentic CLI you already drive.

**Lane → model-family mapping** lives in [config/agents.yaml](config/agents.yaml). The shipped defaults:

| Lane role | Default family | Provider wrapper |
|---|---|---|
| `planner` / `worker` / `verifier` | Anthropic Sonnet | `cl_sonnet.sh` (uses `claude` CLI ambient auth) |
| `reviewer` | Anthropic Opus | `cl_opus.sh` (uses `claude` CLI ambient auth) |
| `codex_lens` | OpenAI Codex | `cl_codex.sh` |
| `glm_lens` | Zhipu GLM | `cl_glm.sh` |
| `kimi_lens` | Moonshot Kimi | `cl_kimi.sh` |
| `minimax_lens` | MiniMax M3 | `cl_minimax.sh` |
| `deepseek` (decomposer) | DeepSeek | `cl_deepseek.sh` |

7 model-family wrappers ship under [lib/providers/](lib/providers/) — `cl_{claude,sonnet,opus,codex,kimi,glm,deepseek,minimax}.sh` (sonnet/opus wrap `claude`; the rest are direct-API wrappers).

**From a master Claude Code / Codex session**, treat mini-ork as a CLI worker:

1. Read the user's goal. Pick or compose a recipe (start with `code-fix` for targeted patches, `framework-edit` for multi-file self-modification, `research-synthesis` for literature jobs, `recursive-self-improve` for bounded self-iteration).
2. Write a kickoff markdown (the recipe's `examples/` shows the shape).
3. Dispatch: `mini-ork run <recipe> ./kickoff.md` from the project root. `bin/mini-ork run` returns when the artifact is verified (or when a gate fails and the run halts).
4. Monitor via `state.db` (sql) or the optional UI (`mini-ork serve` → `http://127.0.0.1:7090`).
5. Read artifacts: plans land in `.mini-ork/runs/<run_id>/plan.json`, verifier output next to it.

For **multi-recipe roadmaps**, use `mini-ork epics` (ingest + split) + `mini-ork scheduler` (autonomous dispatch) + `mini-ork review` (pre-push code reviewer with fix-loop). See [docs/EXTENSION.md](docs/EXTENSION.md) for adding your own recipes, lanes, or verifiers.

### Install the Claude Code skill (recommended)

mini-ork ships an **Agent Skill** at [skills/mini-ork/SKILL.md](skills/mini-ork/SKILL.md) so a
skill-aware CLI (Claude Code, and other agents that read `SKILL.md`) drives the orchestrator
*correctly* instead of improvising flags, paths, or recipes. It encodes the SAFE-USAGE CONTRACT
(cwd/target isolation, `MINI_ORK_ROOT`, kickoff sizing) that prevents the top failure modes —
cross-repo corruption, hollow/truncated plans, silent lane stalls.

Install it once (user-level, available in every project):

```bash
# user scope — all repos
mkdir -p ~/.claude/skills && cp -r skills/mini-ork ~/.claude/skills/

# or project scope — this repo only
mkdir -p .claude/skills && cp -r skills/mini-ork .claude/skills/
```

Then it auto-triggers on prompts like *"run this kickoff"*, *"dispatch a framework-edit / code-fix"*,
*"ingest this roadmap / run the scheduler"*, or *"what recipe should I use"* — the agent loads the
skill and drives `bin/mini-ork` with the right cwd, lanes, and recipe.

---

## Recipes

A recipe is `workflow.yaml` + `prompts/` + `verifiers/` + an `artifact_contract`. It's the user-land unit of pipeline shape — fork it, rewrite it, ship your own. **28 recipes ship today**; `mini-ork run <recipe> <kickoff>` picks one by name.

### RECIPES

| Recipe | Shape |
|---|---|
| `code-fix` | Single-patch fix with typecheck, test, reviewer gates. Minimal reference recipe. |
| `bdd-first-delivery` | BDD-first multi-epic: decompose → parallel (spec_author + implementer) → bdd_runner → reviewer → publisher. |
| `docs` | Single-doc edit verified by grep-pattern assertions + relative-link integrity. |
| `refactor-audit` | 4 lens stances in parallel (glm/kimi/codex/opus); mini-ork's own self-audit recipe. |
| `research-synthesis` | 4-lens research synthesis (web/lit/code/narrative) → synthesizer → publisher. |
| `post-mvp-delivery` | Discovery-first post-MVP product delivery: parallel research → options → user-gate → implementation. |
| `recursive-self-improve` | Wall-clock-budgeted self-improvement loop for mini-ork itself. Outer driver: `bin/mini-ork-self-improve`. |
| `blog-post` | 5-lens blog drafting (editor / researcher / narrative / audience / counter) in parallel. |
| `db-migration` | 5-lens migration audit + plan: integrity / rollback / perf / compat / edge-data. |
| `ops-runbook` | 5-lens runbook generation: detection / containment / diagnosis / recovery / prevention. |
| `ui-audit` | 5-lens UI audit: a11y / perf / visual / interaction / edge-cases. |
| `obs-smoke` | Cheap 2-node observability smoke that touches every emit surface. |
| `recipe-creator` | Meta-recipe: NL epic → complete `recipes/<derived>/` via 3-family drafter panel + opus arbiter. |
| `silent-catch-audit` | First recipe authored by `recipe-creator`. 3-lens audit of TS/JS for silent `.catch(() => {})`. |
| `framework-edit` | Recipe-creator-authored. Routine mini-ork self-modification: planner → glm implementer → verifiers → opus reviewer. **Mandatory dispatch path for every 2+ file change in this repo.** |
| `blog-cohesion` | Multi-lens cohesion audit for long-form blog drafts. GLM thesis + 4 Sonnet reviewers → Opus arbiter. |
| `feature-inventory-cmgk` | Refactor-audit variant tuned for capability enumeration. 4 distinct family lenses (codex/glm/kimi/minimax). |
| `bug-audit-cmgk` | Refactor-audit variant tuned for bug enumeration with file:line anchors and severity tiers. |
| `bug-audit-fe-be` | FE+BE bug audit: kimi contract-violation + minimax user-impact → opus synthesis. |
| `chapter-review` | Multi-axis panel review of a book chapter by 4 heterogeneous LLM lenses. |
| `researcher-qdrant-contract` | PG/Qdrant indexing and retrieval contract remediation. |
| `schema-judge-panel` | Five-lens read-only judge panel for database/codebase architecture plans (2 Opus + Kimi + Codex + MiniMax). |
| `epic-runner` | Multi-epic delivery orchestrator. Ingests an epic markdown, walks it in topological waves. |
| `doc-to-features-loop` | Document-driven feature extraction loop with cross-feature dependency tracking. |
| `recursive-validate-impl` | Recursive implement → 5-tier validate → reflect → replan. arxiv-compliance cross-reference at tier 4. |
| `harness-bridge` | Wraps a full coding-agent harness (claude-code / codex-cli / gemini-cli) as a workflow node. |
| `chapter-validation-10lens` | 10 parallel lens agents each judge one slice of chapter validation. |
| `mo-vs-omnigent` | Head-to-head mini-ork vs [omnigent](https://github.com/omnigent-ai/omnigent) across 4 distinct lens families. |

Add your own under `recipes/<name>/` — see [docs/EXTENSION.md](docs/EXTENSION.md).

---

## Extension Points

1. **WorkflowGraph** — add nodes + edges via `workflow.yaml` in your recipe. Schema: [schemas/workflow.schema.json](schemas/workflow.schema.json).
2. **AgentRegistry** — register new roles / model bindings via `lib/agent_registry.sh:agent_register`. No code change.
3. **VerifierRegistry** — drop a `<name>.sh` under `recipes/<recipe>/verifiers/` and reference it in `workflow.yaml`.
4. **ExperienceMemory** — add namespaces via DB migrations or override `lib/context_assembler.sh` per task class.

Python embed is first-class: `MiniOrk().run(RunRequest(...))` — see [docs/PYTHON_FRAMEWORK.md](docs/PYTHON_FRAMEWORK.md).

---

## Bounded Autonomy

Self-improvement is evidence-gated, not free-running. Mutations are ranked by risk; auto-promotion is **class-restricted** — task classes with an external oracle (tests, schemas) can auto-promote on green; LLM-judged classes (synthesis, audits) are manual-promote-only. The framework refuses to fabricate an oracle it doesn't have. See [docs/SAFETY.md](docs/SAFETY.md) for the PromotionGate contract and immutable constraints.

Cost guard: `MO_DAILY_BUDGET_USD` ($50 default) is enforced inside the dispatcher; over-budget calls are refused, not billed. The self-improve runner re-checks the same cap before each iteration.

---

## Roadmap

**Current: v0.3.0-rc2** — observability, security, and reliability hardening on top of v0.3 oracle-hardening primitives (`coalition_gate.sh`, `cw_por.sh`, `mo_promote_synthesis_gate`, `adaptive_stability.sh`, `circuit_breaker.sh`). Full per-commit release log: [ROADMAP.md](ROADMAP.md).

Shipped totals (regenerable via `scripts/readme-claim-check.sh`):

- **91 framework primitives** in `lib/` (lifecycle + memory + gates + agent registry + runtime + observability + the 4 calibration-list gates + HarnessBridge stack + live control plane + per-run config isolation + `lib/migrate.sh` checksummed transactional DB migrations + `lib/runtime-select.sh` bash→Python runtime cutover switch + `lib/apply.sh` learn→apply loop).
- **33 user-facing `bin/mini-ork` entrypoints** (the `mini-ork` dispatcher + the 6-stage loop + `eval` / `improve` / `promote` / `apply` / `metrics` / `spawn` / `epics` / `scheduler` / `review` / `bugs` / `lifetime` / `coord` / `usage-report` / `watchdog` / `conductor` and friends).
- **50 schema migrations** under `db/migrations/` (memory namespaces, execution traces, gradients, panel topology, recursive orchestration, llm_calls, lifecycle widening, error taxonomy, heartbeat, agent performance, epic + bug + pre-push review tables, HarnessBridge grounded-rejections, run-artifacts trajectory store, apply-attempts audit table, lane-advantage variance + single-sample baseline).
- **28 recipes** shipped — see [Recipes table](#recipes) above.
- **7 model-family wrappers** under [lib/providers/](lib/providers/) + BYO-key registry (`config/providers.yaml`) for custom Anthropic/OpenAI-compatible endpoints.

---

## Recursive self-improvement evidence

The `recursive-self-improve` recipe ran against mini-ork itself for ~5 wall-clock hours, producing **10 autonomous commits to `main`** — each grounded in cited arXiv evidence per the recipe's "new infra requires arXiv evidence" hard rule. Audit trail: `self_improve_runs`, `learning_record`, `self_improve_arxiv_refs` tables in `state.db`. The first three patches were emergent — the loop found the bugs by reading its own prior run logs. See [docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md) for the operator guide.

---

## Dependencies

`bash` 4+ · `sqlite3` 3.35+ (WAL) · `jq` 1.6+ · `yq` 4+ · `git` 2.28+ · `claude` CLI 2.1+ · `codex` CLI (optional, for `codex` lane).

Run `./bin/mini-ork doctor` after installation to verify every dep is present + reachable.

---

## License

**Apache-2.0** — see [LICENSE](LICENSE). Copyright © 2026 Amir Khakshour.

Use it in anything, including commercial and closed-source products. You owe
nothing but the attribution the license already asks for. Published versions are
irrevocable — they cannot be un-published or retroactively relicensed.

Contributing? See the [contributor grant](CONTRIBUTING.md#license) — you keep your
copyright; there is nothing to sign.