# SDE-4c per-epic budget: the scheduler enforces `budget.per_epic_usd`

## Goal

`agents.yaml` declares `budget.per_epic_usd` (repo default 5.00, the primary
home sets 50.00), and the IDE and SDK display it, but no gate reads it
(`docs/operator/env-vars.md` says so explicitly). An epic's retry loop in
`mini_ork/scheduler.py:dispatch_epic` can therefore spend without bound across
attempts, and the self-driving loop (fix epics opened automatically by
`recovery/escalation.py`) has no budget envelope at all. Enforce it.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/scheduler.py`:
   - `epic_spend_usd(epic_id: str, db: str | None = None) -> float` — the sum of
     `task_runs.cost_usd` over the distinct run ids in `epic_attempts` for this
     epic, plus `epics.last_run_id` when it is not already among them. Missing
     tables or rows → 0.0.
   - `per_epic_cap(home: str, root: str) -> float | None` — `MO_PER_EPIC_USD`
     when set (a non-numeric or negative value raises `ValueError` naming the
     variable: fail closed, never a silent default); else `budget.per_epic_usd`
     from the agents config as dispatch sees it (HOME's `config/agents.yaml`,
     else ROOT's, merged with the personal overlay — the same resolution as
     `mini_ork/ide_pages/lanes.py:_agents`, built from
     `mini_ork.dispatch.agents_config.personal_path` and `.merge`); else `None`
     (no cap configured → no enforcement). A configured value that is not a
     number raises `ValueError`.
   - In `dispatch_epic`, before the epic is marked in progress and again before
     every retry attempt: when the cap is not `None` and
     `epic_spend_usd(...) >= cap`, set the epic `blocked` with
     `held_reason='per_epic_budget'` and the note
     `per-epic budget: spent $<spent> >= cap $<cap>` (two decimals), print the
     same line, dispatch nothing, and return `("budget_blocked", 0)`. In
     `dry_run`, print `[dry-run] would block: <epic_id> (<same note>)` and
     change nothing.
   - An epic blocked for budget stays blocked until an operator raises the cap
     or clears it; `pick_ready` must not select it (check whether `blocked`
     epics are already excluded; if not, exclude
     `held_reason='per_epic_budget'`).
2. `docs/operator/env-vars.md`: replace the "display only" paragraph about the
   `budget:` block with: `per_epic_usd` is enforced by the scheduler per epic
   across all its attempts (override `MO_PER_EPIC_USD`); `per_run_usd` and
   `daily_cap_usd` remain display only, and `MO_DAILY_BUDGET_USD` is still the
   dispatch circuit.

## Files in scope (touch ONLY these)

- `mini_ork/scheduler.py`
- `docs/operator/env-vars.md`
- `tests/unit/test_scheduler_epic_budget.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_scheduler_epic_budget.py`, hermetic temp home + sqlite; use `dispatch_epic`'s `runner_cmd` test seam, never a real run)

- `epic_spend_usd` sums two attempts' runs and counts `last_run_id` once;
- no cap configured (no env, no budget block) → `per_epic_cap` is `None` and
  dispatch proceeds;
- `MO_PER_EPIC_USD=abc` → `ValueError`;
- a home `agents.yaml` with `budget.per_epic_usd: 3` and an epic that already
  spent $3.50 → `dispatch_epic` returns `("budget_blocked", 0)`, the runner is
  never invoked, the epic is `blocked` with `held_reason='per_epic_budget'`;
- the same epic with `MO_PER_EPIC_USD=10` → dispatches;
- the personal overlay's `budget.per_epic_usd` wins over the home file;
- `dry_run=True` on an over-budget epic → prints the would-block line and the
  epic row is unchanged;
- `pick_ready` does not return a budget-blocked epic.

## Verification commands

- `pytest -q tests/unit/test_scheduler_epic_budget.py tests/unit/test_scheduler_py.py tests/unit/test_scheduler_retry.py tests/unit/test_scheduler_epic_filter.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_scheduler_epic_budget.py --junitxml="${MINI_ORK_RUN_DIR}/epic-budget-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/epic-budget-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/scheduler.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
