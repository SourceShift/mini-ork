# Stage-2 P0 keystone: `POST /api/v1/app-conversations` (spawn + descriptor)

## Problem

The OpenHands agent-canvas FE (`ui/`) is conversation-shaped end-to-end, but the
mini-ork agent-server shim (`mini_ork/web/routes/agent_server.py`) is a Slice-1
**handshake only** — it has NO conversation lifecycle. Its own docstring says it
"does NOT yet wire conversations/events to real mini-ork runs" (`agent_server.py:22`).
The FE's `useCreateConversation` mutation POSTs to `/api/v1/app-conversations`
(the upstream handler the mock mirrors at `ui/src/mocks/conversation-handlers.ts:357`)
and the whole canvas cannot start a run without it. This is the 3/3-consensus
**keystone** from the coverage audit (`specs/openhands-native-surface.coverage.md`)
and FR-002 in `specs/openhands-native-surface.spec.md` (rescoped 2026-08-26).

This kickoff ships the **spawn half** of FR-002 — the minimum that makes AC-002
reachable: create a conversation → a real mini-ork run spawns → a descriptor
comes back that the conversation view can consume. The event-stream projection
(mo_events → oh_event) is a deliberate FOLLOW-UP slice, not this one.

## Definition of Done

A new backend route module `mini_ork/web/routes/app_conversations.py` providing:

1. `POST /api/v1/app-conversations` — accept the FE conversation-create payload,
   derive `(recipe, kickoff_markdown)` from it, call
   `mini_ork.web.control.launch_run(home, recipe, kickoff_markdown, run_id=None)`
   (the exact spawn primitive `runs.py:33` already uses), and return an
   **AppConversation descriptor** whose `conversation_id` IS the minted `run_id`.
2. `GET /api/v1/app-conversations/{conversation_id}` — return the descriptor for
   a known run (map run state → the descriptor's status field).
3. `GET /api/v1/app-conversations/{conversation_id}/start-tasks` — return the
   run-state → `ExecutionStatus`/`SandboxStatus` mapping the FE expects.
4. `GET /api/v1/app-conversations[/search]` — list known projected conversations
   (may be backed by the runs the shim has spawned this session; empty list is a
   valid P0 answer).

Wire the new router into `mini_ork/web/app.py` alongside the existing
`agent_server` router (additive — do NOT modify agent-server semantics).

**Descriptor shape:** mirror the FE's own `createConversationResponse`
(`ui/src/mocks/conversation-handlers.ts:203`) so the conversation view round-trips.
Read that function + the upstream branch at `:357` and match its field names
verbatim (do not invent a shape). The `@openhands/typescript-client` type for the
response is the source of truth for required keys.

## Files in scope

Write (additive only):
- `mini_ork/web/routes/app_conversations.py` — NEW, the 4 routes above.
- `mini_ork/web/app.py` — register the new router (one `include_router` line +
  import). Do not touch any other route wiring.
- `tests/web/test_app_conversations.py` — NEW, the tests below.

Read-only grounding (cite these; do not edit):
- `mini_ork/web/routes/agent_server.py` — the shim this extends (no conv lifecycle).
- `mini_ork/web/routes/runs.py` — the existing `control.launch_run` call pattern.
- `mini_ork/web/control.py` — `launch_run` signature + return dict (`ok`, `run_id`, …).
- `mini_ork/web/routes/run_detail.py:22-48` — run row + `stale`/state fields to map.
- `ui/src/mocks/conversation-handlers.ts:203,357,370` — the descriptor contract.
- `ui/src/hooks/mutation/use-create-conversation.ts` — the request payload shape.

## Auth

Per FR-NEW-11 (the per-endpoint auth table): the spawn POST is a privileged
write and SHALL require the Bearer token via `mini_ork.web.auth.require_token`
(same dependency `runs.py:30` uses, fail-closed). The GET reads are unauthenticated
(read posture, matching the shim). Do not invent a new auth scheme.

## Success Criteria

- All 4 routes exist and are registered; `python3.11 -c "import mini_ork.web.app"`
  imports clean.
- `POST /api/v1/app-conversations` with a valid Bearer token + a body naming a
  recipe spawns a run (via `control.launch_run`) and returns a descriptor whose
  `conversation_id`/`id` equals the run_id.
- Missing/invalid token → 401/403 (fail-closed), never an unauthenticated spawn.
- `tests/web/test_app_conversations.py` passes: (a) POST spawns + returns a
  run_id-keyed descriptor (mock/patch `control.launch_run` so no real run fires),
  (b) auth fail-closed, (c) GET start-tasks maps a known run state, (d) app
  imports with the new router mounted.

## Proof of success

```
python3.11 -m pytest tests/web/test_app_conversations.py -q && \
python3.11 -c "import mini_ork.web.app; print('app-import OK')" && echo PASS
```

## Non-goals

- Do NOT implement the mo_events → oh_event stream projection (follow-up slice).
- Do NOT modify `agent_server.py` handshake routes or any existing `/api/v1/*`
  semantics (additive only).
- Do NOT build any FE component — this is backend-only; the FE already calls the
  route.
- Do NOT touch `recipes/frontier-llm-research/*` (a concurrent session owns it).
