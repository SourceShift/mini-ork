# ECOSYSTEM §6 · K8: the RouteMoA screen reads the slice's own outcomes, not agent_performance_memory's groups-won count

## Goal

The RouteMoA dominance screen (`mini_ork/dispatch/screening.py`, wired at
`mini_ork/lane_router.py:599-609`) puts Wilson intervals around each candidate
lane's success rate and drops dominated lanes. It reads `(success_count,
runs_count)` from `agent_performance_memory` via
`AdvantageStore.fetch_lane_run_stats` (`mini_ork/learning/advantage_store.py:168-187`).
Those columns are not a success count and a run count:

- `lane_router.recompute_advantages` runs after `write_grpo_advantages`
  (`mini_ork/cli/execute.py:2130` then `:2136`) and overwrites
  `success_count` with the number of GRPO groups the lane won
  (`lane_router.py:253`, `:347-349`), while `runs_count` counts rows. So
  "wins / rows" falls as a lane gets busier. Live: glm on framework_edit has
  412 runs and 3 "successes", but a real reviewer success rate of 0.76.
- The table has no `node_type`, so reviewer verdicts are mixed into
  implementer screens.

On live data this flips 3 of 6 picks: framework_edit implementer
deepseek_flash → deepseek, framework_edit researcher opus → deepseek, code_fix
implementer minimax → deepseek_flash. The best high-volume lane is screened
out. Learned routing currently decides almost nothing because recent traffic is
pinned framework-edit runs; this must be fixed before learned picks come back
(code_fix's unpinned reviewer, where learned did 0.81 vs 0.45 success).

Fix: count real successes over the routed slice's own reward-bearing
`execution_traces` rows, the same (task_class, node_type, objective_domain)
slice the router learns from. Change nothing else.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/learning/advantage_store.py`
   - Add `import json`.
   - Add a module-level helper right after `resolve_db_path`. It parses
     node_type exactly the way `recompute_advantages` groups rows
     (`lane_router.py:140-145`):
     ```python
     def _trace_node_type(raw) -> str:
         try:
             return json.loads(raw or "{}").get("node_type") or "unknown"
         except Exception:
             return "unknown"
     ```
   - Replace `fetch_lane_run_stats` with
     `fetch_lane_run_stats(self, task_class: str, node_type: str = "", objective_domain: str = "") -> dict`,
     returning `{lane: (successes, runs)}`:
     - `cols = self.execution_trace_columns()`. If `cols` is empty, raise
       `sqlite3.OperationalError("no such table: execution_traces")`
       (`PRAGMA table_info` returns no rows for a missing table instead of
       raising, and the caller's "any failure → unscreened" contract needs the
       raise).
     - Run `SELECT agent_version_id, status, verifier_output FROM execution_traces WHERE task_class = ? AND agent_version_id IS NOT NULL AND agent_version_id <> '' AND objective_domain IS NOT NULL AND objective_domain <> '' AND reward_g IS NOT NULL`.
       Add `AND objective_domain = ?` when `objective_domain` is non-empty. When
       `"validity" in cols`, also add
       `AND (validity IS NULL OR trim(validity) = '' OR lower(trim(validity)) = 'valid')`.
     - For each row, skip it when `node_type` is non-empty and
       `_trace_node_type(verifier_output) != node_type`. Otherwise add 1 to
       `runs`, and 1 to `successes` when `status == "success"`.
     - Do not read `agent_performance_memory`; do not catch exceptions.
     - The docstring states why the source changed (the groups-won overwrite,
       the missing node_type).
   - Nothing else in the file changes. `fetch_lane_capabilities` stays on
     `agent_performance_memory`.
2. `mini_ork/lane_router.py`, `_pick_lane`:
   - Change the call to
     `lane_stats = store.fetch_lane_run_stats(task_class, node_type, objective_domain)`.
   - Rewrite the comment at lines 599-602 to say the stats are the slice's own
     outcomes from `execution_traces`.
   - The surrounding `try/except` and every other line stay unchanged.
3. `mini_ork/dispatch/screening.py`: docstring only. Replace "Given per-lane
   ``(success_count, runs_count)`` from ``agent_performance_memory``" with
   "Given per-lane ``(successes, runs)`` over the routed slice's reward-bearing
   ``execution_traces`` (``AdvantageStore.fetch_lane_run_stats``)".

## Files in scope (touch ONLY these)

- `mini_ork/learning/advantage_store.py`
- `mini_ork/lane_router.py`
- `mini_ork/dispatch/screening.py` (docstring only)
- `tests/unit/test_route_screening.py` (only the two fixture additions below)
- `tests/unit/test_route_screening_slice_stats.py` (new)

In `tests/unit/test_route_screening.py`:
- Add a helper `_seed_traces(path, rows)` where each row is
  `(lane, successes, runs)`. It runs
  `CREATE TABLE IF NOT EXISTS execution_traces (trace_id TEXT, agent_version_id TEXT, task_class TEXT, objective_domain TEXT, code_region TEXT, verifier_output TEXT, reward_g REAL, cost_usd REAL, status TEXT, created_at TEXT)`,
  then per lane inserts `successes` rows with `status='success'`,
  `reward_g=1.0` and `runs - successes` rows with `status='failure'`,
  `reward_g=-1.0`; every row uses `task_class=TC`, `objective_domain=OD`,
  `verifier_output='{}'`, `created_at='2026-10-01T00:00:00.000Z'` and a unique
  `trace_id`.
- Call `_seed_traces(path, [("lane-good", 18, 20), ("lane-bad", 2, 20)])` right
  after `_make_db` in `test_pick_lane_screens_the_dominated_exploration_pick`.
- Call `_seed_traces(path, [("lane-borda-keep", 18, 20), ("lane-borda-bad", 1, 20)])`
  right after `_make_db` in `test_pick_lane_equirouter_override_only_considers_kept_lanes`.
- Change no assertion, no other test, and not `_make_db`.

## Tests (`tests/unit/test_route_screening_slice_stats.py`: hermetic, tmp sqlite only)

Constants `TC="slice-stats-tc"`, `OD="od1"`. The trace schema has the columns
above plus `validity TEXT` unless a test says otherwise. The autouse env fixture
pins the same knobs as `test_route_screening.py`'s `_router_env`: unset
`MO_ROUTEMOA*`; set `MO_LEARNING_MIN_SAMPLES=3`, `MO_ROUTER_UCB_C=0.5`,
`MO_ROUTER_CONTEXTUAL=0`, `MO_EQUIROUTER=1`, `MO_ENTROROUTER=1`,
`MO_ROUTER_ANCHOR=0.5`, `MO_ROUTER_ENTROPY_TAU=3`.

- **Real successes for the slice.** Seed:
  - lane-a, implementer, od1: 9 success (validity NULL), 1 failure (validity 'valid')
  - lane-b, implementer, od1: 2 success, 8 failure
  - lane-a, reviewer, od1: 5 failure
  - lane-a, implementer, od2: 5 failure
  - lane-a, implementer, od1, `reward_g NULL`: 3 failure
  - lane `''`, implementer, od1: 2 success
  - lane-a, implementer, od1, validity 'infra_failed': 4 failure
  - lane-b, implementer, od1, task_class `other-tc`: 10 success

  `AdvantageStore(path).open().fetch_lane_run_stats(TC, "implementer", "od1")`
  equals `{"lane-a": (9, 10), "lane-b": (2, 10)}`.
- **Empty filters widen the slice.** Same DB:
  `fetch_lane_run_stats(TC)` == `{"lane-a": (9, 20), "lane-b": (2, 10)}`;
  `fetch_lane_run_stats(TC, "", "od1")` == `{"lane-a": (9, 15), "lane-b": (2, 10)}`;
  `fetch_lane_run_stats(TC, "reviewer", "od1")` == `{"lane-a": (0, 5)}`.
- **node_type parsing matches recompute.** 4 lane-c success rows in TC/od1 with
  `verifier_output` `"not json"`, `'{"node_type": ""}'`, the double-encoded
  `'"{\"node_type\": \"implementer\"}"'`, and NULL →
  `fetch_lane_run_stats(TC, "unknown", "od1")` == `{"lane-c": (4, 4)}`.
- **No `validity` column is fine.** Schema without `validity`, 3 lane-d success
  rows in TC/od1 → `{"lane-d": (3, 3)}`.
- **A missing trace table raises.** DB with only `agent_performance_memory` →
  `fetch_lane_run_stats(TC)` raises `sqlite3.OperationalError`.
- **The pick screens on slice outcomes, not APM's groups-won count.**
  `ensure_advantage_tables()`, then `agent_performance_memory` with the same DDL
  as `test_route_screening._make_db`. APM rows (`upsert_agent_performance`):
  lane-hi success 3, runs 400, rel 0.0; lane-lo success 8, runs 95, rel 0.0.
  Domain rows (`upsert_domain_advantage(lane, TC, "implementer", OD, ...)`):
  lane-hi rel 0.05, runs 400, succ 3, z 0.5; lane-lo rel 0.02, runs 95, succ 8,
  z 0.1. Traces: lane-hi implementer OD 380 success + 20 failure; lane-lo
  implementer OD 80 success + 15 failure; lane-hi reviewer OD 50 failure.
  `lane_router._pick_lane(TC, "implementer", OD, "", path)` gives
  `detail["lane"] == "lane-hi"`, `detail["source"] == "domain"`,
  `[e["lane"] for e in detail["screened"]] == ["lane-lo"]`,
  `detail["screened"][0]["dominated_by"] == "lane-hi"`.
- **Opting out still works.** Same DB with `MO_ROUTEMOA=0` → pick `lane-hi`,
  `screened == []`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_route_screening_slice_stats.py tests/unit/test_route_screening.py tests/unit/test_lane_router_py.py tests/unit/test_lane_router_zscore_py.py tests/unit/test_advantage_store_py.py tests/unit/test_decision_service_py.py tests/unit/test_entrorouter_py.py`
- `ruff check --select F,E9 mini_ork/learning/advantage_store.py mini_ork/lane_router.py mini_ork/dispatch/screening.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_route_screening_slice_stats.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k8-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k8-junit.xml`

## Success criteria

- Both verification commands pass (drop any listed test file that does not
  exist; do not create it).
- Every pre-existing test passes unchanged, except the two named tests in
  `tests/unit/test_route_screening.py`, which gain one `_seed_traces` call each
  and no assertion edits.
- `fetch_lane_run_stats` no longer reads `agent_performance_memory`.
- The diff touches only the five files in scope.
