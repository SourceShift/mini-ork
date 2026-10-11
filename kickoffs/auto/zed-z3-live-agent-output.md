# Zed Z3 — every agent's live output, streamed into the Zed thread

Plan: `docs/plans/2026-10-03-zed-integration.md`. Depends on Z2 (history/attach) merged.

## Goal

A Zed thread for a mini-ork run shows each node as a tool call, but nothing of what the
agent behind it is doing. The data exists only partly: the dispatch drain loop
(`mini_ork/dispatch/core.py` — reader threads, `open_live_writer()`) tees each stdout
line to `agent-<node>.live.jsonl` **only when `MO_LIVE_FILE` is set**, and
`mini_ork/dispatch/providers.py` sets it only for isolated/remote workspaces. A normal
local run therefore has no live sidecar for claude-CLI lanes at all. Make every node
dispatch write the sidecar, and project it into the ACP thread while the run is going —
for runs started from Zed and for runs attached with `session/load`.

## Files in scope

- `mini_ork/dispatch/providers.py` — set `MO_LIVE_FILE` for every node dispatch.
- `mini_ork/acp/live.py` — NEW: byte-offset tailer + line normalizer.
- `mini_ork/acp/agent.py` — feed normalized events into the thread.
- `tests/unit/test_acp_live.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend.
- the existing providers test that covers the isolated-workspace `MO_LIVE_FILE` branch
  (`grep -rln "MO_LIVE_FILE" tests/`) — extend, do not weaken.

No other file changes.

## Mechanism (exact spec)

### 1. Every node gets a live sidecar (`providers.py`)

Where the request env for a node dispatch is assembled (the function that today adds
`MO_LIVE_FILE` only on the isolated branch, ~line 1800), set
`MO_LIVE_FILE=<MINI_ORK_RUN_DIR>/agent-<node>.live.jsonl` whenever `MINI_ORK_RUN_DIR`
and `MO_NODE_ID` are both non-empty and `MO_LIVE_FILE` is not already set — for the host
workspace too. Use the same filename sanitization the stream sidecar uses
(`"".join(c if c.isalnum() or c in "._-" else "_" for c in node_id)`). Keep the isolated
branch's path-map translation untouched. The host child must NOT receive the variable
in a way that changes its behaviour: `core.py` already pops `MO_LIVE_FILE` from the
child env and keeps the writer in the parent — verify that stays true for the host path.

### 2. `mini_ork/acp/live.py`

```python
@dataclass
class LiveEvent:
    kind: str      # "text" | "thought" | "tool" | "tool_output"
    text: str

class LiveTail:
    def __init__(self, path: Path): ...
    def read_new(self, max_bytes: int = 256_000) -> list[dict]:   # parsed JSON objects of complete new lines
def normalize(obj: dict) -> list[LiveEvent]
```

- `LiveTail` keeps a byte offset; returns only complete new lines (a trailing partial
  line waits for the next call); tolerates a missing file (returns []), truncation
  (offset > size → reset to 0) and non-JSON lines (skipped).
- `normalize` handles, and ignores everything else:
  - claude stream-json: `{"type":"assistant","message":{"content":[...]}}` → each
    `text` block → `text`; `thinking` block → `thought`; `tool_use` block →
    `tool` with `"<name>: <compact one-line json of input, max 200 chars>"`;
    `{"type":"user", ... "tool_result" ...}` → `tool_output` (first 400 chars of the
    result text). Ignore `stream_event` partial deltas, `system`, `result`.
  - codex JSONL: `{"type":"item.completed","item":{...}}` with `item.type` →
    `agent_message` → `text` (`item.text`); `reasoning` → `thought`;
    `command_execution` → `tool` (`"$ " + command`) plus `tool_output` (last 400 chars
    of `aggregated_output`); `file_change` → `tool` (`"edited " + comma-joined paths`).
  - Empty texts are dropped. Every text is capped at 2000 chars.

### 3. `agent.py`

- Keep `self._tails[session_id][node_id] -> LiveTail`. On every poll of a running or
  attached session (the `_await_terminal` loop and the Z2 follower), for each node that
  has started (from lifecycle events) open/poll `<run_dir>/agent-<node>.live.jsonl`
  under the run home's `runs/<run_id>/` and emit:
  - `text` / `tool` / `tool_output` → `ToolCallProgress(tool_call_id=<node_id>,
    content=[ContentToolCallContent(type="content", content=TextContentBlock(type="text", text=...))])`
    (append-only: send just the new chunk);
  - `thought` → `AgentThoughtChunk(session_update="agent_thought_chunk", content=TextContentBlock(...))`
    prefixed with `"[<node_id>] "`.
- Drain each tail once more right before emitting the terminal message, so nothing
  written at the very end is lost.
- `session/load` of a finished run replays at most the last 50 normalized events per
  node (read the file from the start, keep the tail) so history shows what each agent did.
- Bound per-poll output: at most 100 events per node per poll; the rest arrive on the
  next poll.

## Tests

`tests/unit/test_acp_live.py`:
- `LiveTail` partial-line handling, missing file, truncation reset, non-JSON skip.
- `normalize` on fixed claude stream-json lines (text, thinking, tool_use, tool_result)
  and codex lines (agent_message, reasoning, command_execution, file_change); unknown
  types → []; caps applied.

`tests/unit/test_acp_agent_py.py`:
- a running session whose run dir gains lines in `agent-implementer.live.jsonl` between
  polls emits `ToolCallProgress` with exactly the new text, and a thought chunk for a
  thinking block; nothing is re-sent on the next poll.
- loading a finished run replays at most 50 events per node.

Providers test: a host-workspace node dispatch with `MINI_ORK_RUN_DIR` + `MO_NODE_ID`
sets `MO_LIVE_FILE` to `<run_dir>/agent-<node>.live.jsonl`; an explicit `MO_LIVE_FILE`
is not overridden; the isolated branch is unchanged.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_acp_live.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_history.py` plus every test file that mentions `MO_LIVE_FILE` or `live_stream` passes.
- `ruff check` clean on touched files.
- Diff touches only the files in scope.
