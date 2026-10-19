# Growth lens: why developers star a repository

Model observed for this lane: `gpt-5.6-luna` (the lane stream records the
Codex launch with that configured model). The reference and target repositories
were read only.

## Evidence Read

Commands and receipts:

- `git -C $REF ls-tree --name-only HEAD`
  and `git -C $REF ls-tree -r --name-only HEAD docs`
  enumerated the reference surface, including `docs/start/why-openclaw.md`,
  `docs/releases/index.md`, and the docs assets.
- `git -C $REF show HEAD:<path> | nl -ba`
  read `README.md`, `VISION.md`, `CHANGELOG.md`, `AGENTS.md`, `docs/index.md`,
  `docs/docs_map.md`, `docs/start/why-openclaw.md`, `docs/start/getting-started.md`,
  `docs/start/onboarding-overview.md`, `docs/releases/index.md`, `CONTRIBUTING.md`,
  and `package.json`.
- Reference receipts: `README.md:1-22` has the identity, banner, badges, and
  promise; `README.md:24-66` has the install-to-working-dashboard path;
  `README.md:68-75` names the Gateway, Control UI, channels, nodes, tools,
  skills, and plugins; `README.md:108-145` exposes governance, Star History,
  sponsors, and contributors. `docs/index.md:25-46` repeats the hero promise
  and four start-here actions; `docs/index.md:48-90` is a use-case docs hub;
  `docs/index.md:109-153` combines a Mermaid architecture diagram with
  capability cards; `docs/index.md:155-204` gives a four-step quick start.
- Positioning receipts: `VISION.md:86-115` makes a small core plus plugins,
  bundle-style extensions, npm distribution, and ClawHub a deliberate product
  rule; `docs/start/why-openclaw.md:21-61` turns the architecture into a
  citable comparison with seven testable properties and explicit limits;
  `docs/start/why-openclaw.md:63-79` connects plugin APIs, ClawHub, and open
  protocols to an adoption story.
- Momentum receipts: `CHANGELOG.md:5-26` exposes Unreleased plus a long,
  date-stamped release index; `package.json:2-28` gives the package identity,
  current version, description, homepage, and MIT license.
- `git -C $REF ls-tree -r --name-only HEAD
  <dir> | awk ... | sort -u | wc -l` returned 183 extension names, 50 skill
  names, and 25 immediate package names. The kickoff's 28-package figure is
  therefore treated as context, not as a re-derived receipt.
- Target receipts: `README.md:2-28` has the icon, badges, and task-OS promise;
  `README.md:30-88` has the certify demo, measured results, warning, and IDE
  screenshot; `README.md:90-200` explains the positioning and seven capability
  pillars; `README.md:202-281` documents install and the first dry-run;
  `README.md:299-312` explains recipes and the extension guide;
  `README.md:483-510` links the SDK, architecture, safety, roadmap, and
  contribution status. `docs/positioning/why-mini-ork.md:1-8` and `:34-55`
  state the distinctive thesis and comparison axes; `docs/EXTENSION.md:1-18`
  and `:26-220` document SDK, workflow, lane, and verifier extension points;
  `CHANGELOG.md:1-100` already records releases and measured features.
- The target verifier was read at
  `recipes/star-growth-panel/verifiers/lens-completeness.py:45-79` and
  `:147-270`; it requires lane-prefixed candidates, evidence, ratings, first
  steps, exact sidecar IDs, and a complete shortlist.

### Cross-Candidate Observations

1. OpenClaw repeats one promise across the repository, docs, and visual assets,
   then gives the system a few retellable nouns. The growth mechanism is
   compression and consistency, not the raw count of channels or platforms.
2. The reference stacks hero, proof, install, and trust in that order. A star
   is easier when a visitor can understand the product, see evidence, and
   imagine a successful first run without opening a source file.
3. “Why us?” is a first-class surface. OpenClaw compares architectures,
   states what it does not claim, and points to testable properties. This gives
   a developer language for defending adoption to a teammate or reviewer.
4. Extension vocabulary and release vocabulary turn a repository into a living
   category: contributors know where new work belongs, while prospective users
   can see that the project is active. mini-ork has much of the underlying
   material; its principal gap is public information architecture.

## Candidate Features

### G1 — Own the “verified agent work” category

- evidence: `README.md:1-22` opens with a compact identity and promise; `docs/index.md:25-30` repeats “your AI assistant” plus “One Gateway. Any model. Any device”; `VISION.md:3-5` reduces the project to three memorable clauses.
- what_openclaw_does: OpenClaw states who it is, where it runs, and what makes it distinct before listing features. The same sentence and nouns recur in the README and docs landing page.
- why_it_drives_stars: A developer skimming a repository can classify it and retell it to a colleague in one sentence. For mini-ork, the winning moment is “this is the runtime that makes agent work prove itself,” not “another agent graph.”
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: A slogan that says “verified” without showing the verifier would become generic AI positioning or an overclaim about subjective work.
- first_step: `README.md:2-28` — rewrite the hero block around one durable category sentence and define a four-noun glossary for recipes, lanes, gates, and certificates.

### G2 — Make the README a proof-first landing page

- evidence: `README.md:3-16` combines a visual banner with CI, package, runtime, license, and community badges; `README.md:82-88` shows the IDE; `docs/index.md:33-46` supplies four action cards and `docs/index.md:48-90` supplies use-case navigation.
- what_openclaw_does: The first screen is a visual and navigational landing page: proof badges, a hero, direct start actions, and a docs map for different intents.
- why_it_drives_stars: This wins the GitHub scan where a visitor asks “is this real, maintained, and relevant to me?” Evidence and a next click reduce the uncertainty that otherwise turns a star into a maybe-later bookmark.
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: More badges, screenshots, or cards can look like theatre if links are stale or the screenshot does not match the current CLI and UI.
- first_step: `README.md:2-19` — add a compact “See it / Try it / Understand it / Extend it” link rail tied to the existing certify demo, results, architecture, and extension guide.

### G3 — Ship a canonical install-to-aha path

- evidence: `README.md:24-66` gives OS-specific install commands, onboarding, gateway status, dashboard launch, and a visible success check; `docs/index.md:155-204` turns the same flow into four numbered steps.
- what_openclaw_does: One install path leads to guided setup and then an observable working surface. The user does not need to assemble the architecture before seeing value.
- why_it_drives_stars: A star is more likely after a visitor reaches a real result. The developer moment is “I installed this and saw the system work,” which creates a credible recommendation to a teammate.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: A “one command” promise that still needs a provider, Docker, a repository, or a long kickoff would create a sharper disappointment than an honest dry-run path.
- first_step: `README.md:202-281` — make one copy-paste `mini-ork demo` path canonical, with a no-provider dry run first and the existing `certify-demo` as the optional proof upgrade.

### G4 — Publish a named architecture map

- evidence: `README.md:68-75` names the Gateway, Control UI, channels, nodes, tools, skills, and plugins; `docs/index.md:109-121` draws their flow in Mermaid; `docs/index.md:123-153` maps capabilities to those nouns.
- what_openclaw_does: It gives a newcomer a diagram and a stable vocabulary for how the parts connect, then routes each noun to a deeper page.
- why_it_drives_stars: A developer can decide where mini-ork belongs in a stack and explain it without reading internals. This is the “I understand the shape” moment that converts curiosity into a repository bookmark.
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: A diagram that names concepts the runtime does not own, or that drifts from the workflow schema, creates more confusion than a plain README.
- first_step: `README.md:187-200` — add a small kickoff → recipe → lanes → gates → artifact → learning diagram, linking each node to the existing architecture and operator docs.

### G5 — Add a citable “why mini-ork” decision surface

- evidence: `docs/start/why-openclaw.md:21-31` frames the trust-boundary comparison and its limits; `docs/start/why-openclaw.md:33-61` lists seven testable properties and short answers; `docs/positioning/why-mini-ork.md:34-55` already contains mini-ork’s comparison axes.
- what_openclaw_does: OpenClaw treats comparison as product documentation: it states the alternative, names the decision criteria, provides source-linked claims, and says what it does not claim.
- why_it_drives_stars: The adoption decision often happens in a team discussion, not on the README page. A citable “choose this when…” surface lets a developer justify mini-ork against generic graph runners, coding agents, and after-the-fact eval tools.
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: A simplistic competitor table becomes marketing copy, especially if it claims superiority instead of documenting ownership boundaries and evidence.
- first_step: `docs/positioning/why-mini-ork.md:1-8` — link this page from the README’s first screen and add a short “use mini-ork when / do not use it when” matrix grounded in the current contracts.

### G6 — Package measured evidence and limits as trust proof

- evidence: `README.md:60-75` reports external-repository and adversarial-corpus results with sample sizes, cost, and abstentions; `README.md:77-80` warns about unattended self-modification; `README.md:469-481` explains vacuous and unverified outcomes; `docs/start/why-openclaw.md:33-57` models explicit properties and limitations.
- what_openclaw_does: The reference makes trust visible by pairing capability claims with governance, security boundaries, caveats, and a citable evidence page instead of relying on confidence language.
- why_it_drives_stars: Agent infrastructure is a risk-sensitive dependency. Transparent evidence makes a developer comfortable starring and sharing the project because the claim survives skeptical review.
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: Selective benchmarks or prominent numbers without the failure cases would invite backlash and conflict with mini-ork’s own honesty thesis.
- first_step: `README.md:60-80` — create a compact “evidence and limits” box linking `docs/RESULTS.md`, `docs/SAFETY.md`, and the current abstention policy, with every headline number tied to a method.

### G7 — Turn release cadence into a visible momentum loop

- evidence: `CHANGELOG.md:5-26` lists Unreleased and dated releases from 2026.10.4 back through 2026.6.1; `docs/index.md:81-86` gives releases a dedicated docs-hub card.
- what_openclaw_does: Release history is a discoverable product surface, not only maintainer bookkeeping. The index lets a visitor see recent activity and drill into highlights.
- why_it_drives_stars: Developers star projects they expect to revisit. A visible “what changed” trail signals that the bookmark will keep paying off and gives them a reason to return after the first scan.
- mini_ork_fit: high
- star_impact: medium
- effort: S
- risk: A noisy or irregular changelog can expose churn rather than momentum; release notes must distinguish user-visible capability from internal maintenance.
- first_step: `README.md:493-510` — add a “What’s new” link and a short rolling highlights block sourced from the existing `CHANGELOG.md`, not a second manually maintained roadmap.

### G8 — Build an authentic extension and contribution funnel

- evidence: `VISION.md:86-115` says core stays lean while plugins, bundle surfaces, npm distribution, and ClawHub grow; `README.md:114-118` routes contributors to plugins and ClawHub; `docs/index.md:69-70` and `:150-152` expose ClawHub and plugin building as first-class paths.
- what_openclaw_does: It tells a contributor where optional capability belongs, how it is distributed, and where users discover it. The registry is part of the product narrative, not an isolated API.
- why_it_drives_stars: A developer who can imagine adding a recipe or provider sees future utility for the project and a path to participate. That turns a star from passive approval into a stake in an extensible platform.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: A catalog without trusted examples, maintenance ownership, or a safe publishing contract looks like an empty marketplace and increases review burden.
- first_step: `docs/EXTENSION.md:1-18` — add a public “recipe/provider gallery” entry point with two or three maintained examples, contribution boundaries, and a clear path from local recipe to shareable package.

### G9 — Give mini-ork a small, authentic visual identity

- evidence: `README.md:1-8` uses a banner and product identity; `README.md:118-120` links lore, project history, Star History, and social accounts; `README.md:138-145` renders contributor avatars; `docs/index.md:25-30` repeats a memorable quote and visual hero.
- what_openclaw_does: OpenClaw uses a mascot, phrase, lore, contributors, and visual assets to make an otherwise technical system recognizable in a crowded GitHub feed.
- why_it_drives_stars: Memorability helps the project be recalled and retold after a short scan. For mini-ork, a restrained “ork” visual language could make verified work distinctive without changing the product category.
- mini_ork_fit: medium
- star_impact: medium
- effort: S
- risk: Mascot-first branding can feel copied or distract from the proof contract; it cannot substitute for a working demo or honest evidence.
- first_step: `README.md:2-19` — define one consistent icon, phrase, and visual treatment for verified runs, then validate it against the existing screenshot and CLI output before adding lore.

## Ranked Shortlist

Ranked by expected star impact per unit of effort:

1. **G1** — A one-sentence “verified agent work” category is a small edit with the broadest comprehension payoff.
2. **G5** — A citable decision page wins the internal evaluation moment and is already mostly written.
3. **G2** — A proof-first README rail makes existing demos, results, and docs discoverable during the GitHub scan.
4. **G4** — A canonical architecture map makes mini-ork’s unusual workflow legible without adding runtime capability.
5. **G6** — Surfacing measured results and limits converts mini-ork’s honesty into adoption confidence.
6. **G3** — A canonical dry-run-to-certify path can create the “I saw it work” moment, but requires more maintenance across environments.
7. **G7** — A visible release/highlights link is cheap momentum proof, with lower impact than first-run comprehension.
8. **G8** — An extension gallery can compound growth, but only after maintained examples and publishing trust exist.
9. **G9** — Visual identity improves recall, but is the weakest causal lever without the other surfaces.

## mini-ork Gap Analysis

| Candidate | What mini-ork has today | What is missing | Gap type |
|---|---|---|---|
| G1 | `README.md:8-28` already says “task operating system” and describes classify → plan → execute → verify → reflect → improve; `docs/positioning/why-mini-ork.md:214-217` gives a one-line summary. | One short category sentence and a stable glossary that lead the README before the long feature inventory. | Already possible but invisible |
| G2 | `README.md:12-19` has badges; `README.md:30-88` has a strong demo, measured results, and an IDE screenshot. | A first-screen action rail and docs landing information architecture that tells a visitor what to click next. | Already possible but invisible |
| G3 | `README.md:202-281` has install, dry-run, `mini-ork init`, kickoff, validate, and a real-run path; `CHANGELOG.md:69-70` records one-line install. | One canonical, low-friction first value command that does not make the user choose among install, init, kickoff, dry-run, provider setup, and certify. | Genuinely incomplete packaging |
| G4 | `README.md:187-200` has a seven-pillar inventory; `docs/ARCHITECTURE.md:1-312` has the detailed architecture and lifecycle. | A small visual map above the detail that shows the end-to-end control/data flow in the project’s own nouns. | Already possible but invisible |
| G5 | `docs/positioning/why-mini-ork.md:1-8` and `:34-55` already articulate family diversity, executable specifications, persistence, and the comparison to per-session agents; README has a “Where it fits” section at `:170-185`. | Prominent README routing and a concise decision matrix for common alternatives and non-goals. | Already possible but invisible |
| G6 | `README.md:60-75` and `:469-481` expose measurements, abstention, vacuous outcomes, and uncertainty; `docs/RESULTS.md` and `docs/SAFETY.md` exist. | A compact trust bundle near the hero that connects each headline number to method, limitation, safety posture, and project status. | Already possible but invisible |
| G7 | `CHANGELOG.md:1-100` has Unreleased, 0.9.0, 0.8.0, measured feature notes, and release history; `README.md:493-503` has roadmap. | README-level “what’s new” discovery and a stable highlights convention that makes activity visible without duplicating the changelog. | Already possible but invisible |
| G8 | `docs/EXTENSION.md:1-18` and `:26-220` document Python, workflow, lane, and verifier extension points; `README.md:299-312` points to recipes. | A curated public gallery, maintained examples, and contribution/publishing guidance that make the extension surface socially legible. | Genuinely incomplete packaging |
| G9 | `README.md:2-9` has an icon and a strong “prove their work” phrase; the screenshot and CLI output provide a visual language. | A consistent, authentic visual system for verified runs; no mascot or lore should be added until the core narrative is settled. | Genuinely incomplete branding |

## Open Questions

- Which phrase can mini-ork defend with current evidence: “verified agent work,”
  “proof-carrying agent workflows,” or another term? The choice changes the
  category owned by G1 and the glossary used by G4.
- Can a no-provider dry run show a meaningful mini-ork result, or should the
  canonical first-run path require a real repository and provider? This decides
  whether G3 is a trustworthy aha moment or only an installation check.
- Which alternatives should the decision page name first—LangGraph/CrewAI,
  coding-agent products, eval frameworks, or per-session agent harnesses—and
  can every contrast be backed by a current path in `docs/`?
- Is the existing measured-results set stable enough to promote into the hero,
  or should G6 stay as a linked trust page until the benchmarks and routing
  claims are refreshed?
- Who would maintain the first recipe/provider gallery in G8, and what minimal
  trust, versioning, and security contract prevents an empty or unsafe catalog?
- Does mini-ork want a distinctive “ork” visual language at all? If yes, can it
  reinforce evidence and work provenance instead of importing an unrelated
  mascot narrative?
