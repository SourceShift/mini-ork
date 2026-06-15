# ContextNest integration

mini-ork now reads from and writes to a local [ContextNest](https://github.com/SourceShift/ContextNest) HTTP service so planner and worker subagents both see fresh cross-session substrate before deciding anything.

## Motivation

Mini-ork's planner has historically pulled context only from its own sqlite
(`task_memory`, `failure_memory`, `execution_traces`). That covers prior
mini-ork runs but misses everything Claude Code captures elsewhere — schema
changes a developer made yesterday, decisions taken in an ad-hoc session, risk
flags raised in another project. A planner reading only mini-ork memory can
confidently emit a plan against an outdated schema.

The trigger was a real audit (2026-06-15): a saved memory entry asserted ten
"facts" about a chapter-anchor table; verification against live code showed
**three were wrong, two were internally inconsistent in the codebase, two
were incomplete**. A planner relying on that memory would have produced the
wrong plan. ContextNest had fresher data the whole time — nobody asked.

Three patterns we steal from recent multi-agent memory research:

- **StackPlanner (arXiv:2601.05890)** — explicit pre-fetch step ("Experience
  Search") before any planning action, plus a `REVISE` action to prune stale
  memory; ambient RAG underperforms.
- **Intrinsic Memory Agents (arXiv:2508.08997)** — scope memory by agent
  role; planner-scope ≠ worker-scope.
- **EvoMem (arXiv:2511.01912)** — outcome feedback decays unused atoms; static
  memory becomes tomorrow's drift.

This PR ships the **read side** plus **session-ingest write side** of the
integration. Outcome feedback (EvoMem) is deferred to a follow-up that needs
a new CN endpoint.

## Components

### `lib/cn_client.sh`

Bash wrapper over CN's HTTP API. Every call has a tight timeout and a silent
fallback to `{}` when CN is unreachable. Public surface:

| Function | What |
|---|---|
| `cn_available` | 0 if CN reachable (cached for `CN_PING_TTL` seconds), 1 otherwise |
| `cn_retrieve <query> [limit]` | `POST /api/v1/tools/retrieve` — semantic atoms |
| `cn_sessions_by_file <path>` | `GET /api/v1/sessions/by-file` — sessions touching path |
| `cn_sessions_by_feature <text>` | `GET /api/v1/sessions/by-feature` |
| `cn_sessions_by_intent <text>` | `GET /api/v1/sessions/by-intent` |
| `cn_inbox [limit]` | `GET /api/v1/inbox` — attention queue |
| `cn_features_recent [since] [layer]` | `GET /api/v1/features` — recent deliveries |
| `cn_hook_post <event> <session_id> [cwd] [transcript]` | Fire-and-forget `POST /api/v1/cc/hook/<event>` |
| `cn_render_atoms_md <json> [limit]` | Convert retrieve JSON to a prompt-injectable markdown block |

We do **not** wrap `/api/v1/tools/store` from mini-ork. Canonical writes go
through CN's session-ingest pipeline only — that keeps the substrate
single-entry and avoids two competing write paths.

### Planner pre-fetch (`bin/mini-ork-plan` + `lib/context_assembler.sh`)

Two new helpers run inside the existing `MO_INJECT_LEARNINGS` block in the
planner:

- `context_contextnest_atoms_md <brief> [limit]` — semantic atoms relevant
  to the kickoff title/objective/description.
- `context_contextnest_recent_sessions_md <brief> [max_files]` — recent
  sessions touching any files listed in the brief.

Both emit nothing (silent) when CN is down or `MO_DISABLE_CN=1`. Both append
to `PROMPT_TEXT` before the planner runs.

### Worker pre-fetch (`hooks/subagent-prefetch.sh`)

`UserPromptSubmit` hook for worker subagents. On the first turn (refresh
cadence controlled by `CN_PREFETCH_REFRESH_SEC`, default 30 min) it fetches:

- Semantic atoms for the prompt itself,
- Top-5 inbox items,
- Recent features (last 48h),

and writes them to `$MINI_ORK_HOME/runs/<run>/cn_prefetch/<session_id>.md`.
The worker prompt template can `cat` this file or reference its path via
`$MO_CN_PREFETCH_PATH` (TODO: export from `subagent-spawn.sh`).

### Hook mirroring (`hooks/subagent-spawn.sh` + `hooks/subagent-stop.sh`)

When mini-ork dispatches a Claude Code subagent or sees one stop, it also
POSTs to CN's existing `/api/v1/cc/hook/*` endpoints. Mini-ork subagent
sessions become first-class in the substrate alongside direct Claude Code
sessions — same downstream consolidation, same feature inventory.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `CN_BASE_URL` | `http://127.0.0.1:28080` | ContextNest server URL |
| `CN_TIMEOUT_SEC` | `2` | Read-call timeout (retrieve/by-file/etc) |
| `CN_HOOK_TIMEOUT_SEC` | `1` | Hook POST + reachability ping timeout |
| `CN_PING_TTL` | `30` | Seconds to cache reachability state |
| `MO_DISABLE_CN` | unset | `1` → every CN call short-circuits, no network |
| `CN_PREFETCH_REFRESH_SEC` | `1800` | Worker prefetch refresh cadence (30 min) |

## Failure modes

- **CN down** — every helper returns `{}` or empty string. Planner gets no
  CN block; worker prefetch file is absent. Mini-ork never blocks.
- **CN slow** — `CN_TIMEOUT_SEC` clips reads at 2s; hooks at 1s. Above
  threshold, treated as down.
- **Stale CN data** — accepted. The audit motivating this PR is the proof:
  fresher than no data, never canonical. Workers should still verify against
  live code before acting on a CN atom — same rule as for any memory source.

## Wiring the prefetch hook in a worker config

Add to the worker's `.claude/settings.json` (whichever spawn template mini-ork
uses to launch worker Claude sessions):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "command": "<mini-ork>/hooks/subagent-prefetch.sh" }
    ]
  }
}
```

## Testing

```bash
bash tests/unit/test_cn_client.sh          # 8 cases, in-process http stub
bash tests/unit/test_context_assembler.sh  # 9 cases incl. CN-disabled paths
```

Both suites are fully hermetic — no live CN required.

## What's deferred to a follow-up PR

- **Outcome feedback loop** (EvoMem pattern): after `subagent_stop`, POST
  `{atom_ids_used[], outcome, evidence}` to a new CN endpoint that decays
  unused atoms and promotes successful ones. Needs CN code changes.
- **Single-call context-pack endpoint** on CN: today the worker prefetch
  makes 3 sequential calls (retrieve + inbox + features). One CN-side
  composer would cut latency and let CN apply better scoring across types.
- **Auto-export of `MO_CN_PREFETCH_PATH`** to worker env so worker prompt
  templates don't have to reconstruct the path from `session_id`.
