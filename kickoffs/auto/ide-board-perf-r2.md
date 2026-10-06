# IDE board perf — revision 2 (Opus review of run ide-board-perf-20261006205933)

## Goal

Commit `c0297a49` (branch `wt/ide-pages`) is the first pass of
`kickoffs/auto/ide-board-perf.md`. Opus returned needs_revision. Fix exactly the
items below; keep everything Opus verified as correct (lazy `mini_ork.acp`,
batched fleet queries, A2–A5).

## Files in scope

- `mini_ork/cli/board_cmd.py`
- `mini_ork/ide_pages/header.py`
- `mini_ork/acp/fleet.py`
- `mini_ork/ide_pages/run.py` (one dead branch)
- `tests/unit/test_ide_pages_actions.py`, `tests/unit/test_board_cmd.py`,
  `tests/unit/test_lane_overlay_resolution.py`, `tests/unit/test_lane_chain_resolution.py`,
  and new tests where listed below

No other file changes.

## Fixes (exact)

1. **`--shell` must not build the full board.** `board_cmd.py` ≈ line 473 calls
   `board(home)` then `_shell_subset(payload)`. Build the shell document directly:
   `_runs(home)` + `header(home, counts)` + `project/home/generated_at/version/errors`.
   Never call `_learnings`, `_automations`, `_scheduler`, `_recipes`, `_workspaces`
   for `--shell`. Target: `time bin/mini-ork board --json --shell --home
   /Volumes/docker-ssd/Migration/Development/researcher/.mini-ork` < 1.5 s warm —
   report the measured number.
2. **Header worktree count regression.** `header.py:31` returns None for the
   relative `.git` that `git rev-parse --git-common-dir` prints in a main
   checkout → count 0. Use `git rev-parse --path-format=absolute --git-common-dir`
   (or resolve against the project dir). Count = 1 + entries of
   `<common>/worktrees/`.
3. **`fleet.py:215` dead cache read.** The diff cache is a JSON *list*
   (`diffs.py:266`); `_diff_counts_cached` always returns None. Parse the list
   (sum added/removed per entry the same way `_diff_counts` does) or remove it.
4. **`fleet.py:254` event filter.** Add `AND event_type IN ('node_start','node_end')`
   to the batched `run_events ... IN (...)` query so state/step match the old
   `fetch_node_lifecycle_events` read exactly.
5. **`run.py:214`** remove the dead `if n.start is None: continue`.
6. **Actions test** (`tests/unit/test_ide_pages_actions.py`):
   - use `mini_ork.cli.main.SUBCOMMAND_REGISTRY` (not `SUBCOMMANDS`) — no skips;
   - seed the run the run-page tabs use (a run row + run dir), or build the run
     page for a run the fixture creates; assert `page["ok"] is True` (no
     `.get("ok", True)`);
   - factor `build_parser()` out of `board_cmd.main` and use the real parser in
     the test instead of a hand-copied one.
7. **Part B tests** (new, in `tests/unit/test_board_cmd.py` unless noted):
   - `board --json --shell` returns exactly
     `{version, project, home, generated_at, header, runs, counts, errors}` and does
     not call `_recipes`/`_learnings` (monkeypatch them to raise);
   - header worktree count == 3 on a temp repo with 2 linked worktrees, with
     `subprocess.run` patched to fail on `git worktree list`;
   - `_runs` output identical before/after on a fixture home with ~30 runs
     (compare to a frozen expected list built in the test);
   - `import mini_ork.acp.fleet` leaves `mini_ork.acp.agent` out of `sys.modules`
     (run in a subprocess so the test is order-independent);
   - `_effective_lanes` honours `$MINI_ORK_RUN_DIR/config/agents.yaml`
     (`opus_lens: opus` in the snapshot wins over the home's `glm`).
8. **Lane test isolation**: in `test_lane_overlay_resolution.py` and
   `test_lane_chain_resolution.py`, `monkeypatch.delenv("MINI_ORK_RUN_DIR",
   raising=False)` in the affected tests (they fail when run inside a mini-ork run).

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_*.py tests/unit/test_lane_overlay_resolution.py tests/unit/test_lane_chain_resolution.py`
  passes, with `MINI_ORK_RUN_DIR` unset AND set (report both), except tests that
  already fail on `c0297a49` for unrelated reasons (name them).
- `board --json --shell` timing on the researcher home reported, < 1.5 s warm.
- ruff clean on touched files; diff only touches files in scope.
