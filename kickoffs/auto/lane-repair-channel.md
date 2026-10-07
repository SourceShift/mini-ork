# Lane repair (C) — tell the user in their Zed thread, and resume on their word

## Why

The user asked: when a run dies because its lane is unavailable (live case: MiniMax 429 "Token Plan
usage limit reached" on `prior_art_lens`), don't just throw the run away. Tell them in their Zed
thread that the run needs repair, suggest another lane, and continue on their answer.

What exists now (merged):
- `mini_ork.recovery.retry_hint.load_or_compute(home, run_id, write=False)` returns, for such a run:
  ```
  {"failed_node": "prior_art_lens", "retryable": True, "strategy": "resume",
   "needs_change": {"kind": "lane", "summary": "minimax is out of quota",
                    "detail": "API Error: Request rejected (429) · Token Plan usage limit reached…",
                    "lane": "minimax", "alias": "codex_lens", "nodes": ["prior_art_lens", "implementer"],
                    "suggestions": [{"lane": "deepseek", "reason": "19 successful calls in the last 6h"}, …]},
   "command": "mini-ork recover <run> --lane codex_lens=deepseek"}
  ```
- `mini-ork recover <run> --lane <alias>=<lane>` resumes from the failed node, reusing finished
  nodes. ACP `/recover <run> [--lane a=b] [--force]` exists (`mini_ork/acp/commands.py:671`,
  `handle_recover`).
- `retry_notify.notify(home, run_id)` (called at `mini_ork/cli/main.py:1009-1014` when a run
  fails) writes `owner.json` / `NEEDS-CHANGE.md` / `retry-gate.json` and an inbox row.

What is missing (mapped 2026-10-07):
1. **Nothing records which thread started a run.** `owner.json` is written at start only if
   `MO_RUN_OWNER` is set, and no launcher sets it: neither ACP `/run` (`_prompt_thread_direct`,
   `mini_ork/acp/agent.py:1603`, launch around :1660-1668) nor orchestrator child runs (the MCP
   `start_run` in `mini_ork/mcp_context/server.py:526-614`; `extra_env` at :544; the orchestrator
   passes `extra_mcp_env` at `agent.py` ~:3660-3670 but no thread id).
2. **The thread hears nothing useful.** `_follow_in_thread` (`agent.py:3283`) stops on the terminal
   status and closes the marker as `failed`. Nothing says why or offers a way out. The pattern to
   copy is `_offer_run_review` (`agent.py:3395-3460`): during an active turn it shows buttons on the
   run's `<run_id>:parent` tool card via `self._conn.request_permission(...)`; with no active turn
   it posts a one-time text message (`_emit_ready_to_review_once`, `agent.py:3560`).
3. **Offline:** if the thread isn't loaded when the run fails, nothing reaches it. Appending an
   `{"type": "update", …}` record to `<home>/acp-threads/<thread_id>.jsonl`
   (`mini_ork/acp/threads.py`, `ThreadStore.append`) is replayed on the next `session/load`
   (`agent.py:4834-4842`).

## Files in scope (touch ONLY these)

- `mini_ork/acp/agent.py`: `_prompt_thread_direct` (owner env), the orchestrator `extra_mcp_env`
  block, `_follow_in_thread`, and a new `_offer_lane_repair` (+ a small handler)
- `mini_ork/mcp_context/server.py`: ONLY `start_run`'s `extra_env` (owner)
- `mini_ork/recovery/retry_notify.py`: ONLY `notify` (the offline thread append)
- `tests/unit/test_acp_lane_repair.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **Owner = thread.**
   - ACP `/run`: before launching, set `self._run_env.setdefault(new_run_id, {})["MO_RUN_OWNER"] =
     f"thread:{session_id}"`. `_run_env` is already merged into the launch env; verify that, and
     wire it if not.
   - Orchestrator: add `extra_mcp_env["MO_THREAD_ID"] = thread_id`.
   - `start_run`: when `os.environ.get("MO_THREAD_ID")` is set, add `extra_env["MO_RUN_OWNER"] =
     f"thread:{that}"`.
   `record_owner_at_start` then writes `owner.json` with kind `thread`. Check that
   `retry_notify.owner()` accepts `thread:<id>`; if its kind list rejects it, extend the list.
   It's in scope.
2. **`_offer_lane_repair(thread_id, run_id) -> bool`**, called from `_follow_in_thread` when the
   run ended `failed`, before the marker is closed:
   - compute `hint = retry_hint.load_or_compute(home, run_id, write=False)` (it can take a few
     seconds after the status flips; retry up to 3 times, 2 s apart, until `needs_change` is
     classified);
   - if `hint.needs_change.kind != "lane"` → return False (unchanged behaviour);
   - **active turn** (same rule as `_offer_run_review`): `request_permission` on the
     `<run_id>:parent` card:
     - title: `f"{run_id} stopped: {lane} {summary_tail}. Resume from {failed_node}?"`;
     - options, up to 2 suggestions: `option_id=f"lane:{s.lane}"`, name `f"Resume on {s.lane}
       ({s.reason})"`, kind `allow_once`; then `option_id="same"` "Retry on {lane}"
       `allow_once`; `option_id="abandon"` "Leave it" `reject_once`.
   - on `lane:<x>`: run `handle_recover(self, thread_id, f"{run_id} --lane {alias}={x}")` (it
     already spawns `mini-ork recover`), post its reply as an agent message, and follow the
     resumed run in this thread via `_start_child_follow` (the same run id).
   - on `same`: `handle_recover(self, thread_id, run_id)`.
   - on `abandon`: close the marker `failed` and post "Left as is. You can resume later with:
     <command>".
   - **inactive turn or no connection:** post ONE agent message (dedupe per run like
     `_ready_to_review_emitted`):
     ```
     **<run_id> needs repair.** <provider> lane `<lane>` (used by <alias>: <nodes>) failed:
     <detail first 200 chars>
     Resume from `<failed_node>` on a working lane:
     /recover <run_id> --lane <alias>=<s1>   (<reason>)
     /recover <run_id> --lane <alias>=<s2>   (<reason>)
     ```
     Return True. Keep the marker `in_progress` until a choice is made (as review does).
3. **Offline delivery** (`retry_notify.notify`): when the owner kind is `thread` and the hint kind
   is `lane`, append ONE record (dedupe by a marker id `lane-repair:<run_id>`) to the thread
   JSONL via `ThreadStore(home).append(thread_id, {...})`, in the exact `{"type": "update", …}`
   shape that `agent.py:4834-4842` replays as an `AgentMessageChunk`, with the same text as the
   inactive-turn message. Read the replay code and match it.
4. Never raise into the follower. Every new path is try/except → falls back to today's behaviour.

## Tests (`tests/unit/test_acp_lane_repair.py`; follow the fixture style of the existing ACP tests: fake conn, fake launcher)

- `/run` launch puts `MO_RUN_OWNER=thread:<sid>` in that run's env. `start_run` with
  `MO_THREAD_ID` → `extra_env["MO_RUN_OWNER"]`.
- Follower on a failed run whose (monkeypatched) hint is `kind: lane`:
  - active turn → `request_permission` called with `lane:deepseek`, `same`, `abandon`;
  - choosing `lane:deepseek` calls `handle_recover` with `"<run> --lane codex_lens=deepseek"`
    and starts a follow.
- Inactive turn → exactly one agent message with both `/recover … --lane` lines; a second
  call → no duplicate.
- A non-lane hint → no prompt, today's behaviour (marker `failed`).
- `notify` with an owner `thread:t1` and a lane hint → one record appended to
  `acp-threads/t1.jsonl` in the replay shape; a second `notify` → no duplicate.
- Exceptions inside `_offer_lane_repair` don't break the follower.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_lane_repair.py && sleep 3 && env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_retry_notify.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/acp/agent.py mini_ork/mcp_context/server.py mini_ork/recovery/retry_notify.py tests/unit/test_acp_lane_repair.py` → clean.
- Proof (read-only): render the inactive-turn message for the live run
  `learn-memory-tab-r2-20261007142114` (call the message builder with the real
  `load_or_compute(..., write=False)` hint) and paste it.
- `git diff --stat` touches only the files in scope.
