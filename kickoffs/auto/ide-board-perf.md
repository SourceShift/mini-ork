# IDE pages — P1 revision, a `board --json` fast enough to poll, lanes that honour the run snapshot

## Goal

The mini-ork IDE polls `mini-ork board --json --home H` every 5 s. On a home with
4,763 runs and 1,115 git worktrees one read takes 8–10 s (measured per section:
runs 4.0 s, header 3.4 s, recipes 1.0 s, the rest < 0.1 s), so a Python process is
always running. Make the shell's read cheap, and fix one lane-resolution
inconsistency found while running this work.

## Files in scope

- `mini_ork/cli/board_cmd.py`
- `mini_ork/ide_pages/header.py`
- `mini_ork/acp/__init__.py`
- `mini_ork/acp/fleet.py` (only if the runs section needs it)
- `mini_ork/ide_pages/runs.py`, `mini_ork/ide_pages/run.py` (the two N+1s below)
- `mini_ork/dispatch/llm_dispatch.py` (`_effective_lanes` only)
- `mini_ork/ide_pages/changes.py`, `orch.py`, `setup.py` (Part A only)
- tests: `tests/unit/test_ide_pages_actions.py` (new), `tests/unit/test_board_cmd.py`, `tests/unit/test_ide_pages_runs.py`,
  `tests/unit/test_ide_pages_run.py`, and the existing tests of `llm_dispatch`
  lane resolution (find them with `grep -rn _effective_lanes tests`)

No other file changes.


## Part A — revision of the P1 run (from the Opus review)

A1. **New `tests/unit/test_ide_pages_actions.py` that never runs a handler.**
    Build every page/tab on a temp home; collect every `cli` action. For each:
    the subcommand exists in the `mini_ork.cli.main` registry; for `board` verbs,
    `board_cmd`'s parser accepts the args (call its argparse, do not execute);
    for other subcommands, build that module's argparse parser and call
    `parse_known_args(args)` — when the action's `home` is not `false`, also with
    `--home <tmp>` appended, and assert it parses. Never call a subcommand's
    `main`/handler. Every page/tab must build with `ok: true` (assert it; no
    silent skips — keep an explicit list if a key is expected to be missing).
A2. `changes.py:106` confirm text: `"Merge this run's branch into <base>?"` (same as run.py).
A3. `board_cmd.py`: reject the extra positional (`target`) unless the verb is `gate`
    (usage error, exit 2).
A4. `orch.py:45`, `setup.py:41`: narrow `except Exception` back to
    `sqlite3.OperationalError` (a SQL bug must not render as an empty panel).
A5. Add the trailing newline to `tests/unit/test_board_cmd.py`.

## Part B — performance

### Changes (exact)

1. **`board --json --shell`**: a light document for the IDE shell — `version`,
   `project`, `home`, `generated_at`, `header`, `runs`, `counts`, `errors` only (no
   learnings, automations, scheduler, recipes, workspaces). Without `--shell` the
   output is unchanged.
2. **Header without `git worktree list`**: count worktrees as
   `1 + number of entries in <git-common-dir>/worktrees/` (one `git rev-parse
   --git-common-dir`, then a directory listing). Branch: read `.git/HEAD` (or the
   worktree's HEAD file) directly; fall back to `git rev-parse` only if that fails.
   ContextNest: pass the 0.5 s timeout to the ping explicitly instead of
   `os.environ.setdefault("CN_TIMEOUT_SEC")` (an ambient value overrides the
   default today and can stall the poll for 8 s).
3. **Runs section ≤ 1 s** on that home: profile `_runs` (`fleet_rows(home,
   state="all", limit=50)`) and remove the dominant cost. Likely: per-candidate
   work over `CANDIDATE_LIMIT=200` rows (diff counts, file reads, per-run queries).
   Batch per-run queries into one `IN (...)` query; skip diff counting for runs
   whose cached counts exist; keep the output identical (same rows, same order,
   same counts) — assert that in a test on a fixture home.
4. **Lazy `mini_ork.acp`**: `mini_ork/acp/__init__.py` imports the whole agent
   (0.4–1.6 s) for every `board` call. Make `MiniOrkAcpAgent` / `mint_run_id`
   lazy (module `__getattr__`), so `import mini_ork.acp.fleet` does not import
   `mini_ork.acp.agent`.
5. **Page N+1s** from the review (`docs/reviews/2026-10-06-ide-pages-review.md`):
   `runs.py` `_live_node` — one query for all active runs instead of one per run;
   `run.py` `_attribute_calls` — index nodes by lane/role once instead of O(N·M).
6. **`_effective_lanes` honours the run snapshot**: when
   `$MINI_ORK_RUN_DIR/config/agents.yaml` exists, use it as the template (as
   `config_resolve.resolve_agents_yaml` already documents: run dir first), then
   merge the personal overlay as today. Today a pre-seeded run snapshot is ignored
   for lens-alias resolution, so a run pinned to other lanes silently uses the
   home's lanes.

## Tests

- `board --json --shell` returns exactly the listed keys; without the flag the
  keys are unchanged.
- Header worktree count on a temp repo with 2 linked worktrees is 3, without
  calling `git worktree list` (monkeypatch `subprocess.run` to fail on it).
- Runs: `_runs` output identical before/after on a fixture home with ~30 runs.
- `mini_ork.acp` lazy: `import mini_ork.acp.fleet` leaves `mini_ork.acp.agent`
  out of `sys.modules`.
- `_effective_lanes`: with `MINI_ORK_RUN_DIR` holding `config/agents.yaml`
  mapping `opus_lens: opus`, `resolve_lane_family("opus_lens")` returns `opus`
  even when the home's agents.yaml maps it to `glm`.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_*.py` plus the lane-resolution tests pass.
- `time bin/mini-ork board --json --shell --home /Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`
  is under 1.5 s warm (report the before/after numbers in the implementer summary).
- `ruff check` clean on touched files; the diff touches only the files in scope.
