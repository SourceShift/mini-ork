# RSI gap: RouteMoA pre-inference screening of dominated lanes

## Goal

RouteMoA (arXiv 2601.18130) screens candidate models with a cheap learned
predictor BEFORE inference and drops the ones that cannot win, which cut cost
by ~90% in the paper. mini-ork's router already ranks lanes from learned
per-lane stats (`mini_ork/lane_router.py:_pick_lane` → `_select_best_lane_scored`,
with UCB exploration, EntroRouter recovery floor and EquiRouter Borda), but it
never removes a lane that is statistically dominated on this task class, so
exploration keeps spending real dispatches on lanes the data already rules out.

Add a dominance screen in front of selection.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New `mini_ork/dispatch/screening.py`:
   - `wilson_bounds(successes: int, n: int, z: float = 1.96) -> tuple[float, float]`
     (lower, upper; `n <= 0` → `(0.0, 1.0)`).
   - `screen(candidates: list, stats: dict[str, tuple[int, int]], *,
     min_runs: int) -> tuple[list, list[dict]]` — `candidates` is the list
     `_select_best_lane_scored` consumes (do not change its element shape; read
     the lane name the same way that function does). `stats` maps lane →
     `(success_count, runs_count)`. A candidate is DOMINATED when both it and
     some other candidate have `runs_count >= min_runs` and its Wilson upper
     bound is strictly below that other candidate's Wilson lower bound.
     Returns `(kept, screened)` where `screened` is
     `[{"lane", "upper", "dominated_by", "dominator_lower"}]`. Never returns an
     empty `kept` when `candidates` is non-empty; lanes below `min_runs` are
     never screened (they are still being explored).
   - `enabled() -> bool`: `MO_ROUTEMOA` unset or not `"0"` → True (default ON,
     opt-out `MO_ROUTEMOA=0`).
   - `min_runs() -> int`: `MO_ROUTEMOA_MIN_RUNS`, default 8, tighten-only: a
     value below 8 is clamped up to 8.
2. `mini_ork/lane_router.py:_pick_lane`: when `screening.enabled()`, fetch the
   per-lane `(success_count, runs_count)` for this `task_class` from
   `agent_performance_memory` once per pick (through the already-open
   `AdvantageStore`; add a small read method there only if none exists — that
   would put `mini_ork/learning/advantage_store.py` in scope, which is allowed),
   and pass each slice's candidates through `screening.screen` before
   `_select_best_lane_scored`. The EquiRouter override must also only choose
   among kept lanes. The returned detail dict gains `"screened": [...]` (empty
   list when nothing was screened or the screen is off); every existing key
   keeps its meaning. A failure while fetching stats or screening must never
   break a pick: fall back to the unscreened candidates.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/screening.py` (new)
- `mini_ork/lane_router.py`
- `mini_ork/learning/advantage_store.py` (only if a stats read method is needed)
- `tests/unit/test_route_screening.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_route_screening.py`, hermetic temp sqlite)

- `wilson_bounds(0, 0) == (0.0, 1.0)`; bounds tighten as n grows at a fixed rate;
- `screen`: lane A 18/20, lane B 2/20 (both ≥ min_runs) → B screened, dominated by A;
- lane B with only 3 runs (below min_runs) → never screened;
- overlapping intervals (A 11/20, B 9/20) → nothing screened;
- every candidate dominated by another in a chain still leaves at least one kept;
- `MO_ROUTEMOA_MIN_RUNS=2` → clamped to 8;
- `_pick_lane` end to end on a temp DB where a dominated lane would otherwise be
  the UCB/exploration pick → the pick is a kept lane and `detail["screened"]`
  names the dominated lane; with `MO_ROUTEMOA=0` the old pick is returned and
  `screened == []`;
- a stats read that raises → the pick still returns (unscreened).

Also run the existing router suites listed in the verification command.

## Verification commands

- `pytest -q tests/unit/test_route_screening.py tests/unit/test_lane_router_py.py tests/unit/test_entrorouter_py.py tests/unit/test_decision_service_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_route_screening.py tests/unit/test_lane_router_py.py --junitxml="${MINI_ORK_RUN_DIR}/routemoa-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/routemoa-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/dispatch/screening.py mini_ork/lane_router.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
