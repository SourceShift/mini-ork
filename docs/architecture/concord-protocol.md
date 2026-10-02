# Concord — cross-agent coordination protocol

Concord lets independent agents notice that they overlap — the same files, shared
settings, the same work — and get them arranged on **every turn**, not just at
start-up. It works with any harness: Claude Code, codex, opencode, mini-ork node
workers, or a shell loop that spawns short-lived workers.

The authority is **ContextNest** (`/api/v1/coord/*`, default
`http://127.0.0.1:28080`). mini-ork ships the client (`mini-ork concord …`).

This document pins the **P0 wire contract**: identity (principals and worker
bindings) and principal-addressed messaging. Later phases (intents, footprints,
change feed, conflicts) extend it; they do not change P0.

## Why principals

A *session* is the wrong unit of identity. Loop workers are short-lived
`claude --print` processes whose session ids change every step, and an
interactive session gets a new id after a restart. Messages and claims
addressed to a session are lost when it exits.

A **principal** is the stable "who": a loop, a mini-ork run, an operator's
session lineage, a human. Workers bind to a principal, and they inherit it
through the `CONCORD_PRINCIPAL` environment variable, which every child
process gets for free.

```mermaid
flowchart LR
    L["principal loop:surface-craft-rsi<br/>pgid 36656 · pane %94"] --> W1["worker cc:1d64b1<br/>(claude --print, step 1)"]
    L --> W2["worker cc:b0554e<br/>(step 2)"]
    L --> MB[(mailbox)]
    H["principal human:amir"] -- "send" --> MB
    MB -- "delivered at the next worker's turn" --> W2
```

## Principal ids

`<kind>:<name>`. The kind is one of `loop`, `run`, `session`, `human`, `agent`.
The name matches `[A-Za-z0-9._@/-]{1,128}`. Full regex:

```text
^(loop|run|session|human|agent):[A-Za-z0-9._@/-]{1,128}$
```

## Endpoints (ContextNest, prefix `/api/v1/coord`)

| Method + path | Body | Success | Errors |
|---|---|---|---|
| `PUT /principals/{principal_id}` — upsert **and** heartbeat | `PrincipalUpsert` | `200 {"principal": Principal, "unacked_messages": int}` | `400` bad id |
| `GET /principals?status=active\|all` (default `active` = live + idle) | — | `200 {"count": int, "principals": [Principal]}`, newest `last_seen` first | — |
| `GET /principals/{principal_id}` | — | `200 Principal` | `404` |
| `DELETE /principals/{principal_id}` — mark ended (mailbox and bindings are kept) | — | `200 Principal` (status `ended`) | `404` |
| `PUT /bindings/{worker_id}` — bind a worker (session id, pid tag, …) to a principal; rebinding replaces | `{"principal_id": str, "pid": int?}` | `200 Binding` | `404` unknown principal |
| `GET /bindings/{worker_id}` | — | `200 Binding` | `404` |
| `POST /principals/{principal_id}/messages` | `{"from": str, "body": str}` (body 1–8192 bytes) | `201 Message` | `404` unknown principal, `400` empty or oversized body |
| `GET /principals/{principal_id}/messages?unacked=true` | — | `200 {"messages": [Message]}`, oldest first | `404` |
| `POST /principals/{principal_id}/messages/{msg_id}/ack` — idempotent | `{"by": str}` | `200 Message` | `404` |

Ids in paths are URL-encoded (`loop%3Asurface-craft-rsi`).

### Shapes

```jsonc
// PrincipalUpsert — every field optional; absent fields keep their stored value
{ "harness": "shell|claude-code|codex|opencode|mini-ork|other",
  "host": "mac-mini", "cwd": "/abs/path", "worktree": "/abs/path" ,
  "pgid": 36656, "pids": [36656, 61062], "tmux_pane": "%94",
  "kill_recipe": ["kill -TERM -36656"], "priority": 0,
  "labels": {"parent": "loop:outer"} }

// Principal — server-computed fields marked *
{ "principal_id": "loop:surface-craft-rsi", "kind": "loop",   // kind* from the id prefix
  "harness": "shell", "host": "mac-mini", "cwd": "…", "worktree": null,
  "pgid": 36656, "pids": [36656, 61062], "tmux_pane": "%94",
  "kill_recipe": ["kill -TERM -36656"], "priority": 0, "labels": {},
  "started_at": "RFC3339",   // * first upsert, or the upsert that re-opens an ended principal
  "last_seen":  "RFC3339",   // * every upsert
  "ended_at":   null,        // * set by DELETE, cleared by a later upsert
  "status": "live|idle|stale|ended" }  // * computed on read (below)

// Binding
{ "worker_id": "cc:4cbe6fa5-…", "principal_id": "loop:…", "pid": 11408, "bound_at": "RFC3339" }

// Message
{ "msg_id": "M-42", "principal_id": "loop:…", "from": "human:amir", "body": "…",
  "created_at": "RFC3339", "delivered_at": null, "delivered_to": null,
  "acked_at": null, "acked_by": null }
```

### Status

Evaluated at read time, in order:

1. `ended` if `ended_at` is set.
2. `live` if `now − last_seen ≤ TTL`. TTL comes from `CONTEXTNEST_COORD_PRINCIPAL_TTL_SECS`, default 90.
3. `idle` if past the TTL, but the principal's `host` is the server's host and any of
   its `pids` is still alive (`kill(pid, 0)` succeeds or returns `EPERM`).
4. `stale` otherwise.

Liveness comes from **activity**. A client heartbeats by re-sending the upsert,
so a server restart heals within one heartbeat interval.

### Persistence

Principals, bindings and messages persist in SQLite at `CONTEXTNEST_COORD_DB`
(default: `coord.db` next to the WAL file). Leases stay ephemeral, as they are
today.

## Client — `mini-ork concord`

| Command | Behaviour |
|---|---|
| `concord run --name N [--kind loop] [--heartbeat-secs 30] -- CMD…` | Starts `CMD` in a new process group with `CONCORD_PRINCIPAL=<kind>:N` in its env. Registers the principal (harness `shell`, host, cwd, git worktree, pgid, live pids, `$TMUX_PANE`, kill recipe `kill -TERM -<pgid>`). Heartbeats every interval, forwards SIGINT/SIGTERM to the group, ends the principal on exit, and exits with `CMD`'s code. **Fail-open:** if ContextNest is unreachable it warns once on stderr, runs `CMD` anyway, and keeps retrying registration on each heartbeat. |
| `concord ps [--all] [--json]` | Table of PRINCIPAL, STATUS, AGE, LAST SEEN, PGID, PIDS, PANE, CWD, UNACKED. |
| `concord stop P [--signal TERM\|KILL] [--yes]` | Kills P's process group (`os.killpg`). Refuses if the pgid is missing or ≤ 1, is the caller's own pgid, or the principal's host is not this host. Without a tty it requires `--yes`. Ends the principal afterwards. |
| `concord send P MESSAGE… [--from F]` | `from` defaults to `$CONCORD_PRINCIPAL`, else `human:$USER`. |
| `concord inbox [--principal P] [--format text\|json\|prompt] [--ack]` | P defaults to `$CONCORD_PRINCIPAL`. `prompt` renders a compact block a loop can prepend to its next worker prompt. `--ack` acks what was shown. |
| `concord ack P MSG_ID` | |

Exit codes: `0` ok, `2` usage, `3` ContextNest unavailable (except `run`, which
fails open), `4` refused (for example, `stop` safety checks).

Base URL: `CN_BASE_URL` (default `http://127.0.0.1:28080`), the same variable
`mini_ork/cn_client.py` already uses.

## P0b — the per-turn hook (shipped)

ContextNest's ingest hooks run their curl in the background, so their responses
never reach Claude. Concord adds a **synchronous** hook on SessionStart and
UserPromptSubmit: `POST /api/v1/coord/turn`, with a 2 s client timeout and
`|| true`. It always returns 200.

| Step | What happens |
|---|---|
| Resolve | Use `X-Concord-Principal` (`$CONCORD_PRINCIPAL`). Otherwise use the **lineage** `session:p<pane>@<repo>`, where `%94` becomes `p94` and `<repo>` is the basename of the nearest `.git` ancestor; then `session:tty-<tty>@<repo>`; then `session:<session_id>`. |
| Bind | Upsert and heartbeat the principal, then bind `worker_id = session_id` to it. |
| Deliver | Claim the principal's undelivered messages **at most once** (per-row compare-and-swap) and return them as `hookSpecificOutput.additionalContext`. Acks stay explicit. |

The lease plane's pretool gate uses the bound principal as the lease
`agent_id`, so a restarted worker in the same lineage still owns its leases.

Every `mini-ork run` registers itself as `run:<run_id>` and exports
`CONCORD_PRINCIPAL`, so the node workers it spawns are attributed to the run. An
outer principal, such as a loop wrapped by `concord run`, becomes `labels.parent`.

## P1 — the pre-action stale-premise check

Evidence first. The P0.5 replay (`scripts/concord_replay.py`) ran over 14 days of
transcripts: 60.7k file events and 1,157 candidate overlaps. Three raters
(κ ≥ 0.93) labelled a 40-incident sample:

| Signal | Precision |
|---|---|
| Raw rules (concurrent write, stale read, logical overlap) | 30% |
| The same, after same-principal and Edit-protected-index filters | 43% |
| **An agent is about to edit a file that another principal changed since this agent last read it** | **100% (7/7)** |

So P1 interrupts **only** on that last signal, at the moment of action. This
follows CoAgent (arXiv:2606.15376): notify, don't lock or abort; the agent judges
whether the change breaks its plan. Everything weaker goes to a batched digest.

| Endpoint | Hook | Behaviour |
|---|---|---|
| `POST /api/v1/coord/footprints` | PostToolUse (async) on Read/Edit/Write/MultiEdit/NotebookEdit | Records `(principal, worker, op, path, mtime_ns, size, seq)`; the server stats the file |
| `POST /api/v1/coord/precheck` | PreToolUse (sync) on Edit/Write/MultiEdit/NotebookEdit | Warns if a write to the path exists from a principal outside P's lineage after P's latest footprint on it. It is advisory (`permissionDecision: "allow"`) and never denies in P1 |


| Phase | Adds |
|---|---|
| P0.5 | ✅ Offline replay + labelled precision (see above) |
| P1 | Footprints + pre-action stale-premise check (above); then intents (declared scope plus assumptions) and a per-turn `changes_since` digest for weaker signals |
| P2 | Strict leases for the hot set (shared config, secrets, `main` ref, DB schema, ports); `--owns` enforced per turn; publish-gate validation |
| P3 | Topic overlap through live intents; ack, escalate and freeze arbitration |
| P4 | Alone-versus-combined test validation at merge |

Design rationale and literature: see the Concord design note (2026-10-02 coordination
technique review, 1000 papers, three independent reviewers).
