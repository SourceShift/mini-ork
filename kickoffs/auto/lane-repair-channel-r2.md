# Lane repair (C) — revision 2 (Opus review of run lane-repair-channel-*)

WIP commit c2f5f473 holds revision 1: owner=thread env, the `_offer_lane_repair` buttons, the
inactive-turn message, the offline thread-JSONL append. Opus returned `needs_revision`. Fix ONLY
the list below.

## Findings to fix

1. **The resumed run is never followed, so its card stays `in_progress` forever.**
   - For orchestrator children, `_start_child_follow` (`mini_ork/acp/agent.py:3259`) no-ops when
     called from inside the run's own follower: `self._followers[run_id]` is the current,
     not-yet-done task (:3271-3273).
   - For `/run`, the cached terminal status (`failed`) makes the next wait return at once, and
     the `_lane_repair_emitted` short-circuit swallows it.
2. **`_lane_repair_emitted` is only cleared in the resume handler**, so a second failure after a
   resume that went another way is silent.
3. **The tests stub `_start_child_follow`**, so the broken path never ran.

## Files in scope

- `mini_ork/acp/agent.py`: ONLY `_follow_in_thread`, `_offer_lane_repair` and its decision
  handler (~:3634-3795), plus a small helper they need
- `tests/unit/test_acp_lane_repair.py`

Do NOT modify any other file.

## Design (exact)

1. **`_offer_lane_repair` returns one of `"resumed" | "offered" | "declined" | "none"`**:
   - `"resumed"`: the user picked a lane, or "same", and `/recover` was spawned;
   - `"offered"`: the inactive-turn message was posted; the marker stays open;
   - `"declined"`: "Leave it"; the marker is closed `failed`;
   - `"none"`: not a lane failure; today's behaviour.
2. **`_follow_in_thread` becomes a bounded loop**, at most 3 resumes per run:
   ```
   while True:
       stop = await self._await_terminal(run_id)
       … existing published / review handling (unchanged) …
       if run failed:
           outcome = await self._offer_lane_repair(thread_id, run_id)
           if outcome == "resumed" and resumes < 3:
               resumes += 1
               forget the cached terminal status for run_id (whatever _await_terminal /
                 _run_status reads); re-mark the <run_id>:parent card in_progress;
               wait up to 120 s (poll every 2 s via the same reader) for the run's status to
                 leave the terminal set;
               if it never does: post "The resume did not start — see <recover log path from
                 the /recover reply>", close the card failed, discard run_id from
                 _lane_repair_emitted, return;
               discard run_id from _lane_repair_emitted   # a later failure is announced again
               continue                                    # follow the resumed run to its next end
           …close the marker as today for "declined"/"none"; leave it open for "offered"…
       break
   ```
   Never call `_start_child_follow` from inside the follower for the same run.
3. **`_lane_repair_emitted` is discarded** whenever the follower moves on from a failure
   (resumed, declined, or a resume that never started). The dedupe still blocks a duplicate
   message for the SAME failure.

## Tests (in `tests/unit/test_acp_lane_repair.py`; do NOT stub `_follow_in_thread`, `_await_terminal` or the loop)

- **Fake reader with the status sequence** `failed` → (after resume) `executing` → `published`.
  The user picks `lane:deepseek`. Then `handle_recover` is called once with `"<run> --lane
  codex_lens=deepseek"`, the card ends `completed`, and the follower returns.
- **Sequence** `failed` → `executing` → `failed`. Two permission prompts are shown, the second
  after the second failure. The second pick is "Leave it", so the card ends `failed`.
- **The resume never starts** (status stays `failed` past the wait): a "did not start" message and
  the card closed `failed`. Use a short wait via a monkeypatched constant.
- **At most 3 resumes:** a 4th failure gets no further prompt.
- Keep the existing passing tests.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_acp_lane_repair.py tests/unit/test_retry_notify.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/acp/agent.py tests/unit/test_acp_lane_repair.py` → clean.
- `git diff c2f5f473 --stat` touches only the files in scope.
