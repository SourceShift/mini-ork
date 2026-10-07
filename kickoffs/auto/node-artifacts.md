# IDE node inspector: the artifacts a node was given and produced, and the code it changed

## Why

The user, about clicking a node in the run graph: "the user can have option to see live stream of
the current working agent, or if not agent the command executed and the output of it up to now.
and also the code changed by that agent, the given artifacts". The live stream and command output
exist (`board node … --view stream`). Missing:

1. **The code changed by that agent.** `--view changes` shows the run's cumulative files after the
   node, not what THIS agent edited. An agent's own edits are in its transcript as `Edit` /
   `MultiEdit` / `Write` tool calls (`mini_ork/ide_pages/node.py` already parses them for the stream:
   `_is_edit_tool`, `_edit_diff_lines` — capped at 6 lines there).
2. **The artifacts.** What the node was given (inputs) and what it produced (outputs), each openable
   with a preview.

A parallel run (`node-overview`) is editing `_resolve_session_path` and adding `--view overview` in
node.py; keep your node.py edits to the few lines listed below so the two merge cleanly.

## Files in scope

- `mini_ork/ide_pages/node_artifacts.py` (new — all new logic lives here)
- `mini_ork/ide_pages/node_changes.py` (add `agent_edits` to the changes payload)
- `mini_ork/ide_pages/node.py` — ONLY: add `"artifacts"` to `_VIEWS` and a 3-line dispatch to
  `node_artifacts.build_artifacts_view(run, node)` next to the existing `changes` dispatch
- `mini_ork/cli/board_cmd.py` — ONLY: add `"artifacts"` to the `--view` choices
- `tests/unit/test_node_artifacts.py` (new)

Never rebase, merge or reset this worktree during the run. Do not edit `mini_ork/web/repositories.py`.

## 1. `--view artifacts` → `{"inputs": [...], "outputs": [...]}`

Each artifact: `{"name", "path" (absolute), "size" (bytes), "kind" ("markdown"|"json"|"diff"|"log"|
"text"), "preview" (first 60 lines, text only; JSON pretty-printed), "from" (the producing node id,
or "run" for run-level inputs)}`. Only files that exist.

- **Outputs:** the node's declared `outputs` ports when the workflow has them; otherwise by node kind:
  planner → `plan.json`; researcher/lens → its report (`node._report_paths(run_dir, node.id)`);
  implementer → `implementer-summary.json`, `impl-<id>.log`, `framework-edit.diff`;
  reviewer/judge → `review-<id>.json`, `review-<id>.json.stdout.md`; verifier → `verifier_<stem>.json`,
  `verifier-<stem>.log`, `verifier-<stem>.checks.tsv`, the newest `evidence/<stem>*.log`,
  `node-cmd/verifier_<stem>.json`; rollback → `rolled-back.json`, `salvage.json`, `salvage.patch`;
  publisher → the run's verdict/publish files. Plus any file under the run dir the agent wrote
  with a `Write` tool call (from its transcript).
- **Inputs:** declared `inputs` ports when present; otherwise the run-level inputs (the kickoff file,
  `context-pack.json`, `plan.json` for non-planners) plus the OUTPUTS of the node's parents in the
  workflow DAG (edges into the node: `depends_on`, `supplies_context_to`, `verifies`), plus any run-dir
  file the node's prompt names (scan the rendered prompt — the transcript's first user message, or
  the prompt view's text — for run-dir paths or bare artifact names that exist in the run dir).
  Reviewer inputs must include `review-diff.patch` when the prompt names it.

## 2. `agent_edits` in `--view changes`

`node_changes.build_changes_view` adds `"agent_edits": [{"path", "tool", "added", "removed",
"diff": "<unified-style text, - old / + new, full length, cap 4,000 lines total>"}]` from the node's
transcript `Edit` / `MultiEdit` / `Write` tool calls (in order; `Write` = whole new content as `+`
lines), and `"agent_edits_note"`: `"This node edited no files."` when it has a transcript but no edit
calls, `""` when there is no transcript. Use `node._resolve_session_path` (lazy import) for the
transcript.

## Tests (`tests/unit/test_node_artifacts.py`; tmp homes, no LLM)

- Verifier outputs (json, log, tsv, newest evidence log, node-cmd record) with kinds and previews.
- Reviewer inputs include its parents' outputs and `review-diff.patch` named in its prompt; outputs
  include `review-reviewer.json`.
- Declared ports win: a workflow node with `inputs`/`outputs` lists exactly those.
- `agent_edits`: a transcript with one Edit, one MultiEdit and one Write → three entries with correct
  +/- counts; a transcript with no edits → the note.
- `board node … --view artifacts` is accepted by the CLI.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_node_artifacts.py tests/unit/test_ide_pages_node_changes.py tests/unit/test_ide_pages_node.py` → 0 failed. Paste it.
- `uvx ruff check` on the touched files → clean.
- Read-only proof (a library read — required): from this worktree
  `MINI_ORK_ROOT=$PWD PYTHONPATH=$PWD python3.11 -c "…build_node(Path('/Volumes/docker-ssd/ps/mini-ork/.mini-ork'), 'lane-repair-hint-r2-20261007160033', <node>, view='artifacts')…"`
  for `implementer` and `reviewer` → paste input/output names; and `view='changes'` for
  `implementer` → `agent_edits` count + the first 3 paths. Each < 1.5 s.
- `git diff --stat origin/main` lists only files in scope (plus kickoffs).
