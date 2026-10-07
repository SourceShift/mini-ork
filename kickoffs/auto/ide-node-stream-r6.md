# IDE node detail — revision 6 (Opus review of run ide-node-stream-r5-20261007042918)

## Files in scope

- `mini_ork/ide_pages/node.py`, `tests/unit/test_ide_pages_node.py`

## Fixes (exact)

1. **Blocker — log proxy must not touch a transcript's steer window.**
   `node.py:644-645` appends `node_start_s + offset - 1` to `lower_bound_ts` whenever
   `log_path is not None`; `build_node` resolves `log_path` independently of
   `session_path`, and an implementer has both a transcript and `impl-<node>.log`.
   Use the log-line proxy ONLY when the stream is log-backed (no session transcript).
   Test (fails on HEAD): dense transcript of 100 lines at 10/s (T0+10..T0+19) plus an
   `impl-<node>.log` in the run dir, steer at T0+25; append a transcript line at T0+30;
   poll at offset 100 → the steer is returned exactly once.
2. **Lower telemetry pad** (`node.py:231`): `int(node.start) - 5` (kickoff asked for 5 s
   both sides). Fix the wrong comment at `node.py:233-237` (SQLite orders INTEGER below
   TEXT, so integer bounds would never match).
3. Remove the dead `raw_ts = cs_env.get("ts")` branch (`node.py:750`) or read the
   outer live.jsonl record's `t`; and delete the stale r3 comment at
   `test_ide_pages_node.py:158-159`.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node.py` passes — paste the summary line.
- Diff touches only files in scope.
