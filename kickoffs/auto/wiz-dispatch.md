# Scope the scheduler's ready-set to chosen epics

## Goal

`mini-ork scheduler` drains every ready epic on the board: `pick_ready(db)`
(`mini_ork/scheduler.py:169`) returns the whole priority-ordered ready-set and
the pool consumes it. There is no way to run the scheduler against a *subset* of
epics, so an operator who wants to advance two specific epics must instead let
the loop touch everything else that happens to be ready too.

Add a readiness filter: an epic-id allow-list that intersects the ready-set, so
`mini-ork scheduler --epic <id> --epic <id2>` (and the `MO_SCHED_EPIC_FILTER`
env equivalent) advances only the named epics.

## Acceptance

- AC1 — `pick_ready` gains an optional keyword parameter `only: set[str] |
  frozenset[str] | None = None`. The existing query (priority inheritance,
  ties oldest-first, hard-dep gating) is UNCHANGED; the filter is a post-step
  intersection applied to the returned ids. With `only=None` the function falls
  back to the `MO_SCHED_EPIC_FILTER` env var (comma-separated ids); when that is
  also unset the behaviour is exactly today's (whole ready-set).
- AC2 — `mini_ork scheduler` accepts a repeatable `--epic <id>` flag. The ids
  collected are threaded to the ready-set pick (pass `only=` down; do NOT mutate
  `os.environ` directly — the house rule is to mutate process env only through
  `mini_ork.context.apply_env_overrides` / `scoped_environ`). `--epic` and
  `MO_SCHED_EPIC_FILTER` feed the same filter; if both are given, `--epic` wins.
- AC3 — filtering is a pure intersection: ids in the filter that are not ready
  (blocked, archived, absent) are simply not dispatched, and an empty result is
  NOT an error — the loop idles/returns as it does today when nothing is ready.
  Do not change when an epic is marked done/escalated.
- AC4 — `_USAGE` (module docstring, `scheduler.py:31`) documents both the
  `--epic` flag and `MO_SCHED_EPIC_FILTER`, in the same style as the existing
  `MO_SCHED_*` lines.
- AC5 — a new unit test builds a temp SQLite DB with the `epics` +
  `epic_dependencies` schema (mirror how existing scheduler tests construct the
  DB), inserts several ready epics, and asserts: `pick_ready(db)` returns them
  all; `pick_ready(db, only={"a"})` returns only `{"a"}`; a filter naming a
  non-ready id yields `[]`; and `MO_SCHED_EPIC_FILTER` is honoured when
  `only=None`.

## Files in scope

- `mini_ork/scheduler.py` — `pick_ready` (:169), the CLI flag parse in `main`
  (:745+), the drain-loop call sites (:718, :828), and `_USAGE` (:31).
- `tests/unit/test_scheduler_epic_filter.py` (new)

## Out of scope

- Do NOT change the ready-set SQL semantics (priority inheritance, dep gating,
  attempt ordering) — only intersect the result.
- Do NOT touch the retry/attempt accounting, the hooks, `--once`, `--dry-run`,
  `--idle-secs`, `--max-iters`, or `--budget-cap-usd`.
- Do NOT edit any other module; do NOT add a subcommand.

## Verification command

`python3 -m pytest tests/unit/test_scheduler_epic_filter.py -q`
