# IDE node inspector: an Overview of what the node did, and transcripts that are never lost

## Why

Clicking a node in the run graph shows the Stream tab first. For the reviewer of run
`lane-repair-hint-r2-20261007160033` (this repo's home) it says "no transcript" and only
"Done · $1.41 · 21 turns · 0s", although the reviewer worked 21 turns. The user: "when user selects a node we
show proper data". Two problems:

1. **Lost transcript.** `_resolve_session_path` (`mini_ork/ide_pages/node.py:122`) finds the
   session id in `agent-reviewer.live.jsonl` (`aeec7f1d…`, then `af0fc3b9…` after the revise
   loop's second round), but `runs/<id>/sessions/<sid>.jsonl` does not exist for either (only
   the lenses/implementer transcripts were copied). The live file itself (4,257 lines of Claude
   stream-json, each record `{"line": "<stream-json>"}`) holds the whole conversation.
2. **No summary.** There is no single place that says what the node did: its verdict and why,
   which checks failed, what files it changed, its final message, its cost/turns/duration.

## Files in scope

- `mini_ork/ide_pages/node.py`, `mini_ork/cli/board_cmd.py` (only the `--view` choices)
- `tests/unit/test_node_overview.py` (new)

Never rebase, merge or reset this worktree during the run. Do not edit `mini_ork/web/repositories.py`
(claimed by another worktree).

## 1. Transcripts are never lost (stream + overview)

Extend `_resolve_session_path` after rule 1:
- For each session id found in the live file (newest first), also try
  `~/.claude/projects/*/<sid>.jsonl` (glob, first hit).
- If no transcript file exists at all but `agent-<node>.live.jsonl` does, use the live file as the
  transcript: unwrap each record's `line` (JSON string) and feed the decoded stream-json objects
  through the same parser `_session_entries` uses (stream-json `assistant`/`user`/`result` objects
  have the transcript's `{type, message:{content:[…]}}` shape). Skip records that do not decode.
  Return a marker the callers understand (e.g. a small dataclass or the live path plus a flag) —
  keep `_session_entries(path)` working for real transcripts.
- The status pill for a live-file-backed stream reads `finished · N events` like a transcript.

## 2. New view `overview` (make it `_DEFAULT_VIEW`)

`board node <run> <node> --view overview` returns, besides the usual header fields:
- `headline`: `{"t": str, "c": colour}` — one sentence on what happened, by node kind:
  reviewer/judge → `"<verdict> — <first reason>"` (from `review-<node>.json` reasons/notes);
  verifier → `"<n> of <m> checks failed: <first failing check>"`, or `"UNVERIFIED: <reason>"`,
  or `"all <m> checks passed"`; implementer/code-changing → `"changed <n> files (+a −r)"` (from
  the changes view); lens/researcher → the report's first heading; planner → the plan objective;
  built-in → its last execute.log line; any failed node → the failure reason first (node_end
  `finish_reason` / `verdict`, the failed llm_call's `error_message`, or the last log line).
- `facts`: kv rows — Status (state + verdict), Model (family · lane), Started / Ended (local
  `HH:MM:SS`, date when not today), Duration, Cost (+ calls), Turns (from the transcript's
  `result` or the live file), Tokens, Exit code (command nodes, from `node-cmd`).
- `result`: the items `node_changes._result_items(run, node)` already builds (findings, checks,
  report bullets) — reuse it, do not duplicate.
- `files`: the changes view's `files` (path, added, removed, abs) when non-empty.
- `final`: `{"text": markdown}` — the agent's final message (the transcript/live `result` text, else
  the last assistant text block), full length (cap 200,000 chars).
- `links`: `[{"label", "path"}]` for the node's own artefacts that exist (review JSON, lens report,
  verifier log, impl log, NEEDS-CHANGE.md).
Every part is best-effort: a missing source drops that part, never the view.

## Tests (`tests/unit/test_node_overview.py`; tmp homes, no LLM)

- Live-file fallback: a reviewer with `.sessions/reviewer.session` pointing at a missing uuid and an
  `agent-reviewer.live.jsonl` built from 3 real-shaped stream-json records (assistant text, tool_use,
  result) → `view=stream` returns those entries; `view=overview` has `final.text` = the result text
  and a Turns fact.
- `~/.claude/projects/*/<sid>.jsonl` fallback with `HOME` monkeypatched to a tmp dir.
- Headline per kind: reviewer needs_revision with reasons; verifier with 2 of 5 failing checks;
  verifier UNVERIFIED; implementer with a diff; failed node with a node_end finish_reason.
- `overview` is the default view; `board node … --view overview` is accepted by the CLI.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_node_overview.py tests/unit/test_ide_pages_node.py tests/unit/test_node_commands.py tests/unit/test_ide_pages_node_changes.py` → 0 failed. Paste it.
- `uvx ruff check` on the touched files → clean.
- Read-only proof (a library read — required): from this worktree
  `MINI_ORK_ROOT=$PWD PYTHONPATH=$PWD python3.11 -c "…build_node(Path('/Volumes/docker-ssd/ps/mini-ork/.mini-ork'), 'lane-repair-hint-r2-20261007160033', 'reviewer', view=…)…"`
  → paste: stream entry count + first 3 heads; overview headline, facts, final text (first 200
  chars), result item count. Then the same overview for researcher
  `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork` run
  `run-le-1791359434-64879-1` nodes `live_smoke`, `implementer`, `opus_judge`. Each < 1.5 s.
- `git diff --stat origin/main` lists only files in scope (plus kickoffs).
