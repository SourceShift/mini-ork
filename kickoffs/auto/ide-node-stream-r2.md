# IDE node detail — revision 2 (Opus review of run ide-node-stream-20261006224824)

## Goal

HEAD of `wt/ide-node-stream` is the first pass of `kickoffs/auto/ide-node-stream.md`.
Opus returned needs_revision with the fixes below; make exactly these, keep the rest.

## Files in scope

- `mini_ork/ide_pages/node.py`
- `tests/unit/test_ide_pages_node.py`

## Fixes (exact)

1. **String prompt content.** Real Claude Code transcripts store the first user
   message's `content` as a plain `str` (all 4 sessions of run
   `run-le-1791314676-64879-1`). `node.py:330` (`if not isinstance(content, list):
   continue`) and `:586` (`_prompt_view`) must accept `str` content: it is the
   `user` entry (prompt) and the rendered prompt for the prompt view. A
   `tool_result` envelope must never be taken as the prompt.
2. **Resolver rule 3 only for running nodes**, and only when the session's first
   prompt contains the node's id/name or its prompt template text. A finished
   node with no mapped session falls through to its log (shell/verifier nodes
   must show `verifier_<node>.log` / `evidence/<node>*.log` / `impl-<node>.log`,
   never another node's transcript). Also implement rule 2
   (`llm_calls.session_id` for the run + the node's lane/actor) before rule 3.
3. **Append-only offset.** `offset` = number of transcript *lines* consumed (not
   an index into the merged entry list). Steering rows are merged into the entry
   list by timestamp, filtered to the node's role (or `any`) and this run
   (exclude `run_id IS NULL`). A poll with `--offset N` returns only entries
   derived from transcript lines after N plus steer rows newer than the line at N;
   nothing is lost or repeated across polls.
4. **Reveal path** (`node.py:217/220`): use the run's own `run_dir` from the
   loaded run object (respects `--home`), not a path rebuilt from env/cwd.
5. **Note from `cost-state`**: transcripts have no `result` entries; build the
   final `note` from the last `cost-state` (`totalCostUSD`, durations) and/or the
   live.jsonl `result` event ("Done · $X · N turns").
6. **Output view report names**: map node `invariants_lens` → `lens-invariants.md`
   (strip a `_lens` suffix; also try `lens-<id>.md`, `<id>.md`, `review-<id>.json`,
   `verifier_<id>.json`).
7. **Spec details**: status `finished · N events`; `attached · live` only when the
   transcript/log grew in the last 120 s; `meta` = `<lane> · <tokens> tokens · $<cost>`
   (tokens summed from the node's llm_calls input+output, else from cost-state);
   telemetry rows from `llm_calls` columns (`input_tokens`, `output_tokens`,
   `cached_input_tokens`, `cost_usd`, `duration_ms`) matched on the node (actor AND
   time window inside the node's start/end), list includes the session id;
   learning view = injected gradients from context-pack.json / the prompt text,
   steering rows for the node's role, gradients produced in the run's window
   (same rule as `run.py`). Fix the wrong docstring at `node.py:153`; trailing
   newlines.

## Tests (add to `tests/unit/test_ide_pages_node.py`)

- a session whose first line is `{type:user, message:{content:"PROMPT AS STRING"}}`
  → first entry kind `user` with that text; the following Read tool entry present;
  prompt view block shows the prompt;
- the shell node's stream source is its verifier log, not a transcript;
- offset after append: poll → offset N; append two assistant texts; poll with
  `--offset N` → exactly those two (and no repeated steer row);
- `cost-state` → final `note` present.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node.py tests/unit/test_ide_pages_run.py tests/unit/test_board_cmd.py` passes.
- On the real run `run-le-1791314676-64879-1` (home `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`)
  `board node … invariants_lens` stream has kinds user ≥ 1, think, tool, text, note
  (report counts) and the output view shows `lens-invariants.md`; < 1.5 s.
- ruff clean; diff touches only files in scope.
