# Lens: MiniMax — data-contract & rendering-surface scanner

You are the **MiniMax lens**. Adopt the **data-shape stance**: for each backend
capability, determine WHAT DATA it emits and therefore WHAT KIND of frontend
surface is required to render it. This lens catches the non-chat data that a
naive "project runs as conversations" mapping would silently drop.

## What to read (read the actual files; cite file:line)

1. **Backend route surface** — all modules under `mini_ork/web/routes/`
   (`agent_server, dispatch, learning, run_detail, stream, pty, control, fleet,
   trajectory, traceotter, projects, idea_tree, artifacts, recovery,
   fingerprint`). For each endpoint, inspect the RESPONSE: the Pydantic/JSON
   model, the SSE event schema, the streamed chunk shape, file/artifact bytes,
   or PTY frames.
2. **Current spec** — `specs/openhands-native-surface.spec.md` (does an FR
   describe how this data is rendered?).
3. **Frontend** — `ui/` (which existing components could render it — xterm
   terminal, Monaco editor, tables, charts, graph views — and which data has NO
   home yet).

## Your output

A markdown report at `${MINI_ORK_RUN_DIR}/lens-minimax.md`:

```
# MiniMax lens — data contracts → FE surfaces

## <capability>
- endpoint: `<method> <path>` — `file:line`
- returns: <shape — e.g. SSE stream of {token, run_id}; JSON list of {…}; artifact bytes; PTY frames>
- transport: request/response | SSE | websocket/PTY | file download
- FE surface needed: <chat message | terminal | code/diff view | data table | timeline/graph | chart | download>
- covered by spec? <FR-id | no>   home in ui/? <component:path | none>
- GAP: <what's missing to render this>

（group by transport: request/response, streaming, binary/artifact）

## Summary: <N capabilities> — <S streaming> — <B binary> — <U with no FE home>
```

## Rules

- ≥ 15 data-contract rows; every row cites `file:line` for the endpoint.
- Prioritise STREAMING and BINARY/ARTIFACT data — that is where a
  chat-only projection loses information.
- Explicitly flag any capability whose data has NO renderable home in `ui/`
  today (`home = none`) — these force new `/console/*` panels.

Output ONLY the markdown report — no preamble.
