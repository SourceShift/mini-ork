# Preview a run before launching it: `POST /api/v1/runs/plan`

## Goal

The wizard lets a user pick a recipe and a set of controllable features, then
launches. Today there is no way to *preview* that decision without actually
launching — the UI cannot show which nodes will run, on which lanes, or what the
selected features do to the projected cost.

Add `POST /api/v1/runs/plan`: it compiles the recipe's workflow and reports the
node DAG plus a feature preview, without creating a run, writing a row, or
dispatching anything. It is a pure read-only computation.

## Acceptance

- AC1 — `POST /api/v1/runs/plan` with body `{"recipe": "<name>", "features":
  {"<id>": "<value>", ...}}` returns HTTP 200 and a JSON object with:
  - `recipe`: the echoed recipe name.
  - `nodes`: a list of `{"name", "type", "lane"}`, one per node, **in
    `topological_order`** (lane = the node's `model_lane`).
  - `topological_order`: the compiled workflow's topological order
    (`CompiledWorkflow.topological_order`).
  - `features`: `{"env": {...}, "blocked": [...], "cost_multiplier": <float>}`.
- AC2 — the workflow is compiled with `compile_workflow()` from
  `mini_ork.workflow.compiler`, resolved against the recipe directory using the
  same path resolution the CLI already uses. Do NOT hardcode a path and do NOT
  re-implement the compiler.
- AC3 — `features.env` is `mini_ork.features.resolve_selection(features,
  accept_premium=<body flag, default False>)`; `features.blocked` is the list of
  premium feature ids that were stripped (derive it from
  `premium_unselected(...)` / the `gate_env` contract — the same ids the launch
  path reports); `features.cost_multiplier` is
  `mini_ork.features.effective_multiplier(features)`. These are the EXISTING
  registry functions — call them, do not re-derive the math.
- AC4 — an unknown recipe returns HTTP 400 with a clear `detail` (the compile
  error message), never a 500. The route must not create a `task_runs` row and
  must not call `control.launch_run`.
- AC5 — the route is a plain read-only preview: no `Depends(auth.require_token)`,
  matching the sibling read routes in `mini_ork/web/routes/fleet.py`. Document
  that choice in the route docstring (no side effects ⇒ no privileged-write auth).
- AC6 — a new unit test asserts 200 + the AC1 shape through FastAPI's
  `TestClient`, asserts `cost_multiplier` rises when a premium feature is
  selected, and asserts the 400 on an unknown recipe.

## Files in scope

- `mini_ork/web/routes/runs.py` — add the `POST /plan` route beside the existing
  `POST ""` launch route on the same `router` (`prefix="/api/v1/runs"`).
- `tests/unit/test_runs_plan.py` (new)

## Out of scope

- Do NOT change `mini_ork/features/registry.py`, `mini_ork/workflow/compiler.py`,
  `mini_ork/web/control.py`, or the existing `POST /api/v1/runs` launch route.
- Do NOT add a pricing table or any new cost-estimation math — the multiplier is
  the cost proxy that already exists.
- Do NOT touch `mini_ork/web/app.py` — the `runs` router is already registered.
- Do NOT add auth, or a persisted plan, or any dispatch.

## Verification command

`python3 -m pytest tests/unit/test_runs_plan.py -q`
