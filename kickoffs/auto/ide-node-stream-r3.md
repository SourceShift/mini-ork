# IDE node detail — revision 3 (Opus review of run ide-node-stream-r2-20261006231312)

## Files in scope

- `mini_ork/ide_pages/node.py`, `tests/unit/test_ide_pages_node.py`

## Fixes (exact) — each must have a test that fails before the fix

1. **Telemetry / resolver rule 2.** `llm_calls` has no `ts_ms` column; its time is
   `ts` (ISO text). `node.py:1164 _call_for_node` and the SELECTs at `:187` and
   `:1136` must parse `ts` to epoch seconds (use `run._epoch` as
   `run._attribute_calls` does) and window-match the node's start/end. Test: a
   call for the node's actor inside its window shows in telemetry and in `meta`
   tokens.
2. **Append-only offset, no repeated note.** `offset` counts transcript lines
   only. The final `note` (from the last `cost-state` / live.jsonl `result`) is
   emitted once: on the poll that consumes the line it comes from (or on the full
   read when offset is absent) — never re-synthesised on later polls, and never
   adds to `offset`. Test: 10-line transcript, no `result` entry → offsets 10, 10,
   10 on three polls; polls 2 and 3 return nothing; append 2 lines → poll returns
   exactly those 2.
3. **Steering by real time.** Place steer rows using the transcript lines' own
   `timestamp` fields (ISO) — not `node_start + idx*1000`. A poll with
   `--offset N` returns steer rows created after the timestamp of line N-1 and not
   later than the last line consumed; a steer created before the node started is
   shown at the top on the full read. No row is returned twice across polls.
   Restore the realistic fixture timing (steer at node.start+30s).
4. **Shell-node log streaming.** For log-backed nodes, `offset` counts log lines;
   each new log line after N is returned as an `out` entry line. Test: append a line
   to the verifier log → poll at the previous offset returns it.
5. **Steer role filter by node role, not id.** Map the node's type/role to the
   `operator_steering` roles (`planner|implementer|reviewer|verifier|any`):
   verifier/static_check/test → verifier; *_lens, reviewer, synthesizer → reviewer;
   implementer/worker → implementer; planner/decomposer → planner. Fix the wrong
   docstring.
6. **Gradient window in seconds** (`node.py:1223/1228`): `gradient_records.created_at`
   is epoch seconds; use the same rule as `run.py` (seconds, task_class match).
7. Minor: tokens = input + output (not cached); `finished · N events` counts all
   entries of the transcript, not just this poll's; trailing newlines on both files.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node.py tests/unit/test_ide_pages_run.py tests/unit/test_board_cmd.py`
  passes — paste the summary line.
- Diff touches only files in scope.
