---
title: Mid-node Operator Steering Injection — Research
date: 2026-06-15
status: research-complete
follows: docs/OPERATOR-STEERING.md (between-nodes steering — already shipped on this branch)
---

# Mid-node injection: how to land operator guidance INTO a running agent turn

`docs/OPERATOR-STEERING.md` (this branch) ships the between-nodes
channel: supervisor calls `mini-ork inject` while a node is finishing;
the NEXT node sees it. This document researches the gap that channel
does **not** close: what if the supervisor needs the CURRENTLY running
node — mid-LLM-call — to course-correct?

Live verified the surface from each CLI's `--help`. The picture is
better than feared: both major worker CLIs (claude, codex) expose native
mid-flight mechanisms; mini-ork just hasn't wired them.

## TL;DR

Three viable paths. Phase 1 is shippable in a single ~50-LOC PR to
`lib/providers/cl_claude.sh`; Phase 2 is similar size for `cl_codex.sh`;
Phase 3 is the durable end state.

| # | Strategy | CLI native? | Loses in-flight tokens? | Effort |
|---|---|---|---|---|
| 1 | claude `--input-format stream-json` realtime user-message injection | ✅ designed for this | NO — appended at next turn boundary | ~50 LOC |
| 2 | codex SIGTERM + `codex fork <session_id> "OPERATOR STEERING: ..."` | ✅ supported flow | YES — current LLM response discarded | ~80 LOC |
| 3 | MCP `mini-ork-steering` server with `get_operator_steering` tool the agent calls between rounds | ✅ both CLIs support `--mcp-config` / `mcp add` | NO — pull-based; the agent decides when | ~300 LOC + recipe-prompt edits |

## Phase 1 — claude stream-json injection (shippable today)

### CLI-level evidence

From `claude --help`:

```
--input-format <format>     Input format (only works with --print):
                            "text" (default), or "stream-json"
                            (realtime streaming input)
--output-format <format>    "text" (default), "json", or "stream-json"
--replay-user-messages      Re-emit user messages from stdin back on
                            stdout for acknowledgment (only works with
                            --input-format=stream-json and
                            --output-format=stream-json)
--include-partial-messages  Include partial message chunks as they
                            arrive (only works with --print and
                            --output-format=stream-json)
--include-hook-events       Include all hook lifecycle events in the
                            output stream (only works with
                            --output-format=stream-json)
```

That's a complete bidirectional pipe: supervisor pushes user-shaped JSON
into stdin while claude streams events out of stdout. Mid-turn injection
is the documented design intent.

### Wire architecture

```
                                       ┌──────────────────┐
[mini-ork-execute]                     │ claude --print    │
        │                              │   --input-format  │
        ├─ fifo stdin ────►            │     stream-json   │
        │                              │   --output-format │
        ├─ fifo stdout ◄──────         │     stream-json   │
        │                              │   --replay-user-msgs│
[mini-ork supervisor sidecar]          └──────┬───────────┘
        │                                     │
        ▼                                     ▼
   operator_steering table ◄── poll ──── (LLM agentic loop)
        ▲
        │ mini-ork inject ...
        │
   [Claude supervisor session]
```

### lib/providers/cl_claude.sh — sketch

Today `cl_claude.sh` does the moral equivalent of:

```bash
claude --print --output-format text "$PROMPT" > "$OUT" 2> "$ERR"
```

The mid-flight variant becomes:

```bash
# Per-run stdin fifo for supervisor injection
FIFO_IN="${MINI_ORK_RUN_DIR}/.claude-stdin.fifo"
mkfifo "$FIFO_IN"

# Initial user message
printf '%s\n' "$(jq -nc --arg t "$PROMPT" \
  '{type:"user",message:{role:"user",content:[{type:"text",text:$t}]}}')" \
  > "$FIFO_IN" &

# Boot claude in stream-json mode
claude \
  --print \
  --input-format stream-json \
  --output-format stream-json \
  --include-partial-messages \
  --replay-user-messages \
  < "$FIFO_IN" \
  > "$OUT" 2> "$ERR" &
CLAUDE_PID=$!

# Sidecar that watches operator_steering, pushes new rows into the fifo
( while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    rows="$(operator_steering_fetch_for "$MINI_ORK_RUN_ID" "implementer" \
              | head -3)"
    [ -n "$rows" ] && while IFS= read -r r; do
      msg="$(jq -r '"OPERATOR STEERING: " + .message' <<<"$r")"
      printf '%s\n' "$(jq -nc --arg t "$msg" \
        '{type:"user",message:{role:"user",content:[{type:"text",text:$t}]}}')" \
        > "$FIFO_IN"
    done <<<"$rows"
    sleep 5
  done ) &
SIDE_PID=$!

wait "$CLAUDE_PID"
kill "$SIDE_PID" 2>/dev/null || true
rm -f "$FIFO_IN"
```

The supervisor sidecar polls the operator_steering table every 5s while
claude is running; new rows become user-shaped messages written to the
fifo; claude picks them up at its next turn boundary (between tool
calls). No in-flight token loss.

### Why not faster than 5s polling?

The MINI_ORK_ON_EVENT hook (PR #12 on this same branch) already lets the
supervisor push events out at < 1s. If you want < 1s injection too,
the sidecar can subscribe to a named pipe the supervisor writes to,
removing the poll. ~10 extra LOC. Keep it simple in v1.

## Phase 2 — codex fork (shippable, but interrupts the call)

### CLI-level evidence

From `codex --help`:

```
exec      Run Codex non-interactively
resume    Resume a previous interactive session (picker by default; use
          --last to continue the most recent)
fork      Fork a previous interactive session (picker by default; use
          --last to fork the most recent)
```

Codex sessions persist to `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
as append-only conversation state. Each line is a typed event:

```json
{"timestamp":"2026-05-27T06:46:07.215Z","type":"session_meta","payload":{"id":"019e682e-eb91-72e2-9be8-5cfd0ec02250","cwd":"...","cli_version":"0.133.0",...}}
{"timestamp":"...","type":"user_message","payload":{...}}
{"timestamp":"...","type":"agent_response","payload":{...}}
```

`codex fork <SESSION_ID> "OPERATOR STEERING: ..."` branches off the
existing session at the head, treats the steering text as the next
user turn, and continues. The previous in-flight LLM response is
discarded.

### Wire architecture

```
mini-ork dispatcher
   │
   ├─ capture codex session_id at exec start (parse from stderr or
   │  session_meta line in the rollout JSONL once it appears)
   │
   ├─ run codex exec in background, watch operator_steering
   │
   └─ on row matching this run_id + "implementer":
        SIGTERM codex (in-flight response lost)
        codex fork "$SESSION_ID" "OPERATOR STEERING: <msg>" \
          --output-format text \
          > "$OUT" 2> "$ERR"
```

### Why this is heavier than Phase 1

Two costs:

1. **In-flight LLM response is discarded** — codex was mid-response;
   SIGTERM cuts it. The fork starts fresh. For agentic tool-use loops
   this is usually fine (the agent re-derives state from disk), but
   for pure-synthesis nodes (e.g. summarisation) it wastes the work
   already done.

2. **Session-id capture** requires parsing codex's startup output (or
   scanning the rollout dir for the newest file). Not hard but not zero.

Phase 2 is the right answer when claude isn't the lane — codex doesn't
expose realtime stdin injection.

## Phase 3 — MCP `mini-ork-steering` server (cleanest end state)

### Architecture

Run an MCP server (Python or shell) that exposes ONE tool:

```typescript
// (TypeScript pseudo, real impl could be Python or bash + jq)
tool({
  name: "get_operator_steering",
  description: "Returns any unconsumed operator guidance for the current run + agent role. Call this between major decisions — between file edits, before committing, when you're stuck on what to do next.",
  inputSchema: {
    run_id: "string",
    role:   "string",   // "implementer" | "reviewer" | "verifier"
  },
  handler: (args) => operator_steering_fetch_for(args.run_id, args.role),
});
```

Register with claude via `--mcp-config "$MINI_ORK_ROOT/mcp/steering.json"`
or with codex via `codex mcp add mini-ork-steering ...`.

Add a single line to every worker prompt (`prompts/implementer.md` etc):

> Before any non-trivial decision, call `get_operator_steering(run_id="<run_id>", role="<your role>")` and incorporate any returned guidance.

### Why this is the durable end state

- **Pull-based, no SIGTERM, no fifo plumbing.**
- **The agent decides when** — natural breakpoints (between tool calls)
  rather than the supervisor having to guess timing.
- **Same surface for every CLI that supports MCP** — claude, codex,
  future workers.
- **The operator_steering table this branch ships becomes the MCP
  server's only state.** No new schema.

### Why we don't do it first

- **Recipe-prompt edits required** — every `prompts/implementer.md`
  needs the "always check steering" line. That's recipe-author work,
  not framework work.
- **Reliability depends on the agent actually calling the tool.** A
  cheap-lane model that ignores tool-use instructions defeats the
  channel. Need empirical measurement to know which lanes comply.
- **Bigger upstream surface** — adds a `bin/mini-ork-mcp-steering`,
  cross-CLI register helpers, prompts edits.

## Recommendation

1. **Today**: ship Phase 1 in a follow-up PR on this branch. ~50 LOC
   in `lib/providers/cl_claude.sh` + a sidecar function in
   `lib/mid_node_injector.sh`. Closes the claude-lane gap immediately.

2. **Within 2 weeks**: ship Phase 2 for codex. Wraps the resume/fork
   primitive. ~80 LOC in `lib/providers/cl_codex.sh`.

3. **Within 1 month**: prototype Phase 3 MCP server. Measure adoption
   rate by lane. If `tool_call_compliance ≥ 80%` across deployed
   workers, deprecate Phase 1 + 2 wrappers and consolidate.

## Composability with existing channels

| Channel | Cadence | Mid-call? | Strategy |
|---|---|---|---|
| Task-class memory | every node | NO | planner only |
| Cross-class gradients | every reflect | NO | next planner pulls |
| Patterns | every reflect | NO | gate consults |
| Bug reports | every node + reflect | NO | next planner pulls |
| Operator steering between nodes (THIS BRANCH) | on-demand | NO | next node's context_assemble |
| **Operator steering mid-node (Phase 1+2+3, THIS DOC)** | **on-demand** | **YES** | **claude stream-json / codex fork / MCP tool** |

The five existing channels feed the **next** decision point. Adding
mid-call injection feeds the **current** decision point. Together they
form a complete supervisor surface — every moment of an agent's
existence has a load-bearing input channel from the operator.

## Out of scope (for now)

- **Pause/resume semantics across SIGSTOP/SIGCONT** — fragile, OS-specific.
- **Conversation-file in-place patching** — racy with the CLI's own
  writes; better to use the CLI's documented resume/fork primitives.
- **Bypass via TCP/IPC into the running LLM HTTP stream** — would
  require provider cooperation; out of mini-ork's control surface.

## Phase tracker

| Phase | Status | This research doc's relation |
|---|---|---|
| A — DF cycles | ✅ | n/a |
| B — lib→bin wiring | ✅ advanced | Phase 1+2 implementations will sit in `lib/providers/cl_*.sh` |
| C — measurable improvement | partial | mid-call steering eliminates wasted iter cost when supervisor spots the issue mid-flight |
| D — scale-ready | unchanged | n/a — Phases 1-3 scale identically to existing dispatch |
| E — substrate (self-improving) | **closing the supervisor loop** | this doc + PR #13 + PR #12 together complete the bidirectional learning channel |
