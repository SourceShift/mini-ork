# IDE node detail — revision 5 (Opus review of run ide-node-stream-r4-20261007020955)

## Files in scope

- `mini_ork/ide_pages/node.py`, `tests/unit/test_ide_pages_node.py`

## Fixes (exact) — each test must fail against the current HEAD

1. **Shell-node steer re-emit (regression).** `node.py:573/609`: for log-backed nodes
   the lower bound is empty on incremental polls, so every steer is re-sent. Compute
   the steer window for log-backed nodes from line positions: a steer is returned
   on the poll whose consumed line range covers its time, with log lines' time =
   the log file's per-line timestamp when present, else `node_start + line_index`
   seconds for BOTH bounds (lower = line `offset-1`, upper = last consumed line).
   Test: 10-line verifier log, steer at T0+62 → poll 1 returns it once; append
   "line 10"; poll 2 at offset 10 returns no steer.
2. **The final note is stateless (regression).** Remove the `.cost-note-emitted.*`
   marker file — the read path must never write. Emit the note: on every full read
   (offset 0) of a finished node; and on an incremental poll iff the node is
   finished and this poll is the first one past the live→finished edge, i.e. the
   newest transcript `_ts` consumed before this poll is older than the node's
   `end` (or the cost-state timestamp) while this poll's upper bound reaches it.
   Two full reads by two viewers both get the note; a third incremental poll after
   the edge gets none. The note never advances `offset`.
3. **Docstring matches code** (`node.py:28-33`): the note carries no `_line` and does
   not advance `offset`.
4. **Tests as specified:**
   - fix #1 of r4 (transcript steer): `test_steer_emitted_once_and_skipped_on_subsequent_polls`
     must append a line between poll 1 and poll 2; change the shared fixture
     (`test_ide_pages_node.py:98-118`) to 60 s spacing and remove `_seed_60s`.
   - fix #4 of r4 (note for live followers): start polling while live with no
     cost-state; then add cost-state and mark the node finished; the next
     incremental poll returns the note once; the following one returns nothing;
     a fresh full read returns it again. No marker-file assertions.
   - fix #3 of r4: a steer with `role_target='reviewer'` shows in the
     `code_impact_lens` (type researcher) stream.
5. **Telemetry query** (`node.py:235`): drop `LIMIT 1000`; prefilter with
   `ts BETWEEN ? AND ?` using ISO bounds from the node's start/end (pad 5 s).

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node.py` passes — paste the summary line
  (the broader run can stall under load; run this file alone).
- Diff touches only files in scope.
