# ContextNest integration

mini-ork reads from and writes to a local [ContextNest](https://github.com/SourceShift/ContextNest) HTTP service so planner and worker subagents both see fresh cross-session substrate before deciding anything.

## Status (as of 2026-06-17)

| Epic phase | Status | PR |
|---|---|---|
| **v1 bridge** (planner CN injection + worker prefetch hook) | ✅ shipped | mini-ork #17 |
| **PR-1 capsule swap** (planner prefers `/prompt-context/capsule` over flat retrieve) | ✅ shipped | mini-ork #21 |
| **PR-2 consolidation backoff** (CN-side: rate-limit detect + exp backoff + adaptive concurrency) | ✅ shipped | ContextNest #159 |
| **PR-3 role-tailored ContextPacks** (8 workflow node types → role-specific endpoint compositions) | ✅ shipped (this PR) | mini-ork #?? |
| **PR-4 worker prompt wiring** (`MO_CN_PREFETCH_DIR` + Step 0 prompt sections) | ✅ shipped | mini-ork eb9bd5d + restoration #22 |
| **PR-5 composed CN endpoint** | ⏸ gated on PR-3 latency measurement | — |
| **PR-6 outcome feedback loop** (EvoMem pattern) | ⏸ planned | — |

Full epic spec: `docs/roadmap/epics/agent-context-pack.md` in the ContextNest repo.

## Motivation

Mini-ork's planner has historically pulled context only from its own sqlite (`task_memory`, `failure_memory`, `execution_traces`). That covers prior mini-ork runs but misses everything Claude Code captures elsewhere — schema changes a developer made yesterday, decisions taken in an ad-hoc session, risk flags raised in another project. A planner reading only mini-ork memory can confidently emit a plan against an outdated schema.

The trigger was a real audit (2026-06-15): a saved memory entry asserted ten "facts" about a chapter-anchor table; verification against live code showed **three were wrong, two were internally inconsistent in the codebase, two were incomplete**. A planner relying on that memory would have produced the wrong plan. ContextNest had fresher data the whole time — nobody asked.

Three patterns from recent multi-agent memory research drive the design:

- **StackPlanner (arXiv:2601.05890)** — explicit pre-fetch step ("Experience Search") before any planning action, plus a `REVISE` action to prune stale memory; ambient RAG underperforms.
- **Intrinsic Memory Agents (arXiv:2508.08997)** — scope memory by agent role; planner-scope ≠ worker-scope. **PR-3 lands this.**
- **EvoMem (arXiv:2511.01912)** — outcome feedback decays unused atoms; static memory becomes tomorrow's drift. **Deferred to PR-6.**

## Components

### `lib/cn_client.sh`

Bash wrapper over CN's HTTP API. Every call has a tight timeout and a silent fallback to `{}` (or empty string for markdown endpoints) when CN is unreachable. Public surface:

| Function | What | Endpoint |
|---|---|---|
| `cn_available` | 0 if CN reachable (cached for `CN_PING_TTL` seconds), 1 otherwise | `GET /api/v1/substrate/health` |
| `cn_retrieve <query> [limit]` | Semantic atoms (JSON `{hits[]}`) | `POST /api/v1/tools/retrieve` |
| `cn_capsule [query] [since] [project]` | **PR-1.** Kind-ordered markdown digest (risks → decisions → failures → ...) | `GET /api/v1/prompt-context/capsule` |
| `cn_sessions_by_file <path>` | Sessions touching a file | `GET /api/v1/sessions/by-file` |
| `cn_sessions_by_feature <text>` | Sessions matching a feature text | `GET /api/v1/sessions/by-feature` |
| `cn_sessions_by_intent <text>` | Sessions matching an intent text | `GET /api/v1/sessions/by-intent` |
| `cn_inbox [limit]` | Attention queue items | `GET /api/v1/inbox` |
| `cn_inbox_filtered [urgency] [limit]` | **PR-3.** Inbox filtered by urgency tier (now/soon/later) | `GET /api/v1/inbox?urgency=...` |
| `cn_features_recent [since] [layer]` | Recent delivered features | `GET /api/v1/features` |
| `cn_basins [project] [limit]` | **PR-3.** Topic-cluster basins (attractor-formed) | `GET /api/v1/field/basins` |
| `cn_connections_for <node_id> [limit]` | **PR-3.** Graph neighbours of a fragment | `GET /api/v1/connections` |
| `cn_hook_post <event> <session_id> [cwd] [transcript]` | Fire-and-forget hook POST | `POST /api/v1/cc/hook/<event>` |
| `cn_render_atoms_md <json> [limit]` | JSON hits → markdown block | (client-side render) |
| `cn_render_features_md <json> [cwd] [limit]` | **PR-3.** Features list → markdown block | (client-side render) |
| `cn_render_inbox_md <json> [limit]` | **PR-3.** Inbox items → markdown block | (client-side render) |
| `cn_render_basins_md <json> [limit]` | **PR-3.** Basins → markdown block | (client-side render) |

We do **not** wrap `/api/v1/tools/store` from mini-ork. Canonical writes go through CN's session-ingest pipeline only — that keeps the substrate single-entry and avoids two competing write paths.

### `lib/context_role_packs.sh` (PR-3)

Dispatch table mapping mini-ork's 8 workflow node types to role-specific CN endpoint combinations. Each role gets the slice of substrate it actually needs — pre-PR-3 every role got the same flat retrieve query.

| Role | Sub-pack composition |
|---|---|
| **planner** | capsule (since=14d) + sessions-by-intent (task_class) + inbox (urgency=now) + basins (project=cwd) |
| **researcher** | broad cn_retrieve (limit=8) + sessions-by-feature |
| **implementer** / **worker** | sessions-by-file (per file in scope) + features-recent (48h) + graph neighbours of top retrieve hit |
| **reviewer** / **verifier** | capsule (since=30d) post-hoc filtered to **only Failures + Verifications + Risks** sections (Decisions stripped) |
| **reflector** | sessions-by-intent (task_class) + capsule (since=30d) full digest |
| **publisher** / **rollback** | features-recent (24h) + inbox (all urgency tiers) |

Public entry: `context_role_pack_md <role> <task_brief_path> [files_csv]` → emits multi-section markdown. Empty when CN unreachable or `MO_DISABLE_CN=1`.

Unknown role → falls back to the generic `context_contextnest_atoms_md` from `context_assembler.sh`.

### Planner pre-fetch (`bin/mini-ork-plan`)

Inside the existing `MO_INJECT_LEARNINGS` block, runs in order:
1. **PR-3** role pack for `planner` via `context_role_pack_md`
2. Generic `context_contextnest_atoms_md` fallback (PR-1 capsule swap, then retrieve)
3. `context_contextnest_recent_sessions_md` for file-touch history

Step 1 is gated by `MO_USE_ROLE_PACKS=1` (default on); set to `0` to skip role packs and use only the generic path.

### Worker pre-fetch (`hooks/subagent-prefetch.sh` + `mini_ork/ported/mini_ork_execute.py`)

`UserPromptSubmit` hook for worker subagents (gated on `MINI_ORK_RUN_ID`). On the first turn (refresh cadence `CN_PREFETCH_REFRESH_SEC`, default 30 min) it fetches:
- Semantic atoms for the prompt itself
- Top-5 inbox items
- Recent features (last 48h)

…and writes them to `$MO_CN_PREFETCH_DIR/<session_id>.md`.

`mini_ork/ported/mini_ork_execute.py` exports `MO_CN_PREFETCH_DIR=$RUN_DIR/cn_prefetch` so all dispatched workers inherit it. The 3 default code-fix prompts (`recipes/code-fix/prompts/{planner,implementer,reviewer}.md`) include an opening **Step 0 — ContextNest prefetch** section that instructs workers to `ls {{MO_CN_PREFETCH_DIR}}` and cat any `*.md` files before reading the main inputs.

### Hook mirroring (`hooks/subagent-spawn.sh` + `hooks/subagent-stop.sh`)

When mini-ork dispatches a Claude Code subagent or sees one stop, it also POSTs to CN's existing `/api/v1/cc/hook/*` endpoints. Mini-ork subagent sessions become first-class in the substrate alongside direct Claude Code sessions — same downstream consolidation, same feature inventory.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `CN_BASE_URL` | `http://127.0.0.1:28080` | ContextNest server URL |
| `CN_TIMEOUT_SEC` | **8** (was 2 pre-PR-1) | Read-call timeout (retrieve / by-file / capsule / etc) |
| `CN_HOOK_TIMEOUT_SEC` | **3** (was 1 pre-PR-1) | Hook POST timeout (the reachability ping uses `CN_TIMEOUT_SEC` instead — PR-1 fix) |
| `CN_PING_TTL` | `30` | Seconds to cache reachability state |
| `MO_DISABLE_CN` | unset | `1` → every CN call short-circuits, no network |
| `MO_USE_ROLE_PACKS` | `1` | **PR-3.** `0` to skip role packs and use only the generic capsule-or-retrieve path |
| `CN_PREFETCH_REFRESH_SEC` | `1800` | Worker prefetch refresh cadence (30 min) |

## Failure modes

- **CN down** — every helper returns `{}` or empty string. Planner gets no CN block; worker prefetch file is absent. Mini-ork never blocks.
- **CN slow** — `CN_TIMEOUT_SEC` clips reads at 8s; hooks at 3s. Above threshold, treated as down. The reachability ping (`cn_available`) uses the read budget so it doesn't false-negative under load (PR-1 fix; previous version used the hook budget and flipped to "down" intermittently on populated substrates).
- **Stale CN data** — accepted. The audit motivating this integration is the proof: fresher than no data, never canonical. Workers should still verify against live code before acting on a CN atom — same rule as for any memory source.
- **CN under consolidation pressure** — pre-PR-2, the worker would peg CPU at >90% retrying rate-limited embedder calls. PR-2 ships exponential backoff + adaptive concurrency drop, so the worker now backs off when the embedder returns `engine_overloaded`/429/`rate_limit` and recovers when the embedder does.

## Smoke testing

Per the Smoke Test Standard in ContextNest's `docs/roadmap/epics/agent-context-pack.md`, every CN-bridge PR ships a `scripts/smoke-<pr-slug>.sh` harness that exercises the live system end-to-end and produces a human-readable evidence file at `tmp/smoke-evidence/<slug>-<ts>.md`.

| Harness | What it tests |
|---|---|
| `scripts/smoke-cn-bridge.sh` | v1 bridge end-to-end (planner CN block + worker prefetch + CN-down + MO_DISABLE_CN + CN-slow) |
| `scripts/smoke-pr-1-capsule-swap.sh` | PR-1 capsule swap (kind-headings present + timeout-default bumps + failure paths) |
| `scripts/smoke-pr-3-role-packs.sh` | PR-3 each of 6 role packs + unknown-role fallback + failure paths |

Plus the hermetic unit tests:

```bash
bash tests/unit/test_cn_client.sh          # 10 cases, in-process http stub
bash tests/unit/test_context_assembler.sh  # 9 cases incl. CN-disabled paths
```

All harnesses produce evidence files with per-assertion verdicts + captured outputs — a reviewer reads the evidence file before approving the PR.

## What's NOT in scope

- **No direct CN write path from mini-ork.** Canonical writes go through CN's session-ingest pipeline only.
- **No fallback to a different memory backend.** When CN is down, mini-ork's local sqlite (`task_memory`, `failure_memory`) carries the load alone.
- **No training of a retrieve-gating model.** Threshold-based gates only; revisit if signal proves weak.
- **No tool-call-level instrumentation.** Hook framework only — substrate ingest stays at session-transcript granularity.

## Deferred to follow-up PRs

- **PR-5 composed CN endpoint** (`POST /api/v1/agent/context-pack?role=<role>`): collapses each role pack's 3-4 sequential CN calls into one server-side composition. Gated on PR-3 latency measurement — only ship if the composed approach measurably beats per-endpoint composition (≥30% latency reduction).
- **PR-6 outcome feedback loop** (EvoMem pattern): after `subagent_stop`, POST `{atom_ids_used[], outcome, evidence}` to CN; CN bumps atom confidence on success, decays on contradiction. Needs new CN endpoint + sidecar table.

## Verification in practice (2026-06-18)

Question asked: *is mini-ork actually consuming the ContextNest ContextPack at
runtime, or just defining the plumbing?* Verified against the live daemon (CN
healthy, substrate populated at ~28k atoms / ~3.9k clusters) and against two
real run artifacts plus a direct live-test of the planner role pack.

### What works (confirmed firing)

- **Planner role-pack injection is live.** `bin/mini-ork-plan:198-199` guards on
  `declare -f context_role_pack_md` and calls
  `context_role_pack_md planner "$KICKOFF" ""`, appending the result to the
  planner prompt (gated by `MO_USE_ROLE_PACKS=1`, the default). A direct
  live-test produced a **6738-char** capsule block (Risks / Decisions /
  Failures / Directives headings present), proving CN was reached and the
  ContextPack reached the planner's prompt — not just the audit artifact.
- **`context-pack.json` is written** as an audit artifact via
  `context_assemble … planner`. Note: its *local-state* fields
  (`prior_similar_runs`, `known_failure_modes`, `similar_lessons`) read from the
  run's local `state.db`, **not** from CN. In a fresh worktree those were `0`
  while the CN-sourced capsule was simultaneously non-empty — so a zero there
  is a local-state artifact, not evidence that CN delivered nothing.

### Gaps (wired for delivery, not yet consumed)

1. **Worker prefetch dir is delivered but only consumed by `code-fix`.**
   `mini_ork/ported/mini_ork_execute.py` exports `MO_CN_PREFETCH_DIR` and
   `hooks/subagent-prefetch.sh` writes the per-session prefetch file, but only
   **3 of 160** prompt templates reference `MO_CN_PREFETCH_DIR`
   (`recipes/code-fix/prompts/{planner,implementer,reviewer}.md`). Every other
   recipe's workers — including `framework-edit` — never read the prefetched
   atoms. In both inspected `framework-edit` runs the `cn_prefetch/` dir was
   **empty**: delivery wired, consumption absent.
2. **Only the planner role pack is called.** `context_role_pack_md` has exactly
   **one** call site (`bin/mini-ork-plan:199`, planner). The
   implementer / researcher / reviewer / reflector / publisher sub-packs in
   `lib/context_role_packs.sh` are defined and unit-smoke-tested but never
   invoked by any node. Five of six role packs are dead-on-arrival at runtime.
3. **Capsule is unfiltered for markdown kickoffs.**
   `_role_pack_extract_query` picks the first ≥4-char token from the first 8
   words of the brief; for a markdown kickoff (which opens with `#`, headings,
   prose) it returns **empty**, so the planner capsule runs unscoped — a
   whole-substrate digest that pulls in unrelated atoms rather than a
   task-scoped slice.

### Bottom line

The headline path (CN → planner role-pack ContextPack → planner prompt) is real
and fires on every plan. The remaining value is locked behind wiring, not
capability: extend prefetch consumption past `code-fix`, add the five missing
role-pack call sites, and give `_role_pack_extract_query` a markdown-aware
fallback so the capsule is task-scoped.

### Resolution (2026-06-18, same day)

All three gaps fixed in-repo:

- **Gap 3 — markdown-aware query scoping.** `_role_pack_extract_query`
  (`lib/context_role_packs.sh`) now strips heading markers + code fences and
  filters structural stopwords (`kickoff`/`phase`/`goal`/`wire`/…) before
  picking a token. A markdown kickoff opening `# Kickoff: Wire grounded-rejection
  …` now scopes to **`grounded-rejection`** instead of `Kickoff`. JSON-brief and
  generic-brief behaviour unchanged (the `oracle-hardening` smoke brief still
  resolves to `Epic`, keeping the PR-3 smoke green).
- **Gap 2 — missing role-pack call sites.** Two chokepoints now invoke
  `context_role_pack_md`:
  - The native `mini_ork.ported.mini_ork_invoke_prompt` implementation behind
    `bin/mini-ork-invoke-prompt` injects a role pack keyed on
    `MINI_ORK_NODE_TYPE` for every recipe-internal node (reviewer / reflector /
    publisher / researcher …), with the *substituted* prompt text used as the
    brief.
  - `bin/_worker-launcher.sh` inlines the **implementer** pack into every
    spawned worker's prompt (previously the implementer pack was defined but
    never called).
- **Gap 1 — prefetch consumed only by `code-fix`.** `bin/_worker-launcher.sh`
  now appends a generic **Step 0 — ContextNest prefetch** instruction pointing
  at `MO_CN_PREFETCH_DIR` for *all* recipes, so the per-session prefetch file is
  no longer ignored outside the three `code-fix` prompts.

All injections are best-effort + bounded (guarded by `MO_USE_ROLE_PACKS=1` /
`MO_DISABLE_CN`, `declare -f`, and silent failure) — they can never block or
fail a plan/worker/node. Verified: PR-3 role-pack smoke 9/9, `test_cn_client`
10/10, `test_context_assembler` 7/7, `bash -n` clean on all three files, no new
shellcheck findings.
