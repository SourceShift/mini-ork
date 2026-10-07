# IDE node stream for non-agent nodes: the full command and its output

## Why

Clicking a node that is not a Claude Code agent (verifiers like `cycle_gate` / `live_smoke` /
`scope_guard` / `static_check_verifier`, `rollback`, `publisher`) shows "no transcript · No stream
yet." The user wants to see **the full command that ran and its output**, like a Bash call in Claude
Code — and when the output was not stored, mini-ork must start storing it.

What exists today:
- `_run_verifier_ref(script, evidence_path, …)` (`mini_ork/cli/execute.py:1389`) runs
  `_verifier_argv(script)` (`[sys.executable, script]`) in the run's target cwd and writes the merged
  stdout+stderr to `evidence_path` (= `<run_dir>/verifier_<stem>.json`, which is why that file can
  start with a `DeprecationWarning` line). The argv, cwd, rc and timing are NOT recorded.
- Verifiers also write their own logs: `verifier-<stem>.log`, `evidence/<stem>*.log`, and some
  record sub-commands they ran: e.g. researcher `verifier_cycle-gate.json` has `gate_cmd_exit` /
  `gate_cmd_output_tail`; `verifier_live-smoke.json` has `surface: "cmd"`, `target` and
  `surfaces[].target`; the run dir has `_smoke_cmd_<step>.log` with `$ <command>` / output /
  `[rc=N]` blocks.
- Built-in steps (rollback, publisher, skips) only print lines like `[rollback] discard_worktree: …`,
  `[ok] rollback complete`, `[skip] node_id=publisher blocked …` into `<run_dir>/execute.log`.
- Stream entries for agent tool calls look like
  `{"k": "tool", "head": <name>, "arg": <summary>, "lines": [{"t", "c"}], "_src", "_line"}`
  (`node.py:~870`); the IDE renders them like Claude Code tool calls (collapsed after 4 lines,
  "… +N lines" / "show less").

## Files in scope

- `mini_ork/cli/execute.py` — ONLY `_run_verifier_ref` (do not touch anything else there)
- `mini_ork/ide_pages/node.py`
- `tests/unit/test_node_commands.py` (new)

Do NOT edit `mini_ork/cli/execute_handlers.py` (another worktree owns it).

## 1. Record every verifier command (`_run_verifier_ref`)

Best-effort (never raises, never changes rc): after `run_check`, write
`<run_dir>/node-cmd/<evidence file stem>.json` (e.g. `node-cmd/verifier_static-check.json`; run dir =
`run_dir` arg or `dirname(evidence_path)`):
`{"script", "argv": [...], "cmd": shlex.join(argv), "cwd", "env": {k: v for the whitelist
MINI_ORK_PLAN_PATH, ARTIFACT_PATH, MINI_ORK_RUN_DIR, MINI_ORK_RUN_ID, MO_TARGET_CWD, PYTHONPATH
when set}, "started_at", "ended_at" (epoch float), "rc", "output_path": evidence_path}`.
Never record any other env var (secrets).

## 2. Stream for non-agent nodes (`node.py`)

When a node has no session transcript and either its type is not an LLM type (verifier, rollback,
publisher, gate, handler…) or it has no `llm_calls`, build the stream from commands instead:

a. **The command** — one `tool` entry: `head` = `"$"`, `arg` = `cd <cwd> && <cmd>`, `lines` = the
   full output file (all lines, capped at 5000 with a final muted "… N more lines — <path>"),
   red lines when rc != 0, then a muted `exit <rc> · <duration>` line. Source: the `node-cmd` record
   (map the node to its verifier stem: basename of the node's `verifier_ref` without `.py`, falling
   back to the node id with `_`→`-`). Runs without a record: reconstruct `cmd` =
   `python3 <recipe dir>/<verifier_ref>` and cwd = the run profile's target root, output =
   `verifier_<stem>.json` raw text, and add a note entry "Command reconstructed from the recipe —
   this run predates command recording."
b. **Commands the verifier ran** — one `tool` entry per sub-command found: `gate_cmd` (+ `gate_cmd_exit`
   + `gate_cmd_output_tail`) keys; `target` when `surface == "cmd"` and each `surfaces[].target` with
   `surface == "cmd"` (+ their `status`/`reason`); and every `<run_dir>/_*cmd*.log` file split into
   `$ <command>` blocks with their output and `[rc=N]`.
c. **The verifier's own logs** — one entry each for `verifier-<stem>.log` and the newest
   `evidence/<stem>*.log` (`head` = `"log"`, `arg` = the path, full text as lines, same cap).
d. **Built-in steps** (rollback, publisher, any node without a command): one entry
   `head` = `"built-in"`, `arg` = `<node type> · <node id>`, lines = the `execute.log` lines that
   belong to it (`[<node type>]` / `[<node id>]` prefixes, `node_id=<id>`, `[ok] <id>`,
   `[fail] <id>`), plus `rolled-back.json` / `salvage.json` summaries for rollback.
e. Nothing stored at all → one note entry "No command or output was stored for this node."
f. The status pill reads `finished · command` (or `failed · command` when rc != 0) instead of
   `no transcript`; kind filters count these entries as Tools.

Keep the agent-node stream path exactly as it is.

## Tests (`tests/unit/test_node_commands.py`; tmp homes/run dirs, no LLM)

- `_run_verifier_ref` with a tiny verifier script writing `{"pass": true}` → the record exists with
  argv/cmd/cwd/rc/timing/output_path, the env holds only whitelisted keys (set a fake
  `SOME_API_KEY` in the env and assert it is absent).
- Recorded verifier node → stream entry 1 is `$` with `cd <cwd> && <cmd>`, all output lines, exit line.
- Legacy run (no record) → reconstructed command + the note.
- Researcher-shaped fixtures: a cycle-gate JSON with `gate_cmd_*` keys, a live-smoke JSON with
  `surfaces` + a `_smoke_cmd_W5-91.log` with two `$` blocks → one sub-command entry per command.
- Rollback node with execute.log lines + rolled-back.json → a built-in entry with those lines only.
- No artefacts → the single note; agent nodes unchanged (an existing transcript fixture still yields
  its tool/text entries).

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_node_commands.py tests/unit/test_ide_pages_node.py tests/unit/test_ide_pages_node_changes.py` → 0 failed. Paste the line.
- `uvx ruff check` on the touched files → clean.
- Read-only proof (in-process with this worktree's code: `PYTHONPATH=$PWD python3.11 -c "…build_node(…, view='stream')…"`),
  paste entry heads/args and line counts for: researcher
  `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork` run `run-le-1791359434-64879-1`
  nodes `cycle_gate`, `live_smoke`, `scope_guard`, `rollback`; and this repo's
  `/Volumes/docker-ssd/ps/mini-ork/.mini-ork` run `ide-node-changes-r3-20261007104328` nodes
  `static_check_verifier`, `test_verifier`, `rollback`. Each < 1.5 s. Never write into those run dirs.
- Diff touches only files in scope.
