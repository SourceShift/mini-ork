# Lane repair (B) — resume a failed run from its failed node on a different lane

## Why

A run that died because its lane is unavailable (live case: MiniMax out of quota, `429 Token Plan
usage limit reached`) can't be continued on another lane. Today:

- `mini-ork recover <run>` (`mini_ork/recovery/planner.py`, durable DAG resume, `--from-node`,
  `--strategy`, `--ack-change`, `--force` at ~:129-188 / :650-694) has **no lane option**.
  `cli_main` (~:810-850) runs execute only and **never calls `retry_notify.notify`** when the
  resumed run fails again.
- `board retry <run> [--ack-change] [--force]` (`mini_ork/cli/board_cmd.py:475-593`, parser
  ~:885-931) spawns the hint's command with the caller's env, with no lane flag.
- ACP `/recover` (`mini_ork/acp/commands.py:655-704`) only passes `--from-node`.

The plumbing exists:
- Lane aliases (`codex_lens`, …) resolve through the run's `config/agents.yaml` snapshot merged
  with the `$MINI_ORK_AGENTS` overlay (`mini_ork/dispatch/llm_dispatch.py:165-251`,
  `agents_config.py:68-87`); the snapshot itself is never overwritten.
- `mini_ork/acp/race.py:250-283` (`seed_run_config`) is an existing overlay-writing pattern.
- The checkpoint config hash doesn't include lanes (`execute.py:2823-2824`), so reused nodes stay
  valid after a lane change.

A parallel worktree (`lane-repair-hint`) makes `retry_hint` produce, for such runs,
`needs_change.kind = "lane"`, `alias`, `lane`, `suggestions`, and
`command = "mini-ork recover <run> --lane <alias>=<lane>"`. This kickoff makes that command
work.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/planner.py`
- `mini_ork/cli/board_cmd.py`: ONLY the `retry` verb and its parser args
- `mini_ork/acp/commands.py`: ONLY the `/recover` command
- `tests/unit/test_lane_repair_resume.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **`mini-ork recover <run> --lane <alias>=<lane>`** (repeatable).
   - Validate `<alias>` against the lane aliases the run's workflow nodes use (`model_lane`), and
     `<lane>` against the lanes in the effective `providers.yaml`. A bad value → exit 2 with the
     valid choices listed.
   - Write `runs/<id>/config/agents.recover.yaml`: `{"lanes": {alias: lane, …}}` merged OVER any
     overlay already named by `$MINI_ORK_AGENTS` (read it, deep-merge `lanes`). Set
     `MINI_ORK_AGENTS` to that file for the execute it launches.
   - Append one line to `runs/<id>/recover-lanes.log`:
     `<iso ts> <alias>: <old lane> -> <new lane>` (old lane from the run's snapshot or overlay).
   - Without `--lane`, behaviour is unchanged.
2. **`recover` reports failures like `run` does.** In `cli_main`, when execute returns non-zero,
   call `mini_ork.recovery.retry_notify.notify(home, run_id)` (fail-soft, exactly as
   `mini_ork/cli/main.py:1009-1014` does) and print its banner lines.
3. **`board retry <run> --lane <alias>=<lane>`** (repeatable): passed through to the spawned
   `recover` command. If the hint's `command` already carries `--lane` flags, the user's flags
   replace those with the same alias. `--lane` alone does NOT imply `--ack-change` / `--force`.
   For a `kind == "lane"` hint (`retryable: true`), `board retry <run>` with no `--lane` uses the
   hint's command as-is (the suggested lane).
4. **ACP `/recover <run> [--from-node N] [--lane a=b]… [--force]`**: parse and pass `--lane`
   and `--force` through to the spawned `mini-ork recover` (same spawn as today). The reply
   message names the lane switch:
   `"Resuming <run> from <node> with codex_lens → deepseek. Log: <path>"`.

## Tests (`tests/unit/test_lane_repair_resume.py`, temp home, no network; monkeypatch the execute call)

- `recover --lane codex_lens=deepseek` writes `agents.recover.yaml` with that lane, merged over a
  pre-existing `MINI_ORK_AGENTS` overlay (other aliases kept), sets `MINI_ORK_AGENTS` for the
  execute call, and logs the switch line.
- An unknown alias or lane → exit 2, listing the choices.
- A failing resumed execute → `retry_notify.notify` is called once.
- `board retry <run> --lane codex_lens=opus` spawns `… recover <run> … --lane codex_lens=opus`
  with no `--ack-change`. With a lane hint and no `--lane`, it spawns the hint command verbatim.
- `/recover <run> --lane codex_lens=deepseek --force` builds the expected argv and reply text.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_lane_repair_resume.py tests/unit/test_recover_verify.py tests/unit/test_board_cmd.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/recovery/planner.py mini_ork/cli/board_cmd.py mini_ork/acp/commands.py tests/unit/test_lane_repair_resume.py` → clean.
- Dry proof (no execute): `bin/mini-ork recover learn-memory-tab-r2-20261007142114 --lane codex_lens=deepseek --status`
  (or the planner's plan-only mode) shows the resume plan from `prior_art_lens` and the lane
  override. Paste it.
- `git diff --stat` touches only the files in scope.
