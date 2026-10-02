# mini-ork + ContextNest coordination state (snapshot 2026-10-02)

What already exists for coordinating concurrent agents, WHEN each piece is
checked, and the failures it misses. Reviewers: "already-shipped" means the
mechanism below exists AND is wired; a built-but-unwired piece makes a
technique "extends", not "already-shipped".

## The failure class this protocol must close

Observed live (2026-10): two shell-launched RSI loops (`run-rsi.sh`, each with
a long-lived mini-ork child process) ran in tmux panes alongside an
interactive Claude session working the same files. No supervisor knew about
the others. The loops spawn a fresh short-lived `claude --print` worker per
step, so session ids change every step; the interactive agent sent "handover"
messages to those one-off workers, which exited unread. The interactive
agent's own session id also changed after a restart. Overlap was discovered by
a human reading a process tree, not by any check. Similar incidents in the
history: concurrent sessions editing the same in-place tree (framework-edit
had to add a pre-implementer baseline snapshot to keep the reviewer diff
clean); automation fast-forwarding `main` mid-session; tests clobbering shared
secrets; a shared `providers.yaml` shadow dropping lanes for every run; a live
goal-loop reading `.mini-ork/config` while another session edited it.

## mini-ork — shipped and WIRED

| Mechanism | Anchor | When checked | Scope / gap |
|---|---|---|---|
| CAID `--owns` claim registry | `scripts/mini_ork_worktree.py:106-182` | ONCE at `make worktree` | Path prefixes per worktree slug. No re-check while editing, no flock (TOCTOU), nothing stops edits outside the claim, invisible to agents not using worktrees |
| Worktree ref guard | `.githooks/reference-transaction` | Every git ref write | Blocks foreign-history commits and direct branch creation only; no file/topic awareness |
| Scheduler epic claim + bounded pool | `mini_ork/scheduler.py:581-591`, `run_pool:691-737` | At epic admission | Atomic DB claim of one epic; admission uses dependency readiness + budget only — no file/topic overlap gating. `MO_SCHED_PRE_DISPATCH_HOOK` (`scheduler.py:648`, defer rc=75) exists as an extension seam |
| Single-writer run lease | `mini_ork/stores/lease.py:112-191`, `checkpoints.py:319-330` | Recovery dispatch + checkpoint publish | TTL + fencing token per run_id. Two different runs on the same repo are invisible to it |
| Stale-heartbeat watchdog | `mini_ork/cli/execute.py:1378-1420` | Before each LLM node dispatch | Liveness inside one run's DAG only |
| Pre-implementer baseline snapshot | `mini_ork/cli/execute.py:1524-1588` | Once per run | Post-hoc diff isolation from a concurrent session's dirt; does not detect or block it |
| Publisher strict-path commit | `mini_ork/cli/publisher.py:46-136`; pluggable `gate_run_all` at `:149-169` | Publish node | Never `git add -A`; no probe for another run committing to the same repo |
| Goal-loop protected-path guard | `recipes/goal-loop/lib/transforms.py:532-699` | Once per deploy | Static glob list, inert unless `MO_GOAL_PROTECTED_PATHS` set; one child's own deploy only |
| Remote tree-sync conflict | `mini_ork/remote/tree_sync.py:238-273` | Each sync round-trip | Whole-tree hash, one local↔remote pair, not agent↔agent |
| ContextNest "same file / same intent" pack | `mini_ork/cn_client.py:125-140`, `steering/context_role_packs.py:97-112` | ONCE at plan assembly, planner node only | Advisory text; never re-queried by later nodes or turns |

## mini-ork — BUILT but UNWIRED (no production caller)

| Mechanism | Anchor | Notes |
|---|---|---|
| Path-prefix lease registry with read/write modes, TTL, flock, wound-wait deadlock victim | `mini_ork/registries/coord_registry.py:290-421`, CLI `mini-ork coord acquire/release` | Only runs when something shells out to it |
| PreToolUse-shaped coord gate (advisory nudge / strict deny) | `mini_ork/gates/coord_gate.py:453-505` | Exactly the shape of a per-tool-call hook; no caller in executor, ACP bridge, or dispatch |
| Epic-vs-epic file/symbol overlap detector + union-find serialization partitions | `mini_ork/gates/scope_overlap.py:295-502` | Ported and tested; `scheduler.py` never calls it. Knows a "shared trunk" list (incl. `.mini-ork/config/*`) that is never evaluated at edit time |
| SharedDrive (run-scoped path containment) | `mini_ork/runtime/shared_drive.py` | "nothing imports it yet" |
| Human oversight inbox | `mini_ork/gates/oversight_inbox.py` | "deliberately passive: nothing in the executor calls it" |

## ContextNest (Rust service, :28080) — shipped

| Capability | Anchor | Freshness | Gap |
|---|---|---|---|
| Lease registry (agent_id, fleet_id, paths, read/write, priority, TTL, strict) + contention audit ring + metrics | `src/api/coord.rs` — `POST/DELETE/PUT /api/v1/coord/lease*`, `GET /api/v1/coord/leases`, `/coord/audit`, `/coord/metrics` | Real-time, in-memory, sub-ms | In-memory only (lost on restart); agent_id is free text; advisory unless a scope is strict |
| PreToolUse lease gate (WAIT advisory / strict deny) | `src/api/cc_hooks.rs:395-460` — `POST /api/v1/cc/pretool` | Per Edit/Write tool call | NOT auto-installed (`install-hooks` wires only SessionStart, UserPromptSubmit, Stop, TaskCompleted); agent_id = session_id, so a restarted worker is a stranger to its own lease |
| files_touched index | `src/ingest/claude_code/extractor.rs:340-650` → `GET /api/v1/sessions/by-file?path=` | Per Stop/UserPromptSubmit batch or ≤30s sweeper | Retrospective (edits already made); no `since` filter |
| Topic overlap | `GET /api/v1/sessions/by-intent?q=` (cold-embeds each session's latest domain+goal+state) | Per-turn signal, ~5s over ~2.4K sessions | No recency filter; cost scales with all sessions, not live ones |
| z-insight blocks (work_unit id, goal, tasks, files, decisions) | `docs/z-insight-schema.md`, extractor | Per assistant turn (Stop hook) | Only for sessions that emit the block; work_unit is not used for identity anywhere |
| Attention inbox (`requires_user_action`, todos) | `GET /api/v1/inbox` | Per turn | Human-facing; no agent↔agent arrangement |
| MCP tools (14 `cn_*`) | `src/mcp/tools.rs:66-301` | — | None expose `coord/*`; MCP-only agents cannot claim or check leases |

## Cross-cutting gaps (what nothing covers today)

1. No per-turn check anywhere in mini-ork; every wired check fires at a coarse
   boundary (worktree create, epic admission, node dispatch, publish).
2. No stable agent identity above the session id: no loop/fleet/work-unit
   identity binding pid, tmux pane, cwd, and worktree; messages and leases are
   addressed to ephemeral workers.
3. File overlap (leases, files_touched) and topic overlap (by-intent) are
   disconnected; nothing produces "these two agents share a concern right now".
4. No shared-settings/config collision detection at edit time.
5. Detection without arbitration: nothing decides who yields, who merges, or
   how a paused agent is resumed with the other's outcome.
6. Shell-launched loops and non-Claude harnesses (codex, opencode, minimax/glm
   workers) emit no hooks into ContextNest unless explicitly wired.
