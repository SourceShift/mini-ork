# Non-agent node stream — revision 2 (Opus review of run node-commands-20261007114301)

WIP commit ec514a0f holds the first cut (3 files). Opus verified: 83 tests pass, ruff clean, the
node-cmd record holds only the whitelisted env keys. Fix only the list below.

**Process rule: never rebase, merge or reset this worktree during the run.** r1 rebased mid-run, so
the run's diff swept in 30 upstream files and both verifiers failed.

## Files in scope

- `mini_ork/ide_pages/node.py`, `mini_ork/cli/execute.py` (only `_run_verifier_ref` and what it calls)
- `tests/unit/test_node_commands.py`

## Fixes

1. **Legacy runs keep sub-commands and logs** (`node.py:~1268`): the reconstructed-command branch
   must NOT return early — after the `$` entry and the "reconstructed" note, still add 2b
   (sub-commands) and 2c (logs). Every researcher run predates recording, so this is the main case.
2. **One entry per gate command** (`node.py:~1417`): `gate_cmd` + `gate_cmd_output_tail` +
   `gate_cmd_exit` form ONE `tool` entry: `arg` = the command (`gate_cmd`, or "the step's gate
   command" when the key is missing), `lines` = the output tail, then a muted `exit <n>` line
   (ints included). Same for `surfaces[]` / `target` entries: arg = target, lines = status · reason.
3. **Built-in lines** (`node.py:~1279`): also keep `[ok] <id>…`, `[fail] <id>…`, `[skip] node_id=<id>…`
   and `[info] <id>…` lines.
4. **Offsets are append-only** (`node.py:~2120`): the command stream must honour `offset` like the
   transcript path: entries `[offset:]`, and the returned `offset` = total entries. Polling at the
   returned offset returns no duplicates (legacy and built-in too).
5. **Status pill** (`node.py:~2194`): `failed · command` (red) when rc != 0 OR the verifier evidence
   says `pass: false` / the node state is failed; `finished · command` otherwise. Legacy and
   built-in entries use the node state (never `no transcript` when entries exist).
6. **No duplicate log**: skip a 2c log whose path equals the record's `output_path`.
7. **Reconstructed path**: `python3 <recipe dir>/<verifier_ref>` with the real recipe directory
   (`mini_ork.recipes_catalog.find_recipe(recipe, home).path`, falling back to the engine
   `recipes/<recipe>`), not `<recipe>/<basename>`. Remove the dead expression at `node.py:~1111`.
8. **execute.py scope**: inline the helper and constant into `_run_verifier_ref` (keep `import
   shlex` at the top only if needed — prefer `shlex` imported inside the function).
9. **Tests that pin each fix**: legacy researcher-shaped run (verifier_cycle-gate.json with
   `gate_cmd_*` ints, `_smoke_cmd_W5-91.log` with two `$` blocks, `verifier-cycle-gate.log`) →
   `$` + note + one gate entry with an `exit 1` line + two smoke entries + the log; rollback
   execute.log with `[ok] rollback complete` / `[fail] rollback` kept; offset poll returns nothing
   new; pill red for exit 0 + `pass: false`; no duplicate log entry.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_node_commands.py tests/unit/test_ide_pages_node.py tests/unit/test_ide_pages_node_changes.py` → 0 failed. Paste it. Also `python3 -m pytest` on test_node_commands.py.
- `uvx ruff check` on the 3 files → clean.
- Read-only proof (this is a library read, not running the BE/FE; required): from the worktree,
  `MINI_ORK_ROOT=$PWD PYTHONPATH=$PWD python3.11 -c "from pathlib import Path; from mini_ork.ide_pages.node import build_node; …"`
  for researcher `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork` run
  `run-le-1791359434-64879-1` nodes `cycle_gate`, `live_smoke`, `scope_guard`, `rollback` → paste
  each entry's `head`, `arg` (first 80 chars) and line count, the status pill and wall time.
- `git diff --stat origin/main` lists only the 3 files in scope (plus kickoffs).
