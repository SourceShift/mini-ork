# IDE node detail — revision 4 (Opus review of run ide-node-stream-r3-20261007005337)

## Files in scope

- `mini_ork/ide_pages/node.py`, `tests/unit/test_ide_pages_node.py`

## Fixes (exact) — each with a test that fails before the fix

1. **Steer lower bound from real time.** `node.py:580` `prev_line_ts = node_start_s + prev_line_idx`
   is the forbidden proxy. Use the max `_ts` (the line's own ISO `timestamp`) over
   transcript entries with `_line < offset`; a steer is returned on a poll iff its
   `created_at` is after that bound and not after the last consumed line's `_ts`.
   Test: 5 lines 60 s apart (not 1 s), steer at start+100 s → poll 1 returns it;
   append a line; poll 2 at the previous offset does NOT return it again. Fix the
   fixture spacing (`test_ide_pages_node.py:98-118`) to 60 s so the proxy can't pass.
2. **Shell log offset on absolute lines.** `node.py:836` slices the last 40 lines and
   indexes within the slice. Index by absolute file line number; apply the 40-line
   cap only to the full read (offset 0). Test: 45-line log → offset 45; append
   "NEW LINE" → poll at 45 returns exactly that line.
3. **Lens / review roles.** `_NODE_ROLE_MAP` (`node.py:69`): node types
   `researcher` whose id ends in `_lens`, and types `lens`, `eval`, `judge`,
   `synthesizer`, `reviewer` → `reviewer` (reuse `run.py`'s `_REVIEW_TYPES`). Test:
   `code_impact_lens` (type researcher) → reviewer; a reviewer-targeted steer shows
   on it.
4. **Final note for live followers.** `node.py:1407` emits the cost-state note only
   when `offset == 0`. Emit it exactly once on the first poll where the node is no
   longer live and the cost-state/result is available (whatever the offset), without
   changing `offset`; never on later polls. Test: start polling while live (no
   cost-state), add cost-state + mark finished → next incremental poll returns the
   note once; the one after returns nothing.
5. Minor: drop `LIMIT 50` before the window filter (`node.py:212`) or prefilter by an
   ISO `ts` range; `offset` = total non-blank transcript lines consumed (trailing
   non-emitting lines count too).

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node.py tests/unit/test_ide_pages_run.py tests/unit/test_board_cmd.py` passes — paste the summary line.
- Diff touches only files in scope.
