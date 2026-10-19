# Synthesis — star-growth panel

Panel: growth (`gpt-5.6-luna`), activation (`GLM-5.3`), ecosystem
(`deepseek-v4-flash`), capability (`MiniMax-M3`) — four lanes, four model
families, one reference (OpenClaw) read against one target (mini-ork).
Inputs: the four `lens-<lane>.md` reports and their JSON sidecars. All claims
below trace to receipts cited in those reports.

## Panel Summary

All four lenses reached the same structural diagnosis from different angles:
mini-ork's constraint is not capability but *visibility and sequencing*. The
growth lane classifies 7 of its 9 gaps as "already possible but invisible" —
the README has the demo, results, screenshot and positioning material, but no
first screen that routes a visitor to them. The activation lane walks the
first-run path and finds strong primitives for every stage (offline dry run,
`doctor`, `validate` with a lane liveness probe, `providers configure`,
fail-closed secrets, `run --resume`) but "nothing sequences them" — the
newcomer must order ~60 subcommands from a 514-line README. The capability
lane's recurring verdict is "the capability exists but is invisible":
heterogeneous lanes, gates, and multi-node DAGs are real, but the surface is
"one CLI flag deep". The ecosystem lane counts six `register_*` seams and 50+
shipped recipes, but no deterministic authoring scaffold and no distribution
path outside the repo. One mechanism is proposed independently at full
strength by two lanes: one command to first verified value (activation's A1
demo command, growth's G3 install-to-aha path) — and both lanes note the
material already exists (`examples/00-demo.sh`, the certify demo).

The lenses disagree on where the next star comes from, and the disagreement
is positional, not evidential. Growth bets on the GitHub scan: its shortlist
is dominated by S-effort documentation surfaces. Activation bets on the
first-run funnel: demo, onboarding, recovery. Capability bets on the
"wait, it can do that?" IDE moment: team presets, live lane pivot, gate
approvals. Ecosystem bets on compounding: scaffold authors before building
any registry, and sign nothing until third parties exist. On effort, the two
lanes that proposed the demo split (A1 said S, G3 said M); this synthesis
takes M because the merged item includes the canonical README path and
honest dry-run success semantics, not just the command handler. The deepest
tension is identity: the reference *is* a personal assistant, so several of
its star mechanisms — persona memory (K2), 50-channel omnipresence (K3),
computer use (K8), a browser dashboard (A10) — only transfer if mini-ork
becomes a different product. The plan below keeps the mechanisms that
translate into task-OS terms (detection, one-command value, trust made
visible, authoring) and rejects the category shifts in
`Rejected Or Not Worth It`.

## Top 10 Adoption Plan

Ranked by star impact per unit of effort, then by mechanism consensus and
causal clarity. S-effort items lead because their ratio is best; T1 leads
despite M effort because it is the only two-lane mechanism consensus and its
content already exists.

### T1 — `mini-ork demo`: one command to a completed, verified loop
- sources: A1, G3
- what: a registered `demo` subcommand that wraps the existing
  `examples/00-demo.sh` flow (temp project → `mini-ork init` → synthetic
  kickoff → dry `run code-fix` → printed verdict and artifact paths), and a
  README Start-here path collapsed onto it, with the existing certify demo as
  the optional real-provider upgrade. Absorbs A7's install tail:
  `scripts/full_install.py` ends by pointing at `mini-ork demo`.
- why_it_drives_stars: a visitor's first-hand "I ran one command and the
  classify → plan → execute → verify loop completed in front of me" is the
  moment a bookmark becomes a recommendation; today that visitor faces six
  manual steps whose own example doc breaks on a personal absolute path
  (`examples/01-hello-world/README.md:21`).
- first_step: register a `demo` handler in `mini_ork/cli/main.py`
  (`_build_default_registry`, alongside `doctor`); fix the example's copy
  path; make `scripts/full_install.py` print "first success in 60 seconds:
  `mini-ork demo`" as its last line.
- star_impact: high
- effort: M
- confidence: high

### T2 — README as a proof-first landing page
- sources: G1, G2
- what: the first screen rebuilt as hero + rail: one durable category
  sentence, a four-noun glossary (recipes, lanes, gates, certificates), and a
  "See it / Try it / Understand it / Extend it" link rail over the existing
  certify demo, measured results, IDE screenshot, architecture docs and
  extension guide.
- why_it_drives_stars: the star decision happens in a ~30-second scan where
  the visitor asks "is this real, maintained, relevant to me?"; a first screen
  that classifies the product in one retellable sentence and routes the next
  click removes the uncertainty that turns a star into a maybe-later
  bookmark.
- first_step: rewrite the hero block at `README.md:2-28` around the category
  sentence and glossary, and add the four-link rail at `README.md:2-19`.
- star_impact: high
- effort: S
- confidence: high

### T3 — Citable "why mini-ork" decision surface
- sources: G5
- what: promote `docs/positioning/why-mini-ork.md` to a first-screen
  destination and add a "use mini-ork when / do not use it when" matrix
  grounded in current contracts, with every contrast linked to a real docs
  path and explicit non-claims.
- why_it_drives_stars: the adoption decision usually happens in a team
  discussion, not on the README; a source-backed decision page hands the
  advocate the language to defend mini-ork against generic graph runners,
  coding agents and after-the-fact eval tools — the page is already mostly
  written per the growth receipts.
- first_step: link `docs/positioning/why-mini-ork.md` from the README first
  screen and append the use-when / do-not-use-when matrix to that page.
- star_impact: high
- effort: S
- confidence: medium

### T4 — Measured-evidence-and-limits trust bundle
- sources: G6
- what: a compact "evidence and limits" box near the README hero pairing each
  headline number with its method, sample size, cost, abstention policy and
  failure cases, linking `docs/RESULTS.md` and `docs/SAFETY.md`.
- why_it_drives_stars: agent infrastructure is a risk-sensitive dependency; a
  claim that survives skeptical review (numbers *and* the failures) is what
  makes a developer comfortable starring and sharing — selective benchmarks
  would invite the backlash the growth lane warns about.
- first_step: add the evidence box at `README.md:60-80`, every number tied to
  a method, linking `docs/RESULTS.md` and `docs/SAFETY.md`.
- star_impact: high
- effort: S
- confidence: medium

### T5 — Live lane pivot in the run view
- sources: K4
- what: an IDE verb on the running node that swaps the `dispatch_model` lane
  mid-flight with partial-transcript preservation, and a run header that
  shows the current lane plus declared fallbacks.
- why_it_drives_stars: "it kept going when the API rate-limited, and I
  switched lanes mid-run without touching the recipe" is a demonstrable
  capability moment that mini-ork's dispatch stack (`llm_dispatch.py`,
  `routing.py`, `cost_pause.py`, `throttle_guard.py`) already earns — the
  swap exists today but is operator-invisible, living in env vars and lane
  metrics.
- first_step: add the "live lane pivot" action to `mini_ork/ide_pages/run.py`
  and surface current lane + fallbacks in the run header.
- star_impact: high
- effort: S
- confidence: medium

### T6 — Operator-facing gate approvals
- sources: K6
- what: a "Pending approvals" card in the IDE inbox listing every gate
  decision awaiting operator input (`mini_ork/gates/intervention_gate.py`,
  `merge_approval.py`), with one-click approve/decline, landed behind an
  env gate (`MO_GATE_APPROVAL_UX=1`) so the simple tier defaults to no
  change.
- why_it_drives_stars: "it asked before doing the destructive thing, and the
  queue was right there" is safety-as-a-product; mini-ork's 30-file gate
  machinery already blocks destructive ops, but the operator only sees it by
  tailing a log — invisible trust earns no stars.
- first_step: extend `mini_ork/ide_pages/inbox.py` with the gate-decision
  card behind `MO_GATE_APPROVAL_UX=1`.
- star_impact: high
- effort: S
- confidence: medium

### T7 — Named architecture map in the project's own nouns
- sources: G4
- what: one small Mermaid diagram — kickoff → recipe → lanes → gates →
  artifact → learning — above the detailed architecture docs, each node
  linked to its deep page and named with the glossary fixed in T2.
- why_it_drives_stars: a developer who can see the shape of the system can
  place it in their stack and explain it to a teammate without reading
  internals; that "I understand the shape" moment is what converts curiosity
  into a bookmarked dependency.
- first_step: add the diagram near `README.md:187-200`, linking each node to
  the existing `docs/architecture/` and operator docs.
- star_impact: high
- effort: S
- confidence: medium

### T8 — `mini-ork onboard`: detect, verify, and a key-free local lane
- sources: A2, A8
- what: an onboarding command that detects what the visitor already has
  (claude/codex CLIs on PATH, env keys, vault entries, reachable
  OpenAI-compatible local servers), probes each candidate with the existing
  cached lane probe from `validate.py`, and writes the winner — with a
  zero-credential local lane (`openai-chat` kind at `localhost:11434/v1`)
  as the key-free fallback.
- why_it_drives_stars: the cliff between dry run and first real run is the
  credential wall; detection converts "go get an API key" into "press 1 to
  reuse the CLI you already have", and the local-lane fallback lets a
  key-less visitor complete a *real* plan → dispatch → verdict loop —
  first value without owning the hardest prerequisite.
- first_step: add `mini_ork/cli/onboard.py` (detect via `shutil.which` and
  `provider_environment()`, probe via validate.py's lane probe, write via
  `providers configure`); register it in `_build_default_registry()`.
- star_impact: high
- effort: M
- confidence: medium

### T9 — Capability catalog: make the 50 existing recipes pickable
- sources: E6, K7
- what: `mini-ork recipes list` / `recipes search <term>` over
  `recipes_catalog.list_recipes` and `task_class.yaml` `matches.keywords`,
  plus an IDE skills-style pane generated from per-recipe manifests
  (`inputs / outputs / required node types`) — surfacing, not new
  engineering.
- why_it_drives_stars: "there are recipes for what I do" is proven by a
  populated result list; the 50+ shipped recipes are currently reachable only
  by classification or by browsing the repo, so the strongest evidence of an
  ecosystem is invisible at the exact moment a curious developer looks.
- first_step: add `mini_ork/cli/recipes_cmd.py` with `list` and `search`,
  registered via `register_subcommand`; the IDE pane (`mini_ork/ide_pages/`)
  follows the same catalog.
- star_impact: medium
- effort: S
- confidence: medium

### T10 — Team presets: an org of agents in one command
- sources: K1
- what: `recipes/team-presets/` (research-team, launch-team, audit-team)
  wiring existing node kinds — researcher → implementer → reviewer — with
  handoff contracts, surfaced in the IDE as one team card fanning into live
  per-specialist cards.
- why_it_drives_stars: "I made a team in one command and they wrote me a
  launch plan while I watched" is the most retellable demo in the reference;
  mini-ork's DAGs are technically equivalent today but render as node
  completion rows — the product moment requires the *visible* composition.
- first_step: add `recipes/team-presets/research-team.yaml` using existing
  node kinds, and the fan-out card in `mini_ork/ide_pages/kanban.py`.
- star_impact: high
- effort: M
- confidence: low

### T11 — `mini-ork recipe init`: deterministic scaffold + validate
- sources: E2
- what: a deterministic scaffold emitting `task_class.yaml`, `workflow.yaml`,
  `artifact_contract.yaml`, `prompts/` and a verifier stub from a chosen
  template, plus `mini-ork recipe validate` wrapping the existing
  recipe-creator validator and pre-run checks — replacing today's "copy a
  shipped recipe by hand" instruction.
- why_it_drives_stars: the moment a developer thinks "I could wrap my
  workflow as a recipe" is the moment they become an author; a scaffold that
  produces a runnable dry-run in five minutes converts that thought into a
  contribution instead of a abandoned copy attempt.
- first_step: add `mini_ork/cli/recipe_init.py` registered as `recipe-init`
  with `code-fix` as the first template; generate from the same validator
  the engine uses so scaffolded YAML always passes.
- star_impact: high
- effort: M
- confidence: medium

### T12 — Curated registry with install-by-name for recipes and skills
- sources: E1, E10, G8
- what: a static, maintainer-curated `registry.json` (id, version, sha256,
  `min_mini_ork_version`, repo) backing `mini-ork recipes install <id>` and
  `mini-ork skills install <name>` with checksum verification that refuses
  on mismatch, plus a public gallery section in `docs/EXTENSION.md` with
  maintained examples and contribution boundaries.
- why_it_drives_stars: every external repo or post that says
  `mini-ork recipes install <id>` distributes the project without maintainer
  work; and because mini-ork already speaks the shared `SKILL.md` +
  `.agents/skills` format, a skill published once is useful in three
  ecosystems — the distribution flywheel other lenses keep gesturing at.
- first_step: add the `remote` source kind and `registry.json` handling to
  `mini_ork/recipes_catalog.py` with sha256 verification on install;
  `skills install` reuses the same index shape and the existing skill
  validator.
- star_impact: high
- effort: M
- confidence: medium

## Score Matrix

| Rank | Item | Impact | Effort | Confidence | Lenses | Lane sources |
|---|---|---|---|---|---|---|
| 1 | T1 one-command demo | high | M | high | 2 | activation (A1), growth (G3) |
| 2 | T2 README landing page | high | S | high | 1 | growth (G1, G2) |
| 3 | T3 why-mini-ork page | high | S | medium | 1 | growth (G5) |
| 4 | T4 evidence & limits box | high | S | medium | 1 | growth (G6) |
| 5 | T5 live lane pivot | high | S | medium | 1 | capability (K4) |
| 6 | T6 gate approvals | high | S | medium | 1 | capability (K6) |
| 7 | T7 architecture map | high | S | medium | 1 | growth (G4) |
| 8 | T8 onboard + local lane | high | M | medium | 1 | activation (A2, A8) |
| 9 | T9 capability catalog | medium | S | medium | 2 | ecosystem (E6), capability (K7) |
| 10 | T10 team presets | high | M | low | 1 | capability (K1) |
| 11 | T11 recipe init scaffold | high | M | medium | 1 | ecosystem (E2) |
| 12 | T12 curated registry | high | M | medium | 2 | ecosystem (E1, E10), growth (G8) |

Effort merges: T1 takes M over A1's S because the merged scope includes the
canonical README path and honest dry-run semantics, not just the handler;
T12 takes M (E1 M, E10 S, G8 M) for the same reason — the merged item is the
larger scope. All other merged items agreed on effort.

## Cross-Lens Consensus

- **One command to first verified value — A1 (activation) + G3 (growth).**
  The only full-strength mechanism consensus: both lanes name the same causal
  step (first-hand success before reading any config), the same observation
  that the material already exists (`examples/00-demo.sh`, the certify demo),
  and the same failure (a six-step manual path whose example doc breaks on a
  personal path). This proves the funnel's first stall is real and cheap to
  fix. It does *not* prove the dry run satisfies: both lanes independently
  flagged that `MINI_ORK_DRY_RUN=1` records no `task_runs` row
  (`examples/00-demo.sh:73`), so the demo must print what the loop proved and
  where the artifacts are, or the moment reads as theatre.
- **Surface what already exists — E6 (ecosystem) + K7 (capability).** Both
  lanes found 50+ recipes invisible and concluded surfacing is near-zero
  engineering; they disagree on surface (CLI search vs IDE pane). Agreement
  proves the catalog is the cheapest ecosystem proof; it does not prove which
  surface matters — that rides on the IDE-primacy question below, so T9 ships
  the CLI verb first and the pane second.
- **A public home for extensions — G8 (growth) + E1/E10 (ecosystem).** The
  lanes agree extensions need a visible, curated public home with a path from
  local artifact to shareable package. They differ in degree: growth's
  version is a curated docs gallery with maintained examples; ecosystem's is
  install-by-name infrastructure with checksums. The union is T12, and the
  ecosystem lane's own sequencing (scaffold before registry) is why T11
  precedes it. The agreement does not prove demand — with zero third-party
  recipes today, the registry must launch curated, not open.
- **Theme agreement deliberately *not* weighted as consensus.** G6 (evidence
  box) and K6 (gate approvals) both make existing trust machinery visible,
  but the mechanisms differ (a README evidence bundle vs an IDE approval
  inbox) — kept as separate items, neither inflated. Likewise K1's team panel
  and G4's architecture diagram share a "make composition legible" theme
  while being different artifacts. Per the ranking rule, only
  mechanism-level agreement raised ranks.

## Rejected Or Not Worth It

Every candidate not in the plan, with its reason. "Absorbed" means the id's
content ships inside a top item rather than standing alone.

- A3 — `mini-ork triage` (sanitized diagnosis handed to a coding agent):
  sequenced behind T8, not rejected on merit. The bundle pays off only once
  real runs and their failure population exist; its secret-redaction risk
  also deserves a cycle of its own. First candidate to revive in the next 90.
- A4 — `doctor --fix`: below the cut line — medium impact, single lane, and
  an opportunistic add-on to T8's workstream (the handler already names every
  fix) rather than a star lever on its own.
- A5 — "First 60 seconds" ladder + first-run FAQ: below the cut line for the
  same reason; its own receipts argue the failure set is predictable, so
  write the ladder when T8's real failures accumulate, not speculatively.
- A6 — interrupted-run resume story: absorbed into T1/T8 — one banner naming
  the run id and the exact `--resume` command plus one README sentence;
  messaging around machinery that already exists is not a plan item.
- A7 — verified install tail / version stamp: absorbed into T1 — the version
  disagreement (0.9.0 vs "v0.1" vs "0.1.0") and the install-tail pointer are
  fixed inside T1's scope; the honesty crack is real and the fix is a line.
- A9 — Windows `install.ps1` one-liner: effort disproportionate until the
  native-Windows-vs-WSL audience share is known (the activation lens's own
  open question); revisit with data, keep the manual winget list honest.
- A10 — browser success view: rejected on identity — it contradicts the
  deliberate removal of the web UI in favour of the native IDE
  (`docs/UI.md`); the activation lens itself predicted this rejection.
  `serve.py` keeps the read surface if a verdict view is ever wanted.
- G7 — visible release momentum: largely already shipped — `CHANGELOG.md`
  plus a README roadmap exist; the residual (a "what's new" link) is a
  one-line edit inside T2's rail, not a plan item.
- G9 — visual identity / "ork" branding: theatre risk — recall without the
  proof contract; the growth lens itself ranks it last and defers any mascot
  until the core narrative settles.
- E3 — portable recipe bundles (`.tgz`): the right follow-on to T12, not a
  first-cycle item — a bundle format with nothing to distribute yet is
  effort disproportionate; schema it after install-by-name has consumers.
- E4 — migration importers: broken-promise risk (agent state models differ;
  a shallow import is worse than none) and a narrower audience; the Claude
  skill-import beachhead lands far more cheaply inside T12's skills install
  once that surface exists.
- E5 — versioned extension SDK contract: premature versioning — freezing
  seams before external authors exist, and the lens's own receipt shows the
  schema already drifting (`docs/EXTENSION.md:32`); the cheap hygiene slice
  (`extensions list --json`) can ride with T12, the policy waits for T11's
  authors.
- E7 — provider breadth as extension surface: partly already shipped
  (`openai-chat` kind, `providers.yaml`, `register_provider_kind`) and
  breadth without demonstrated demand is a support burden; the slice that
  serves activation — the local-lane default — lives in T8.
- E8 — contributor funnel (fix CONTRIBUTING, issue routes): the bug is real
  (`CONTRIBUTING.md` still documents the removed bash runtime — fix it now
  as hygiene), but the causal chain to stars runs through contributors and
  is slower and less certain than the visitor funnel this plan optimizes.
- E9 — signed feed / registry quality bar: premature with zero third-party
  recipes; the ecosystem lens itself ranks it last. The fail-closed core
  (checksum verification, refuse on mismatch) is carried inside T12.
- K2 — memory diary / "remembers while you sleep": identity risk — the
  appeal *is* the personal-assistant promise; mini-ork is a task OS for agent
  work, and even the project-scoped, operator-curated salvage is a retention
  feature, not a star mechanism for agent-workflow developers. Revisit only
  as project-scoped operator-curated notes, never persona.
- K3 — chat-channel expansion ("everywhere you already are"): category pull —
  50 messaging channels serve an assistant product; the only on-mission
  slice (PM-tracker adapters so `awaiting input` prompts are answerable from
  GitHub/Linear/Jira) is a workstream of its own, not a 90-day star item.
- K5 — between-node steering: real product value, weak star lever — the
  artifact-graph contract forces the honest "applies to the next node"
  framing, which mutes the demo; ship on operator demand, not for growth.
- K8 — computer-use node: off-mission and L effort — per-platform driver
  infrastructure for an operator-assistant surface mini-ork explicitly is
  not; the capability lens itself defers it behind everything else.

## 90-Day Sequence

Each step ships on its own; each bucket makes the next cheaper.

**Days 0–30 — Be findable and tryable (T1, T2, T3, T4, T7).** All S-effort
docs plus the demo command. Observable outcome: on a clean machine, a fresh
visitor runs the installer, then `mini-ork demo`, and sees a loop complete
with a printed verdict — without reading past a first screen that carries
the category sentence, the action rail, the why-page link, the evidence box
and the architecture map. A7's version fix lands here inside T1.

**Days 31–60 — Make the first real run and the living system visible
(T8, T5, T6, T9).** Observable outcome: a visitor with no API key completes a
*real* run to `verdict.json` via onboard's detected-or-local lane; the IDE
shows a mid-run lane swap and a pending gate approval decided in one click;
`mini-ork recipes search <term>` returns seeded results over the existing
catalog. T1's demo users generate exactly the failure modes that justify
reviving A4/A5 here if capacity allows.

**Days 61–90 — Turn users into authors (T11, T12, T10).** Observable
outcome: a recipe scaffolded by `mini-ork recipe init` passes
`mini-ork recipe validate`, installs by name from the curated registry onto a
second machine, and a team preset run renders as one card fanning into live
specialist cards. T9's catalog front-ran the registry; T11's scaffold creates
its first authors; T2's glossary fixed the nouns everything renders.

## Questions For Human Decision

1. **The category phrase.** "Verified agent work" vs "proof-carrying agent
   workflows" (G1's open question) — this single sentence sets T2's hero and
   T7's glossary, and every later surface repeats it.
2. **Dry-run semantics.** Should `MINI_ORK_DRY_RUN=1` record a `task_runs`
   row so T1's demo success is inspectable in `state.db`? Both the growth and
   activation lanes flagged the gap; the answer changes what the demo can
   honestly claim.
3. **Is the IDE a primary surface or a companion?** T5, T6 and T10 assume
   primary (35 IDE page files suggest yes); a "companion" answer demotes them
   and makes T9's CLI verb the load-bearing surface.
4. **Registry scope.** Curated static index vs open; community recipes
   in-tree under `recipes/` vs a separate registry repo (the ecosystem lane's
   first open question). This gates T12's shape and whether E3/E5/E9 revive
   later.
5. **What mini-ork refuses to become.** Confirm the identity rejections —
   no web dashboard (A10), no persona memory (K2), no chat-channel expansion
   (K3), no computer use (K8) — as standing product lines, not just ranking
   outcomes; the reference's gravity will keep re-proposing them.
6. **Wizard or checklist.** Does mini-ork's operator-first minimalism accept
   an interactive `mini-ork onboard`, or should T8 ship as a non-interactive
   `onboard --check` that prints a plan? A negative answer reshapes T8 toward
   A4/A5-style guided repair.
