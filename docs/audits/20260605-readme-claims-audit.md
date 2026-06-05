# README claims audit — 2026-06-05

Verification pass of every load-bearing claim in `README.md` (272 lines) against
the current main branch. Goal: surface staleness + false claims before a v0.3
release tag is cut. Method: shell-grep + live `mini-ork init` probe + state.db
schema inspection + recipe inventory.

## Methodology

1. Read README.md end-to-end, enumerated 38 individually-verifiable claim
   points (paths cited, counts asserted, CLI commands promised, version
   declarations, example yaml blocks, comparison-table rows).
2. Each claim probed by one of: file existence check, grep against the cited
   source, live `mini-ork init` + `sqlite3` pragma, git log query, or
   schema-enum extraction via python yaml.
3. Verdict ∈ { ✓ true | ⚠ partially true / clarification needed |
   ✗ false / stale }. Priority assigned by reader-impact: P0 = publicly
   misleading; P1 = directionally correct but numbers wrong; P2 = minor
   illustrative drift; P3 = wording nit.

## Findings — P0 (publicly misleading, ship-blocking)

### P0-1. Version freshness — README still claims v0.1 current

- **README line 228**: "### v0.1 (current — this release)"
- **Actual state**: `ROADMAP.md` declares `v0.2.0 — 2026-06-01 (current)` and
  `v0.3.0-rc1 — 2026-06-05 (in flight)`.
- **Reader impact**: a contributor reading the README sees v0.1 as the
  shipped version, misses 2 years of work in `lib/` + 7 new recipes + the
  oracle-hardening primitives shipped today.
- **Fix**: replace the entire "Roadmap" section (lines 226-252) with a
  "see ROADMAP.md" pointer + collapse to a 3-line `### Current: v0.3.0-rc1`
  block listing the latest release header. Single source of truth.

### P0-2. Recipes table lists 2 of 9 actual recipes

- **README lines 188-191**: Recipes table shows only `code-fix` +
  `bdd-first-delivery`.
- **Actual `recipes/` dir contents** (9 entries):
  `bdd-first-delivery`, `blog-post`, `code-fix`, `db-migration`, `docs`,
  `ops-runbook`, `refactor-audit`, `research-synthesis`, `ui-audit`.
- **Reader impact**: 78% of shipped recipes (7 of 9) are invisible from the
  README. A user can't discover `refactor-audit` (the framework's own
  self-audit pipeline) or `docs` (shipped today) from the top-of-fold.
- **Fix**: expand the recipes table to all 9 entries with one-line shape
  descriptions per recipe.

### P0-3. `install.sh --check` is advertised but unsupported

- **README line 266**: "Run `bash install.sh --check` to verify deps before
  first use."
- **Actual**: `grep -- "--check" install.sh` returns 0 matches. The flag
  does not exist; running it falls through to a normal install (or fails
  silently depending on the install path).
- **Reader impact**: a contributor following the README copy-pastes the
  command, gets unexpected behavior, debugs from scratch.
- **Fix options**: (a) add a `--check` mode to `install.sh` that runs
  `mini-ork doctor` post-install; (b) drop the claim from the README; (c)
  replace the command with `mini-ork doctor` which DOES exist + check deps.
  Path (c) is the smallest change.

## Findings — P1 (directionally correct but numbers stale)

### P1-1. "13 framework primitives in `lib/`" — actual count 38

- **README line 232**: roadmap lists "13 framework primitives in `lib/`" as a
  v0.1 deliverable.
- **Actual**: `ls lib/*.sh | wc -l` = 38 (including the 5 oracle-hardening
  primitives shipped 2026-06-05: `coalition_gate.sh`, `cw_por.sh`,
  `adaptive_stability.sh` + the others). The framework grew ~3× since
  this number was written.
- **Fix**: either move this line under "Released" with the original count
  preserved as historical record, OR refresh to the current count if you
  want the roadmap to live-update.

### P1-2. "8 `bin/` entrypoints" — actual count 13

- **README line 233**: "8 `bin/` entrypoints"
- **Actual** `ls bin/mini-ork*`: 13 user-facing entrypoints — `mini-ork`,
  `mini-ork-classify`, `-eval`, `-execute`, `-improve`, `-init`,
  `-invoke-prompt`, `-metrics`, `-plan`, `-promote`, `-reflect`,
  `-topology`, `-verify`. Plus `_worker-launcher.sh` (private helper) and
  `mo-check-claude-invocations` (lint tool).
- **Fix**: same as P1-1.

### P1-3. "4 new schemas + 4 new migrations" — actual 15 migrations

- **README line 234**: "4 new schemas + 4 new migrations (memory namespaces,
  benchmarks, evolution, safety)"
- **Actual** `ls db/migrations/`: 15 files numbered 0001 through 0015. The
  named themes (memory namespaces / benchmarks / evolution / safety) all
  still correspond to migrations 0009-0012, so the description is
  partially correct — but the count "4 new" is misleading at 15 total.
- **Fix**: either restore the "v0.1 delta" framing (i.e. "added 4 NEW
  schemas on top of the v0.0 baseline") OR refresh to "15 migrations,
  covering memory namespaces, benchmarks, evolution, safety, panel
  topology telemetry".

## Findings — P2 (minor / illustrative drift)

### P2-1. Roadmap v1.0 section claims recipes that already shipped

- **README line 251** (under "v1.0 future"): "Built-in recipes:
  research-synthesis, blog-post, ui-audit, db-migration, ops-runbook"
- **Actual**: all 5 of those recipes exist on disk under `recipes/` today.
  They are NOT future work; they are already shipped (presumably across
  v0.2.x → v0.3.0-rc1).
- **Fix**: move the line up to "Released" / "v0.2" with the correct
  shipping version, OR collapse the entire Roadmap section into a link
  to ROADMAP.md.

### P2-2. "8 node-type interfaces in `lib/agent_registry.sh`" — wrong path

- **README line 172**: "8 node-type interfaces | `lib/agent_registry.sh` |
  planner / researcher / implementer / reviewer / verifier / reflector /
  publisher / rollback"
- **Actual**: `lib/agent_registry.sh` stores agent VERSION RECORDS
  (`role`, `model`, `provider`, `tools`, `prompt_hash`, `task_classes`,
  `cost_profile`, `context_window`, `success_rate`, `known_failure_modes`).
  It does NOT define the 8 node-type interface set. The 8 node-type names
  themselves DO appear in `schemas/workflow.schema.json` (as the `enum`
  for `nodes[].type`) and in `bin/mini-ork-execute`'s dispatch case
  statement.
- **Fix**: change the Location column to
  `schemas/workflow.schema.json + bin/mini-ork-execute` to point readers
  at the actual definitions.

### P2-3. `config/agents.yaml` example block (lines 37-48) is illustrative, not literal

- **README lines 37-48**: shows lanes pinning `glm_lens: glm`,
  `kimi_lens: kimi`, `codex_lens: codex`, `opus_lens: opus`.
- **Actual `config/agents.yaml`**: the canonical-loop-role lanes are
  pinned first (planner / researcher / implementer / reviewer / etc) and
  the lens-lanes (glm_lens / kimi_lens / codex_lens / minimax_lens) appear
  in a later block of the same file. The README's example uses
  `opus_lens` as the 4th lens; the current refactor-audit workflow uses
  `minimax_lens` instead (per the 2026-06-04 family-diversity swap in
  commit `ef135c4` upstream).
- **Fix**: update the README's example yaml block to read
  `minimax_lens: minimax` instead of `opus_lens: opus`, and add a
  comment that `reviewer: opus` (already on the line below) IS the
  Anthropic-family arbiter, not duplicated as a lens.

## Findings — P3 (wording / nit)

### P3-1. Gate-type list says 6 but registry declares 7

- **README line 174**: "6 gate types | `lib/gate_registry.sh` |
  deterministic / reviewer / human / budget / scope / deployment"
- **Actual** `lib/gate_registry.sh::_VALID_GATE_TYPES`:
  `deterministic_verifier`, `reviewer_gate`, `human_gate`, `budget_gate`,
  `scope_gate`, `deployment_gate`, `custom`. The named 6 ARE all
  present, but a 7th `custom` slot exists for user-defined types.
- **Fix**: either bump the count to "6 built-in + 1 custom" OR drop
  `custom` from the list entirely (it's an escape hatch, not a built-in).
  Either is fine — current wording is technically wrong on the count but
  not misleading.

## Findings — ✓ confirmed-true claims

For completeness, these claims pass the audit:

- 10-second demo claim (just fixed in commit `ae54a3d` — describes dry-run
  trace + plan path + `MINI_ORK_DRY_RUN=0` to populate `task_runs`).
- 7 lib/providers/ wrappers ship exactly as listed
  (`cl_{glm,kimi,codex,deepseek,opus,sonnet,minimax}.sh`).
- 6 edge types in `schemas/workflow.schema.json` match the README list
  exactly (`depends_on`, `supplies_context_to`, `verifies`, `blocks`,
  `retries`, `escalates_to`).
- `task_runs` table has `id`, `task_class`, `recipe`, `status`, `verdict`
  columns — the SELECT query in README line 102 works as written.
- `bin/mini-ork-metrics` exists and is executable.
- 42 git commits authored by `mini-ork@local` — the publisher node
  self-commit claim is real.
- `recipes/refactor-audit/` does use 4 distinct families per cycle
  (glm_lens / kimi_lens / codex_lens / minimax_lens) — end-to-end
  verified prior session.
- `docs/EXTENSION.md`, `docs/SAFETY.md`,
  `docs/positioning/why-mini-ork.md`, `LICENSE` all exist.
- 6 papers cited with arXiv IDs are accurate (verified prior session via
  WebFetch against arxiv.org).

## Proposed punch list

Ranked by reader-impact / effort:

| # | Defect | Effort | Reader-impact | Recommended |
|---|---|---|---|---|
| P0-1 | Roadmap stale (v0.1 → v0.3) | 5 min | High — first-time readers misjudge maturity | Replace with "see ROADMAP.md" pointer |
| P0-2 | Recipes table missing 7 of 9 | 10 min | High — 78% of recipes invisible | Expand table |
| P0-3 | `install.sh --check` doesn't exist | 5 min | High — copy-paste fails | Replace with `mini-ork doctor` |
| P1-1 | lib count 13 → 38 | 1 min | Medium | Refresh number |
| P1-2 | bin count 8 → 13 | 1 min | Medium | Refresh number |
| P1-3 | migration count framing | 2 min | Medium | Clarify "added since v0.0 baseline" |
| P2-1 | v1.0 roadmap lists already-shipped recipes | 3 min | Low | Move to released section |
| P2-2 | wrong path for node-type interfaces | 2 min | Low | Fix Location column |
| P2-3 | yaml example uses opus_lens not minimax_lens | 2 min | Low | Update example |
| P3-1 | gate-type count off-by-one | 1 min | Trivial | Either fix |

**Total fix budget**: ~30 minutes to clear P0 + P1 (the publicly-misleading
defects); ~10 more minutes for P2; ~1 minute for P3. Whole pass clears in
under one hour.

## Why this audit matters now

mini-ork's pitch — "you can verify every claim by running the demo, reading
the lib, and counting the files" — is undercut when the counts in the
README are off by 3× and the recipes table hides 78% of what ships. The
detection-fingerprint claim ("audit recipe uses 4 distinct families")
remains TRUE, but readers who notice the OTHER staleness lose trust in the
verifiable claims too.

The fix is mechanical, not architectural. The framework is healthy; the
shop-window is stale.
