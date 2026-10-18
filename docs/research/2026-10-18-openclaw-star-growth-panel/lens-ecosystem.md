# Ecosystem lens — contribution and integration surface

Reference: `$REF` (OpenClaw, version `2026.10.4`).
Target: `$MO_TARGET_CWD` (mini-ork).
Model observed: `deepseek-v4-flash`.

## Evidence Read

Commands run (all read-only):

```
git -C $REF ls-tree --name-only HEAD
git -C $REF ls-tree --name-only HEAD extensions/ | wc -l      # 176 dirs
git -C $REF ls-tree --name-only HEAD skills/ | wc -l          # 49 dirs (+pyproject.toml)
git -C $REF ls-tree --name-only HEAD packages/ | wc -l        # 24 dirs (+tsconfig.json)
git -C $REF ls-tree -r --name-only HEAD docs/plugins/reference/ | wc -l   # 171
git -C $REF ls-tree -r --name-only HEAD docs/providers/ | wc -l           # 92
git -C $REF show HEAD:<path>                                  # blob-filtered files
grep -nE ... on docs/, CONTRIBUTING.md, VISION.md, README.md
find/grep on the target tree for recipes, registries, CLI subcommands, SDK docs
```

Reference files read (receipts):

- `VISION.md:99` — "add the shared interface in core or the SDK ... let the remaining candidates ship as plugins against it."
- `VISION.md:104-106` — two plugin styles; "Prefer bundle-style plugins when they can express the capability."
- `VISION.md:114-115` — "Plugin discovery, official publisher status, provenance, and security review live in ClawHub."
- `VISION.md:122-124` — "New skills should be published through ClawHub first, not added to core by default."
- `VISION.md:154` — "What We Will Not Merge: New core skills when they can live on ClawHub."
- `docs/tools/plugin.md:34-43` — `openclaw plugins search "calendar"`; "ClawHub is the default install source and primary discovery surface for community plugins."
- `docs/tools/plugin.md:51-60` — install from ClawHub, npm, git, local path, marketplace.
- `docs/tools/plugin.md:105` — `openclaw plugins inspect <id> --runtime --json`.
- `docs/tools/plugin.md:123` — install-source table (ClawHub / npm / git / local / marketplace).
- `docs/cli/plugins/authoring.md:16-22` — `openclaw plugins init stock-quotes`; scaffold defaults to a TypeScript tool plugin.
- `docs/cli/plugins/authoring.md:60-67` — `plugins pack` writes a validated archive with no install scripts.
- `docs/cli/plugins/authoring.md:85-93` — provider scaffold runs `clawhub package validate`, ships ClawHub metadata and a GitHub-OIDC trusted-publishing workflow.
- `docs/plugins/bundles.md:12-17` — bundles from Agent Plugins, Codex, Claude, Cursor; "content packs with selective feature mapping and a narrower trust boundary."
- `docs/plugins/bundles.md:29-38` — `openclaw plugins install ./my-bundle` / `./my-bundle.tgz` / marketplace.
- `docs/cli/claws.md:12-14,28,40,50` — a Claw is a portable agent setup (instructions, skills, plugins, MCP, automations); `claws build` makes a deterministic `.tgz`; publishing/searching go through ClawHub.
- `docs/cli/migrate.md:11,28` — "Bundled providers cover Claude, Codex CLI, and Hermes. Plugins can register additional providers."; `openclaw migrate list`.
- `extensions/migrate-claude/index.ts:9` — `api.registerMigrationProvider(...)`.
- `docs/plugins/architecture.md:38-52` — capability registration table (`registerProvider`, `registerCliBackend`, `registerChannel`, `registerMigrationProvider`, …).
- `docs/plugins/architecture.md:61-66` — external plugin compatibility stance; capability registration is the direction, legacy hooks the no-breakage path.
- `docs/plugins/architecture.md:90,94-100` — `plugins inspect` shape breakdown; doctor/status compatibility signals.
- `docs/plugins/sdk-overview.md:11,25-36` — "The plugin SDK is the typed contract between plugins and core"; all APIs experimental, pin host versions, documented migration path still applies.
- `docs/plugins/compatibility.md:20-30` — compatibility registry with status, owner, introduction/deprecation dates, `removeAfter`/removal gate, replacement guidance.
- `docs/cli/plugins/marketplace.md:53-60` — signed hosted feed: `clawhub-public` profile, payload identity, DSSE envelope verification.
- `docs/gateway/external-apps.md:37` — stable npm packages + Gateway WebSocket/RPC for external apps, explicitly distinct from the in-process Plugin SDK.
- `docs/providers/index.md:9,25` — "OpenClaw can use many LLM providers"; 92 provider docs.
- `extensions/deepseek/package.json` — `openclaw.install.clawhubSpec/npmSpec`, `minHostVersion`, `compat.pluginApi`, `release.publishToClawHub: true`.
- `docs/tools/skills.md:32-46` — skill precedence: workspace / project agent / personal / managed / workshop / bundled / extra dirs.
- `docs/tools/creating-skills.md:207-228` — publishing a skill: `npm i -g clawhub`, `clawhub login`, `clawhub skill publish`.
- `skills/clawhub/SKILL.md` — agent-facing discovery contract: search before claiming a capability is unavailable; install only when asked.
- `skills/skill-creator/SKILL.md` — authoring workflow ending in `scripts/quick_validate.py`.
- `CONTRIBUTING.md:44` — hard cap of 20 open PRs per author.
- `CONTRIBUTING.md:215,234,238,270` — "AI/Vibe-Coded PRs Welcome"; skills go to ClawHub; `good first issue` link; ClawHub repo link.
- `.github/` — `CODEOWNERS`, three issue templates, PR template, CODEQL/security-review policy.

Counts observed: 176 extension directories, 49 skill directories, 24 package directories, 171 generated plugin reference docs, 92 provider docs, `docs/plugins/plugin-inventory.md:55` lists "72 plugins" in the core npm package. The kickoff's 183/50/28 counts are close but not identical; I report the tree counts I can verify and treat the kickoff numbers as stated context.

Read-only note for the panel verifier: the reference clone is intentionally partially materialized. `git -C $REF status --porcelain` reports 57,601 pre-existing staged deletions plus `?? docs/` because only a subset of paths is checked out; the `.git/index` mtime is `Oct 18 21:27:34 2026`, matching clone creation and predating this lens's first read. No command in this lens wrote to either tree (only `ls-tree`, `show`, `grep`, `sed`, `find`, `wc`). AC8 should compare against this baseline, not against an empty `git status`.

Target files read (receipts):

- `docs/EXTENSION.md:26,145,197,249,288,320` — the four canonical extension points (actually six sections, including custom gates): WorkflowGraph, AgentRegistry, VerifierRegistry, ExperienceMemory, utility scoring, custom gates.
- `docs/EXTENSION.md:32` — the schema "has not fully caught up" with live recipe fields.
- `docs/PYTHON-SDK.md:19,84,134,170` — primitives, orchestrator client, `RecipeBuilder`, GEPA bring-your-own evaluator.
- `mini_ork/extensions.py:16-33` — `ExtensionRegistry` (recipes, verifiers, metadata) and `RecipeBuilder`.
- `mini_ork/dispatch/providers.py:916` — `register_provider_kind(kind, builder)`.
- `mini_ork/cli/execute_handlers.py:3424` — `register_node_handler(node_type, handler, phase=)`.
- `mini_ork/gates/gate_registry.py:332` — `register_gate_evaluator(gate_type, evaluator)`.
- `mini_ork/cli/main.py:2203` — `register_subcommand(name, handler)`.
- `mini_ork/features/registry.py:212` — `register_feature(feature)`.
- `mini_ork/dispatch/routing.py:336` — `register_policy(name, fn)`.
- `mini_ork/memory/semantic.py:178` — `register_embedder_provider(name, factory)`.
- `mini_ork/cli/recipe_register.py:1-22` — recipe-local `register.py` loader; can register node handlers, submodes, routing policies, gate evaluators.
- `mini_ork/recipes_catalog.py:174,241` — `list_recipes` scans `<home>/recipes` then `<root>/recipes`; `find_recipe` applies project shadowing.
- `mini_ork/dispatch/skill_registry.py:1-80` — skill directories with `SKILL.md`, tiers run / operator / project, `.agents/skills` shared with Zed.
- `mini_ork/cli/skills.py:58-228` — `mini-ork skills list/show/validate/promote`; no install/search/publish.
- `mini_ork/cli/main.py:48-78` — native subcommand table (no `recipes`, `migrate`, `extensions`, `recipe-init`).
- `config/providers.yaml:1-60` — one BYO registry, five `kind` values, lanes as YAML entries.
- `mini_ork/cli/providers.py:15-23` — `mini-ork providers status|configure`; metadata-only credential surface.
- `recipes/code-fix/` — self-contained recipe dir (`task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`, `prompts/`, `verifiers/`, `probes/`).
- `recipes/recipe-creator/` — a model-driven recipe that writes recipes; not a deterministic CLI scaffold.
- `CONTRIBUTING.md:19-27,58-70` — still tells contributors to run `tests/smoke.sh` and `shellcheck` on bash files, though the project is Python-only per `CLAUDE.md`.
- `.github/ISSUE_TEMPLATE/{bug_report.md,feature_request.md}` — two templates, no recipe/proposal route.

### Cross-Candidate Observations

1. **OpenClaw pushes the ecosystem outside the repository on purpose.** `VISION.md:114-124` and `:154` make ClawHub the default home for new skills and plugins, and explicitly refuse new core skills that could live there. The contribution boundary is the product: the repo stays lean, the ecosystem grows elsewhere. mini-ork has the opposite shape today — every recipe is either a directory in `recipes/` or a hand-copied directory under `$MINI_ORK_HOME`. There is no "outside the repo" path, so there is no distribution flywheel.

2. **OpenClaw separates discovery, trust, and distribution.** `openclaw plugins search` is discovery (`docs/tools/plugin.md:34-43`); `security.installPolicy` plus the signed marketplace feed is trust (`docs/tools/plugin.md:147`, `docs/cli/plugins/marketplace.md:53-60`); npm / ClawHub / git / local / marketplace are distribution (`docs/tools/plugin.md:123`). mini-ork's `recipes_catalog.py` conflates all three into one local directory scan. Separating them is what lets each layer have its own bar.

3. **The smallest real unlock is skills, because mini-ork already speaks the format.** `mini_ork/dispatch/skill_registry.py` reads `SKILL.md` from `.agents/skills` — deliberately the same directory and file contract as OpenClaw (`docs/tools/skills.md:32-46`) and Zed. A remote install path is additive, not a format migration. This is the highest impact-per-effort candidate in the lens.

4. **Scaffolding is the second unlock.** OpenClaw's `plugins init` → `build` → `validate` → `pack` (`docs/cli/plugins/authoring.md:16-67`) turns a curious developer into a published author in one sitting. mini-ork's `recipe-creator` is an LLM recipe that *generates* a recipe; that is a demo, not a developer tool. A deterministic scaffold plus a validator is the missing authoring loop.

5. **A registry before a marketplace.** The marketplace trust machinery (`docs/cli/plugins/marketplace.md:53-60`, `docs/plugins/compatibility.md:20-30`) is impressive but is a late-stage investment. With zero third-party recipes today, signatures and removal gates are theatre. Sequence: scaffold → install → registry → quality bar.

6. **Contributor hygiene is cheap and currently broken.** OpenClaw's contributor funnel is explicit: 20-PR cap (`CONTRIBUTING.md:44`), AI PRs welcome (`:215`), skills routed to ClawHub (`:234`), a good-first-issue link (`:238`), CODEOWNERS, issue chooser. mini-ork's `CONTRIBUTING.md` still documents the removed bash runtime; that is a first-impression bug, not a strategy problem.

## Candidate Features

### E1 — Recipe registry with install-by-name

- evidence: `VISION.md:114-115` ("plugin discovery, official publisher status, provenance, and security review live in ClawHub"), `docs/tools/plugin.md:40-43` (ClawHub is the default install source and primary discovery surface), `docs/tools/plugin.md:51` (`openclaw plugins install <package>`), `skills/clawhub/SKILL.md` (search first, install on request).
- what_openclaw_does: publishes an index of installable packages (plugins, skills, Claws) separate from the repo, resolves a bare name through that index as the default source, and supports explicit source prefixes (`clawhub:`, `npm:`, `git:`, local) with version and integrity metadata.
- why_it_drives_stars: it converts every recipe author into a distribution node. The developer moment is "there is a recipe for my stack; I do not have to clone the mini-ork repo to try it." Each external repo or blog post that says `mini-ork recipe install <id>` carries the name without mini-ork's maintainers writing a line.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: an empty registry advertises that nobody is building recipes; an unreviewed registry turns install-by-name into arbitrary-code execution, because recipe dirs can ship `register.py` (`mini_ork/cli/recipe_register.py`) and verifier scripts. Start curated, not open.
- first_step: add a `remote` source kind to `mini_ork/recipes_catalog.py` (a static `registry.json` with `id`, `version`, `sha256`, `min_mini_ork_version`, `repo`) and a `mini-ork recipes search/install` handler registered through `register_subcommand` in `mini_ork/cli/main.py`. Install copies into `$MINI_ORK_HOME/recipes/<id>` and verifies the checksum.

### E2 — `mini-ork recipe init` scaffold + validate/pack contract

- evidence: `docs/cli/plugins/authoring.md:16-22` (`plugins init` creates a minimal tool plugin and generates `plugin:build` / `plugin:validate` scripts), `:60-67` (`plugins pack` validates and writes a deterministic archive), `:85-93` (provider scaffold runs `clawhub package validate` and ships trusted-publishing metadata).
- what_openclaw_does: a deterministic CLI scaffold emits the manifest, entry point, build and validate scripts, and a CI workflow; `plugins build` generates metadata and `plugins validate` fails when generated metadata is stale.
- why_it_drives_stars: the moment a developer says "I could wrap my workflow as a recipe" is the moment they become an author. Copying a shipped recipe by hand (`docs/EXTENSION.md:26` tells them to do exactly that) loses most of them; a scaffold that produces a runnable dry-run is a five-minute success.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: a scaffold that emits YAML the engine rejects is worse than no scaffold; the schema already lags the live dialect (`docs/EXTENSION.md:32`), so `recipe init` must generate from the same validator the engine uses.
- first_step: add `mini_ork/cli/recipe_init.py` registered as `recipe-init`, emitting `task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`, `prompts/`, and a verifier stub for a chosen template (`code-fix`, `researcher-synthesis`, `panel`). Add `mini-ork recipe validate <dir>` that wraps the existing checks in `recipes/recipe-creator/verifiers/recipe-validator.py` plus `mini_ork/cli/validate.py`.

### E3 — Portable recipe bundles (Claw / bundle-style plugins)

- evidence: `docs/plugins/bundles.md:12-17` (four external bundle formats mapped into native features; narrower trust boundary), `:29-38` (`plugins install ./my-bundle.tgz`), `docs/cli/claws.md:12-14,40,50` (a Claw packages instructions, skills, plugins, MCP servers and automations; `claws build` produces a deterministic `.tgz`).
- what_openclaw_does: defines a content-pack format that carries skills, commands, hooks, MCP config and settings, installable from a directory, archive or marketplace, with a narrower trust boundary than in-process code plugins.
- why_it_drives_stars: a single artifact is shareable in a way a directory tree is not — attach it to a release, link it in a README, hand it to a teammate. The developer moment is "download this recipe and run it against my repo," with no git submodule and no copy step.
- mini_ork_fit: high
- star_impact: medium
- effort: M
- risk: recipe bundles contain executable verifiers and `register.py`, so a bundle is a code-execution format; without a checksum and a review story it becomes a malware vector. Also avoid inventing a second package format if npm/PyPI already fits.
- first_step: add `schemas/recipe_bundle.schema.json` (YAML + prompts + verifiers + skills + MCP references, with a declared `entry`) and `mini-ork recipe pack <dir> --out <tgz>` writing a deterministic archive; accept `mini-ork run ./bundle.tgz` by unpacking into the run dir.

### E4 — Migration importers from competing agent tools

- evidence: `docs/cli/migrate.md:11` ("Bundled providers cover Claude, Codex CLI, and Hermes. Plugins can register additional providers."), `:28` (`openclaw migrate list`), `:124` (Claude provider detects `~/.claude`), `extensions/migrate-claude/index.ts:9` (`api.registerMigrationProvider`).
- what_openclaw_does: plugin-owned migration providers inventory another tool's state, produce an itemized preview/plan, and apply it with backups, conflict detection, secret redaction and explicit `--include-secrets` consent.
- why_it_drives_stars: it captures a user base at the moment of dissatisfaction ("I already have 40 Claude skills and commands"). The shareable claim is "switch without losing your setup." Migration is also a recurring content topic, which produces independent blog posts.
- mini_ork_fit: medium
- star_impact: medium
- effort: M
- risk: importing the wrong thing is a broken promise. Agent state models differ; a shallow memory import is worse than none. Import artifacts (skills, command prompts, kickoff templates), not private conversation state.
- first_step: add `mini_ork/cli/migrate.py` registered as `migrate` with `plan` and `apply`; first provider `claude-code` reads `~/.claude/skills/*/SKILL.md` and `.claude/commands/*.md` into `$MINI_ORK_ROOT/.agents/skills/`, previews by default, and never touches auth credentials.

### E5 — Versioned extension SDK contract + capability inspection

- evidence: `docs/plugins/sdk-overview.md:11,25-36` (typed SDK contract, experimental status, pin host version, documented migration path), `docs/plugins/architecture.md:38-52` (one registration API, many capability types), `:61-66` (external compatibility stance), `:90-100` (`plugins inspect` reports registered capabilities and compatibility signals), `packages/plugin-sdk/package.json` (explicit export map), `docs/gateway/external-apps.md:37` (stable packages for external apps, distinct from the in-process SDK).
- what_openclaw_does: freezes a small set of documented import subpaths and registration methods, version-gates them with `compat.pluginApi` / `minHostVersion`, records deprecations in a compatibility registry, and proves what actually loaded with `inspect --runtime --json`.
- why_it_drives_stars: a developer will build on a surface they can pin, test and inspect without reading core. The moment is "I can ship this integration and know which host versions it supports." That is the difference between a repository and a platform.
- mini_ork_fit: high
- star_impact: medium
- effort: M
- risk: declaring stability too early freezes internals that still need to move; `docs/EXTENSION.md` already shows schema drift. An "experimental" label without deprecation windows is a trust leak.
- first_step: write `docs/EXTENSION-SDK.md` with a version table for the six `register_*` seams and a deprecation policy; add `mini-ork extensions list --json` (or `doctor --extensions`) that reads `PROVIDER_KIND_BUILDERS`, `GATE_EVALUATORS`, node handlers, registered features, routing policies and subcommands.

### E6 — In-CLI discovery (`search`) over recipes and skills

- evidence: `docs/tools/plugin.md:34-43` (`openclaw plugins search "calendar"` is the first quick-start step), `skills/clawhub/SKILL.md` ("Search before claiming that a requested capability is unavailable"), `docs/tools/plugin.md:123` (source table makes the result actionable), `docs/tools/skills.md:27` (ClawHub card in the skills docs).
- what_openclaw_does: a first-class search command surfaces installable capabilities by name and description, before the user knows what package to install; install is a second, consent-gated step.
- why_it_drives_stars: the first command a curious developer runs is a search. A populated result list is proof the ecosystem exists; an empty one is proof it does not. The moment is "there are recipes for what I do."
- mini_ork_fit: high
- star_impact: medium
- effort: S
- risk: search over a tiny catalog is a worse first impression than no search; seed it with the 46 shipped recipes and make keyword/task-class matching real (`task_class.yaml` already carries `matches.keywords`).
- first_step: add `mini_ork/cli/recipes_cmd.py` with `mini-ork recipes list` and `mini-ork recipes search <term>`, backed by `recipes_catalog.list_recipes` and the `matches.keywords` field; register it in `mini_ork/cli/main.py`.

### E7 — Provider breadth as a first-class extension surface

- evidence: `extensions/` has 176 directories; `docs/providers/` has 92 pages; `docs/providers/index.md:9` ("OpenClaw can use many LLM providers"); `extensions/deepseek/package.json` carries its own `openclaw.install` hints and `publishToClawHub`; `docs/plugins/architecture.md:38` shows `api.registerProvider(...)` as one capability among many.
- what_openclaw_does: every model/media/search provider is a small plugin with its own manifest, install hints, compatibility floor and release metadata; breadth is a byproduct of the extension API, not core work.
- why_it_drives_stars: every provider has a user community. "Works with your model" is a discovery vector, and a provider plugin is a small, well-scoped first contribution. The moment is a user of a niche provider finding their model in the list.
- mini_ork_fit: high
- star_impact: medium
- effort: M
- risk: breadth without quality is a support burden; a provider lane that silently stalls or misprices is worse than an absent one. Do not chase a count — chase a catalog with a `docs_url` and a health probe per lane.
- first_step: split `config/providers.yaml` entries into `providers/<id>.yaml` files carrying `kind`, `family`, `model`, `docs_url` and `api_key_env`; teach `mini_ork/dispatch/providers.py:_load_providers_registry` to merge the directory with the single file; add `mini-ork providers list --json`.

### E8 — Contributor funnel: correct CONTRIBUTING, issue routes, pre-PR review

- evidence: `CONTRIBUTING.md:44` (20-PR hard cap), `:215` ("AI/Vibe-Coded PRs Welcome"), `:234` (skills routed to ClawHub), `:238` (good-first-issue link), `.github/CODEOWNERS` + three issue templates, `skills/autoreview/SKILL.md` (pre-PR review skill), `docs/tools/plugin.md:147` (install policy warnings).
- what_openclaw_does: routes each contribution type to the right surface (bug, docs, feature, plugin/skill, security), caps PR volume, publishes a good-first-issue queue, gives authors a local `autoreview` skill to run before opening a PR, and supports AI-assisted PRs explicitly.
- why_it_drives_stars: stars follow contributors. A visible on-ramp turns a user into an author, and an author's commits, posts and talks carry the name. The moment is "my first PR got a fair review and landed."
- mini_ork_fit: medium
- star_impact: medium
- effort: S
- risk: process theatre if the issue queue is not curated; worse, `CONTRIBUTING.md:19-27,58-70` still documents the removed bash runtime (`tests/smoke.sh`, `shellcheck`, bash 4.0), which actively repels a Python contributor.
- first_step: rewrite `CONTRIBUTING.md` for the Python runtime and the worktree flow in `CLAUDE.md`; add `.github/ISSUE_TEMPLATE/recipe_proposal.yml` and a `good first issue` section with 5–10 curated `good first issue` links.

### E9 — Registry quality bar and signed feed

- evidence: `docs/cli/plugins/marketplace.md:53-60` (hosted feed, `clawhub-public` profile, payload identity, DSSE verification, checksum pinning), `docs/plugins/compatibility.md:20-30` (statuses, owners, deprecation dates, removal gates), `docs/tools/plugin.md:147` (operator install policy can allow/warn/block), `docs/cli/plugins/authoring.md:85` (`clawhub package validate`).
- what_openclaw_does: separates discovery from trust: feeds are signed and checksum-pinned, package metadata declares compatibility floors, an operator policy hook can block installs, and a compatibility registry makes removals planned rather than surprising.
- why_it_drives_stars: teams adopt ecosystems they can defend in review. A security story is what turns individual stars into organizational adoption. The moment is a platform engineer saying "we can allow this registry."
- mini_ork_fit: medium
- star_impact: medium
- effort: L
- risk: over-engineering before there is a registry. Signing a feed nobody consumes, or forcing provenance on a five-recipe catalog, looks like theatre and slows the useful work.
- first_step: define `schemas/recipe_registry.schema.json` with `version`, `sha256`, `min_mini_ork_version`, `author`, `review_status`; make `recipe install` verify the checksum and refuse on mismatch. Defer signatures until the registry has real third-party entries.

### E10 — Skill exchange: `mini-ork skills install` over the shared SKILL.md format

- evidence: `mini_ork/dispatch/skill_registry.py:1-80` (SKILL.md directories, run/operator/project tiers, `.agents/skills` shared with Zed), `docs/CONFIG.md:471-520` (grant-by-name, precedence table), `mini_ork/cli/skills.py:212-228` (`list`/`show`/`validate`/`promote`), reference side `docs/tools/skills.md:32-46` (precedence) and `docs/tools/creating-skills.md:207-228` (`clawhub skill publish`).
- what_openclaw_does: treats skills as portable `SKILL.md` directories with a documented precedence across workspace/project/personal/managed/bundled sources, a publish path through ClawHub, and a validator (`skills/skill-creator`).
- why_it_drives_stars: skill authors are the largest contributor pool and the least likely to write framework code. Because mini-ork already uses the same file format and `.agents/skills` location as OpenClaw and Zed, a skill written once is immediately useful in three ecosystems. The moment is "install this procedure into my agent."
- mini_ork_fit: high
- star_impact: medium
- effort: S
- risk: a remote skill is prompt-injection surface; an unreviewed install can instruct an agent to exfiltrate. Ship `--dry-run` and `skills validate` first, and pin checksums.
- first_step: add `mini_ork skills install <name> [--dry-run]` backed by a static `skills/index.json`; copy into `$MINI_ORK_HOME/config/skills/<name>`, run the existing validator, and emit the same grant events. Add `mini-ork skills search` over the same index.

## Ranked Shortlist

1. E2 — `recipe init` is the cheapest way to turn users into authors, and authoring is the precondition for every other ecosystem mechanic.
2. E1 — the registry is the flywheel; it should follow, not precede, a working authoring and install path.
3. E10 — the format is already compatible with OpenClaw/Zed, so remote skill install is a small change with a real "install someone else's procedure" moment.
4. E6 — CLI search is an S-sized change that makes the 46 existing recipes legible; it is the front door to E1.
5. E8 — the contributor docs are currently wrong (bash-era), so this is a bug fix before it is a strategy; S effort.
6. E3 — a portable bundle is the shareable artifact that lets recipes travel outside the repo; depends on E2's manifest.
7. E5 — a versioned SDK contract plus inspection turns six internal registries into a platform surface; M effort, mostly documentation discipline.
8. E7 — provider breadth is already partly real (`register_provider_kind`, five kinds); per-provider packages and a catalog are the next step.
9. E4 — migration importers have a narrower audience and a higher broken-promise risk than the authoring loop, but Claude Code skill import is a credible beachhead.
10. E9 — signatures and compatibility gates are the right end state, but with zero third-party recipes today this is premature.

## mini-ork Gap Analysis

**E1 — Recipe registry.** Today: `mini_ork/recipes_catalog.py:174` discovers recipes only under `<home>/recipes` then the engine's `recipes/`, with project shadowing at `:241`; `mini_ork/cli/main.py:48-78` has no `recipes` subcommand. Missing: a remote index, version/checksum metadata, an install destination and a source prefix. Nearest surface: the recipe catalog itself — it already has the id/path/description model a registry needs.

**E2 — Authoring scaffold.** Today: `docs/EXTENSION.md:26` tells authors to copy a shipped recipe; `recipes/recipe-creator/` is an LLM-driven recipe, not a deterministic generator; `mini_ork/cli/validate.py:407` validates a project, not a recipe directory. Missing: `recipe init`, a template set, and a `recipe validate/pack` contract. Nearest surface: `mini_ork/extensions.py:35` `RecipeBuilder` (already builds `RecipeSpec` objects in Python) and the recipe-creator validator script.

**E3 — Portable bundles.** Today: recipes are self-contained directories (`recipes/code-fix/` ships task class, workflow, artifact contract, prompts, verifiers, probes) and a recipe-local `register.py` is loaded by `mini_ork/cli/recipe_register.py`. Missing: a bundle manifest, a deterministic pack command, and an install/unpack path. Nearest surface: the recipe directory contract plus `mini_ork/cli/recipe_register.py`; the format is 80% defined by convention, 0% by schema.

**E4 — Migration importers.** Today: no `migrate` subcommand in `mini_ork/cli/main.py:48-78`; skills can be copied into `.agents/skills` or `$MINI_ORK_HOME/config/skills`; kickoffs are plain Markdown. Missing: a preview/apply plan model, source detection, conflict handling and a provider registration seam. Nearest surface: `mini_ork/dispatch/skill_registry.py` (the import target) and `register_subcommand` for the CLI.

**E5 — Extension SDK contract.** Today: six documented `register_*` seams (`providers.py:916`, `execute_handlers.py:3424`, `gate_registry.py:332`, `main.py:2203`, `features/registry.py:212`, `routing.py:206`, `semantic.py:178`) and `docs/EXTENSION.md` as prose. Missing: a versioned compatibility policy, per-seam stability labels, and an introspection command that proves what is registered in a given process. Nearest surface: `mini-ork doctor` and `mini-ork features --json`; both already enumerate registry state, just not the extension seams.

**E6 — CLI discovery.** Today: `recipes_catalog.list_recipes` returns every visible recipe with description and task class; `task_class.yaml` carries `matches.keywords`; the IDE thread pages use it. Missing: a user-facing `recipes list/search` command. Nearest surface: `mini_ork/cli/features.py` is the exact precedent — a registry exposed as a human table and `--json`.

**E7 — Provider breadth.** Today: `config/providers.yaml` is a single hand-maintained registry with five `kind` values (`anthropic-native`, `anthropic-compat`, `openai-compat`, `openai-chat`, `uhp`, plus `codex-native`, `opencode-native`, `executable`); `mini_ork/dispatch/providers.py:916` accepts new kinds; `mini-ork providers status/configure` handles credentials. Missing: per-provider files/packages, a catalog command with docs URLs, and a health matrix. Nearest surface: `mini_ork/cli/providers.py` and `resolve_provider`.

**E8 — Contributor funnel.** Today: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `GOVERNANCE.md`, `MAINTAINERS.md`, two issue templates, a PR template and CI. Missing: a Python-era CONTRIBUTING (current text references `tests/smoke.sh`, `shellcheck`, bash arrays and `lib/*.sh`), a recipe/plugin proposal route, a curated good-first-issue section, and a pre-PR review skill analogous to `autoreview`. Nearest surface: the existing `CONTRIBUTING.md` and `.github/`.

**E9 — Registry quality bar.** Today: `config/vault.example.yaml` demonstrates the simple/secure two-tier pattern; the skill registry validates frontmatter (`mini_ork/cli/skills.py:97`); recipes are local and unreviewed by construction. Missing: checksums, provenance, compatibility floors, an install policy hook. Nearest surface: `mini_ork/gates/gate_registry.py` and the vault config pattern — the codebase already knows how to fail closed, but the recipe path has no gate.

**E10 — Skill exchange.** Today: `mini_ork/dispatch/skill_registry.py` resolves `SKILL.md` directories across run/operator/project tiers and `.agents/skills`; `mini-ork skills list/show/validate/promote` exists (`mini_ork/cli/skills.py:212-228`); `docs/CONFIG.md:471` documents grants and precedence. Missing: `search`, `install`, `publish`, and a remote index. Nearest surface: the existing skills CLI — `install` is one new verb plus a fetch-and-validate path.

## Open Questions

1. **Where should community recipes live: a separate registry repo, or PRs to `recipes/`?** If mini-ork wants to keep reviewing recipes in-tree, E1 and E3 drop in value and E2/E6 dominate. If it wants an external registry, E1 becomes the centerpiece.
2. **Who is the primary audience: agent-workflow developers authoring recipes, or ops teams wiring providers/gates?** The former ranks E2/E1/E3 above E7; the latter ranks E7/E9 higher.
3. **Can mini-ork support arbitrary recipe code safely?** Recipe-local `register.py` and verifier scripts are code execution. Without a sandbox or review bar, E1/E3 require either curation or a trust model; with one, E9 rises.
4. **Is there an existing distribution channel that makes a new registry redundant?** If recipes can ship as PyPI packages or GitHub templates, E1 shrinks to a thin index and E3 becomes the main artifact.
5. **Do Claude Code / Codex skill authors want a second registry, or is GitHub enough?** If GitHub is enough, E10 shrinks to a documented import path and E4 absorbs it.
6. **What is the maintainer capacity for review?** A registry with no review is worse than none; the answer determines whether E1 launches open or curated.
