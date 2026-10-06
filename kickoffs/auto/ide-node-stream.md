# IDE node detail — every DAG node opens its agent's stream; live nodes can be steered

## Goal

In the mini-ork IDE a run's DAG opens full screen; clicking any node opens a side
panel with tabs **Stream · Output · Prompt · Telemetry · Learning**. Stream must
show the node's agent session the way Claude Code's terminal UI shows it:
thinking, tool calls with their inputs, tool results, text — replayable for a
finished node, and live (follow by offset) for a running node, which the user can
steer. Add the data verbs the IDE calls. The IDE side is not in this repo.

## Files in scope

- `mini_ork/cli/board_cmd.py` — two new verbs (`node`, `steer`) on the existing `build_parser()`
- `mini_ork/ide_pages/node.py` — new: node detail builder
- `mini_ork/ide_pages/run.py` — DAG node fields (below)
- `tests/unit/test_ide_pages_node.py` — new

No other file changes.

## Where the data is (verified on a real run)

- `<run_dir>/sessions/<uuid>.jsonl` — the node's Claude Code session transcript,
  appended live while the agent works. Entries with `type` `assistant` (content
  blocks `thinking`, `text`, `tool_use{name,input}`), `user` (blocks
  `tool_result{content,is_error}`; the first `user` message is the prompt),
  `system`, `cost-state{totalCostUSD,...}`, plus noise (`attachment`,
  `atis-latch`, `last-prompt`, `queue-operation`) to skip.
- `<run_dir>/agent-<node>.live.jsonl` — `{"seq","stream","t","line"}` rows; stdout
  lines may hold a stream-json `result` event with `session_id`, `total_cost_usd`,
  `num_turns`.
- Node ↔ session mapping, in this order: the `session_id` in the node's
  `agent-<node>.live.jsonl` result event; `llm_calls.session_id` for the run and
  the node's lane/actor; for a still-running node, the newest
  `sessions/*.jsonl` created after the node's `node_start` event whose first user
  prompt contains the node's prompt template text or node name. Unknown → no
  stream (say so).
- Shell / verifier nodes have no session: stream = the node's log
  (`verifier_<node>.log`, `evidence/<node>*.log`, `impl-<node>.log`) as output lines.
- Steering: `operator_steering` rows for the run (`mini_ork.web.control.steer_run`
  writes them; roles in `_STEER_ROLES`).

## Verbs (exact)

### `board node <run_id> <node_id> [--view stream|output|prompt|telemetry|learning] [--offset N]`

Default view `stream`. One JSON object, exit 0 (`{"ok": false, "error"}` exit 1 for
an unknown run/node). Always present:

```
{"ok": true, "run": id, "node": id, "label": str, "state": "pending|running|done|failed|skipped",
 "role": str, "lane": str, "family": str, "stage": str, "stage_index": int, "stage_count": int,
 "live": bool,            # running AND its transcript/log grew in the last 120 s
 "kv": [{"k","v","c"}],   # Lane, Provider, Cost, Duration, Tokens, Gates — real values or "—"
 "acts": [button],        # spec.btn: running → Stop run (cli board stop, confirm) + Kill;
                          # failed/done → "Open run folder" (reveal); never invented verbs
 "view": str, ...view fields}
```

Colours use `spec.COLOURS` names / `fam:<lane>`; buttons use `spec.btn` and actions.

- **stream**: `{"entries": [entry], "offset": int, "status": str, "status_c": colour,
  "meta": str, "source": str, "done_note": str}`.
  `entry` = `{"k": kind, "head": str, "arg": str, "lines": [{"t","c","bg"}]}` with kinds:
  - `user` — the prompt (first user message): `arg` = first 400 chars, one box.
  - `think` — thinking block: `head` "Thinking… ", `arg` = first 600 chars.
  - `text` — assistant text: `arg` = full text (cap 4,000 chars).
  - `tool` — tool_use + its matching tool_result: `head` = tool name, `arg` = a
    one-line summary of the input (`file_path`, `command`, `pattern`, `url`,
    first 160 chars), `lines` = result, at most 12 lines + `"… N more lines"`;
    `c` red when `is_error`; for Edit/MultiEdit show `- old` / `+ new` lines with
    `bg` `"red-bg"` / `"green-bg"`.
  - `todo` — TodoWrite: `lines` = `☒`/`☐` + content per item.
  - `steer` — operator steering for this run/role, placed by timestamp: `head`
    "You · steering ", `arg` = `[severity] message`.
  - `note` — e.g. `"Done · $0.31 · 12 turns"` from the result / cost-state.
  `offset` = number of transcript lines consumed; with `--offset N` return only
  entries from line N on (the IDE polls this while live). `status`:
  `"attached · live"` (live), `"finished · N events"`, `"not dispatched yet"`,
  `"no transcript"` — with `status_c` green / sub / sub / yellow.
  `meta` = `"<lane> · <tokens> tokens · $<cost>"`. `done_note`: finished →
  "Agent finished — steering only reaches running nodes."; pending → "This node
  has not started. Its stream opens when it is dispatched."
- **output**: `{"block_title", "block": [{"t","c"}], "list_title": "Artifacts",
  "list": [spec item with an open_path act]}` — the node's final text / report file
  (e.g. `lens-<x>.md`, `review-*.json`, verifier JSON) and the files it wrote in the run dir.
- **prompt**: `{"block_title": "Rendered prompt · as dispatched", "block": [...],
  "list_title": "Prompt ref", "list": [...]}` — the first user message of the
  session (the rendered prompt), else the recipe prompt template file.
- **telemetry**: `{"block_title": "LLM calls", "block": header + one row per
  llm_calls row for the node (turn, in, out, cache, cost, ms)}`, list = trace id /
  session id. Shell nodes: "Deterministic node — no LLM calls."
- **learning**: `{"list_title": "Learning", "list": [...]}` — gradients injected
  into the prompt (from context-pack.json / the prompt text), steering messages
  for the node's role, gradients produced by the run (same window rule as run.py).

### `board steer <run_id> --role ROLE --severity info|warn|critical --text TEXT`

Calls `mini_ork.web.control.steer_run(db_for(home), run_id, text, role_target=ROLE,
severity=SEVERITY, source="ide")`; prints its dict as JSON; exit 0/1. `--role`
defaults to `any`.

### `run.py` DAG nodes

Each `spec.dag_node` also carries `role`, `dur` (e.g. `"222s"` or `"—"`), `cost`,
`gates`; the `dag` section carries `heads` (one stage label per column: the role
of its first node, or `"stage N"`), `run_title`, `recipe`. Existing keys unchanged.

## Tests (`tests/unit/test_ide_pages_node.py`)

A temp home with one run dir holding a small session transcript (thinking,
Read tool_use + result, Edit with old/new, Bash with an error result, TodoWrite,
final text), an `agent-<node>.live.jsonl` with a result event naming that session,
a shell node with a verifier log, and an `operator_steering` row. Assert: every
view builds `ok: true` for both nodes; stream kinds and order; Edit diff lines and
error colouring; `--offset` returns only later entries; `board steer` writes a row
and rejects a bad severity; unknown node → `ok: false`; `run.py` dag nodes carry
`role/dur` and the section carries `heads`.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node.py tests/unit/test_ide_pages_run.py tests/unit/test_board_cmd.py` passes.
- On the real run `run-le-1791314676-64879-1` in `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`,
  `bin/mini-ork board node <run> invariants_lens --home …` returns a stream with
  thinking, tool and text entries (report entry counts by kind), in < 1.5 s.
- ruff clean on touched files; diff touches only files in scope.
