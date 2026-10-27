# RSI Tier-1 #10: learned routing reads the region slice (pass a segment into decide())

## Goal

`learning_governed_lane` (`mini_ork/dispatch/routing.py:184`) calls
`decision_service.decide(node_type, task_class, objective_domain, db=db)` with
no `segment`. `decide` therefore sets `code_region = ""`, and
`lane_router._pick_lane` never tries the region slice as a pick source. It goes
straight to domain, then global.

The region tables are populated. `lane_region_advantage` has 104 rows and 61
clear the sample floor. The implementer slices hold most of them:

- `framework_edit`: 42 rows, 24 at the floor;
- `code_fix`: 21 rows, 17 at the floor;
- `goal_loop`: 10 rows, 6 at the floor.

`decide()` is the only `decision_service.decide` call site on `origin/main`.

**What this does NOT fix, measured on the primary `state.db`.** Of 11,790
traces, `route_source` breaks down as follows:

| `route_source` | Traces | Where they come from |
|---|---:|---|
| NULL | 7,577 | Stage traces that never pass through `policy_route_lane` |
| `policy` | 2,477 | A launch-time policy that never consults `decide()`; framework-edit runs logged `[route] policy=workflow_default`, 1,537 rows since 10-01 |
| `pinned` | 1,399 | |
| `default` | 158 | |
| `learned` | 97 | |
| `static` | 65 | |
| `explore` | 14 | |
| `calibrated_escalation` | 3 | |

Where `decide()` is consulted it already decides: since 10-01 it returned
`learned` 18 times, `explore` 3 and `calibrated_escalation` 3, against
`default` 4. The low learned count is operational (eco6 §6, and the screen fix
`13fe4e81`), and this kickoff does not change it.

**What it does fix, and whom it reaches.** It makes the region slice a pick
source for the nodes `decide()` actually routes that have a region: unpinned
implementers under `learning_governed`. Every other recipe pins its implementer
(framework-edit to `codex_lens`, code-fix to `worker`). So the live consumers
are:

- `goal_loop`, which made 78 brain-routed implementer dispatches (50 `learned`,
  19 `default`, 9 `explore`), the last on 2026-09-26;
- `spec_driven_delivery`, with 1.

**The region can be predicted before dispatch.** The predictor reads the run's
declared scope (`run_profile.json` → `scope_allow`, parsed by the existing
`preferences.scope_paths`) and takes the most common top-level directory. A
read-only backtest covered 793 implementer traces since 09-12 that have a run
dir. It predicted a region for 663, and the prediction matched the trace's
recorded `code_region` in 433 of them (65%).

**A known limit, not changed here.** EquiRouter (default ON) re-orders a pick by
the cross-region Borda ranking whenever two or more region slices carry a
preference. That behaviour is pinned by
`tests/unit/test_lane_router_py.py::test_preferred_lane_equirouter_overrides_scalar_dominance`.
So the segment changes the routed lane only when fewer than two region slices
carry a preference, or under `MO_EQUIROUTER=0`. Otherwise it changes the
candidate set and the margin.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/dispatch/routing.py`

- **New module constant and three functions**, placed directly above
  `_registry_known_lanes`:

  ```
  REGION_NODE_TYPES = ("implementer",)
  ```

  - **`region_of_path(path: str) -> str`.**
    1. Set `p = str(path or "").strip().replace("\\", "/")`.
    2. Strip leading `"./"` repeatedly.
    3. Return `""` when `p` is empty, starts with `"../"`, or starts with `"/"`.
    4. Otherwise return `p.split("/", 1)[0]` when `"/"` is in `p`, else
       `"(root)"`.

    This is the same normalisation as `infer_trace_code_region` in
    `mini_ork/cli/execute.py`. Do not import it from there: `routing.py` must not
    import `mini_ork.cli`.
  - **`scope_region(run_dir: str | None) -> str`.**
    1. Return `""` when `run_dir` is falsy.
    2. Import `mini_ork.memory.preferences` locally and take
       `paths = preferences.scope_paths(run_dir)`; any exception returns `""`.
    3. Map each path through `region_of_path`, dropping `""`.
    4. Return `""` when nothing remains. Otherwise return the most frequent
       region; on a tie, the one that appears first.
  - **`route_segment(node_type: str, run_dir: str | None) -> str`.**
    - Return `"default"` when `os.environ.get("MO_ROUTE_REGION", "1") == "0"`.
    - Return `"default"` when `node_type not in REGION_NODE_TYPES`.
    - Otherwise return `scope_region(run_dir) or "default"`.
- **`learning_governed_lane`.**
  - Add a keyword-only parameter `run_dir: str | None = None` after
    `task_class`.
  - Change the `decide` call to:

    ```
    decision_service.decide(node_type, task_class, objective_domain,
                            segment=route_segment(node_type, run_dir), db=db)
    ```

  - Nothing else in the function changes.
- **`RoutingContext`.** Add a last field `run_dir: str | None = None`.
- **`_policy_learning_governed`.** Pass `run_dir=ctx.run_dir` to
  `learning_governed_lane`. The pinned branch is unchanged.
- **`policy_route_lane`.**
  - Add a keyword-only parameter `run_dir: str | None = None` after
    `task_class`.
  - Build the context as
    `RoutingContext(node_type, current_lane, root=root, task_class=task_class, run_dir=run_dir)`.
  - Nothing else changes: the dry-run path, provenance, and the `[route]`
    stderr line all stay as they are.

### 2. `mini_ork/cli/execute_handlers.py`, `dispatch_node`

Add `run_dir=run_dir` to the existing `policy_route_lane(...)` call at the top of
`dispatch_node`. `run_dir` is `dispatch_node`'s own parameter, and the run
profile lives there. `MINI_ORK_RUN_DIR` is published only later in the function,
so it cannot be used. Nothing else changes.

### 3. `docs/LEARNING-LOOP-LIFECYCLE.md`

Add exactly this row to the §5 "Defaults" table, directly below the
`MO_LEARNING_MIN_SAMPLES` row:

```
| `MO_ROUTE_REGION` | `1` | an unpinned implementer's route consults the region slice first (region = most common top-level dir of `run_profile.json` scope) | `MO_ROUTE_REGION=0` |
```

### Out of scope, on purpose

- **`mini_ork/cli/lane_preflight.py`** keeps calling `policy_route_lane` without
  `run_dir`. Its test stub `_identity_route` in
  `tests/unit/test_lane_preflight_llm_nodes.py` takes no `run_dir`, and preflight
  is a lane-health prediction. A dispatch-time region pick that preflight did not
  probe still goes through the lane fuse and the fallback chain.
- **`mini_ork/lane_router.py`**: the EquiRouter override stays as it is.

### The default, and why

`MO_ROUTE_REGION` defaults to ON. It changes live routing but no reward or
advantage computation. It is proven safe by the tests below:

- The region slice decides only when at least one lane clears
  `MO_LEARNING_MIN_SAMPLES` there; otherwise `_pick_lane` falls through to
  domain exactly as today.
- Non-implementer nodes and pinned nodes are untouched.
- `MO_ROUTE_REGION=0` restores `segment="default"`.

There is no secret or remote surface, so both tiers run the same code path.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/routing.py`
- `mini_ork/cli/execute_handlers.py`
- `docs/LEARNING-LOOP-LIFECYCLE.md`
- `tests/unit/test_route_region_segment.py` (new)

Do NOT modify any other file. In particular, do not touch
`mini_ork/cli/lane_preflight.py`, `mini_ork/lane_router.py` or
`mini_ork/steering/decision_service.py`.

## Tests (`tests/unit/test_route_region_segment.py`: hermetic temp DB, no network, no LLM)

### Fixtures

- **`env_db`.** Copy it from `tests/unit/test_decision_service_py.py`, adding
  `cwd=str(REPO)` to its `subprocess.run`. It runs `db/init.sh` and sets:
  - `MINI_ORK_ROOT`, `MINI_ORK_HOME`, `MINI_ORK_DB` and `MO_STORE_DB`;
  - `EPSILON=0`;
  - `MO_LEARNING_MIN_SAMPLES=1`;
  - `MO_LEARNING_HALFLIFE_DAYS=0`.

  In addition, `delenv`: `MO_ROUTE_REGION`, `MO_ROUTING_POLICY`,
  `MINI_ORK_OBJECTIVE_DOMAIN`, `MO_OBJECTIVE_DOMAIN` and `MO_EQUIROUTER`. The
  router's other knobs stay at their defaults.
- **`_seed(db, lane, rv, region, n)`.**
  - It calls `trace_store.trace_write` `n` times with:
    - `task_class "code-fix"`;
    - `status "success"`;
    - `agent_version_id lane`;
    - `objective_domain "code-delivery"`;
    - `verifier_output {"node_type": "implementer"}`;
    - `reward_value rv`, `reward_anchor 0.5`, `reward_direction "higher_is_better"`.
  - When `region` is not None it then runs
    `UPDATE execution_traces SET code_region=? WHERE trace_id=?`.
    `trace_write` does not write `code_region`.
- **`_one_region(db)`.** It seeds a single region slice that disagrees with the
  domain:
  - `_seed(db, "laneB", 1.0, "mini_ork", 3)`;
  - `_seed(db, "laneA", 0.0, "mini_ork", 3)`;
  - `_seed(db, "laneA", 1.0, None, 6)`;
  - `_seed(db, "laneB", 0.0, None, 6)`;
  - then `lane_router.recompute_advantages(db=db)`.

  The resulting domain order is laneA over laneB. The only region slice,
  `mini_ork`, orders laneB over laneA. With one preferring region slice,
  EquiRouter does not engage.
- **`_profile(tmp_path, entries, name="run")`.** It writes
  `<tmp>/<name>/run_profile.json` as `{"scope_allow": entries}` and returns the
  dir as a str.
- **`MINI`.** The entries
  ```
  ["`mini_ork/dispatch/routing.py`", "`mini_ork/cli/execute_handlers.py`",
   "`tests/unit/test_x.py` (new)", "Do NOT modify `docs/a.md` or any other file."]
  ```
  `preferences.scope_paths` yields the first three paths, so the region is
  `mini_ork`.

### Tests

- **`region_of_path`** (parametrize):
  - `mini_ork/cli/execute.py` → `mini_ork`
  - `./docs/a.md` → `docs`
  - `README.md` → `(root)`
  - `""` → `""`
  - `../x/y.py` → `""`
  - `/abs/p.py` → `""`
  - `recipes/*/workflow.yaml` → `recipes`
  - `a\b\c.py` (backslashes) → `a`
- **`scope_region`:**
  - the `MINI` profile → `mini_ork`;
  - entries `` ["`docs/a.md`", "`tests/b.py`"] `` → `docs` (the tie goes to the
    first);
  - `[]` → `""`;
  - a missing dir → `""`;
  - `None` → `""`.
- **`route_segment`**, with the `MINI` dir:
  - `("implementer", dir)` → `mini_ork`;
  - `("reviewer", dir)` → `default`;
  - `("implementer", None)` → `default`;
  - an empty-scope dir → `default`;
  - with `MO_ROUTE_REGION=0`, `("implementer", dir)` → `default`.
- **The region slice decides** (`env_db`, `_one_region`):
  - `routing.learning_governed_lane("implementer", "implementer", task_class="code-fix", run_dir=<MINI dir>)`
    returns `"laneB"`;
  - the same call without `run_dir` returns `"laneA"`;
  - with `MO_ROUTE_REGION=0` and `run_dir` it returns `"laneA"`.
- **An unknown region falls through.** With a profile `` ["`tests/a.py`"] ``
  (region `tests`, no region rows), the same call returns `"laneA"`.
- **`policy_route_lane` threads `run_dir`.**
  - `routing.policy_route_lane("implementer", "implementer", task_class="code-fix", run_dir=<MINI dir>)`
    returns `"laneB"`, and `routing.last_route_provenance()["route_source"]` is
    `"learned"`.
  - A pinned call `policy_route_lane("implementer", "codex_lens", ..., run_dir=<MINI dir>)`
    returns `"codex_lens"` with `route_source == "pinned"`.
- **The context default.** `routing.RoutingContext("implementer", "implementer").run_dir is None`.
- **`dispatch_node` passes its `run_dir`.** Use the harness pattern of the `gate`
  fixture in `tests/unit/test_eco6_k5_cost_pause.py`:
  - `monkeypatch.setattr(execute, "_capture_pre_impl_baseline", lambda *a, **k: None)`;
  - `monkeypatch.setattr(execute, "_intervention_decision", lambda *a, **k: ("proceed", {}))`;
  - a handler registered with
    `monkeypatch.setitem(execute_handlers.NODE_HANDLER_REGISTRY, "rsi1probe", lambda ctx: (0, "done"))`;
  - delete `MINI_ORK_RUN_DIR`, `MINI_ORK_RUN_ID`, `MINI_ORK_HOME` and
    `MO_TARGET_CWD`;
  - register a capture policy with
    `monkeypatch.setitem(routing.POLICY_REGISTRY, "rsi-t1-capture", capture)`,
    where `capture(ctx)` appends `ctx.run_dir` and returns `ctx.current_lane`,
    and set `MO_ROUTING_POLICY=rsi-t1-capture`.

  Then, inside `run_context_scope({})` (from `mini_ork.context`), call:

  ```
  execute_handlers.dispatch_node(["rsi1probe-n1", "rsi1probe", "probe", "", "", "", "", ""],
      root=str(root), run_dir=str(run_dir), plan_path="", task_class="framework-edit",
      db=str(tmp_path / "missing.db"), run_id="run-rsi1", dispatch_fn=lambda *a: (0, "ok"))
  ```

  The captured list equals `[str(run_dir)]`.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_route_region_segment.py tests/unit/test_decision_service_py.py tests/unit/test_routing_registry_py.py tests/unit/test_lane_router_py.py tests/unit/test_lane_router_zscore_py.py`
- `pytest -q -p no:asyncio tests/unit/test_route_screening.py tests/unit/test_route_screening_slice_stats.py tests/unit/test_entrorouter_py.py tests/unit/test_lane_preflight_gate_py.py tests/unit/test_lane_preflight_llm_nodes.py`
- `pytest -q -p no:asyncio tests/unit/test_agents_local_overlay_py.py tests/unit/test_lane_chain_resolution.py tests/unit/test_lane_fuse_and_chain_py.py tests/unit/test_eco6_k5_cost_pause.py tests/unit/test_acp_race.py tests/unit/test_mini_ork_execute_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_route_region_segment.py --junitxml="${MINI_ORK_RUN_DIR}/route-region-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/route-region-junit.xml`

## Success criteria

- All three verification commands pass, with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/dispatch/routing.py mini_ork/cli/execute_handlers.py`
  is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes. Do not change routing for pinned
or non-implementer nodes, the EquiRouter override, `decide()`, or lane preflight.
