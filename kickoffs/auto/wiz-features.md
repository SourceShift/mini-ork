# Expose the controllable-feature registry over HTTP: `GET /api/v1/features`

## Goal

The controllable-feature registry (`mini_ork/features/registry.py`) is the
wizard's single source of truth for which run knobs a user may control and what
each costs. Today it is reachable only through the `mini-ork features` CLI, the
`/wizard` ACP command, and `mini_ork/web/control.py`. Add a read-only HTTP route
so the web and IDE surfaces can list features without shelling out.

Add `GET /api/v1/features` returning the registry's machine form.

## Acceptance

- AC1 — `GET /api/v1/features` returns HTTP 200 and a JSON object with exactly the
  shape of `mini_ork.features.catalogue_json()`: `{"premium_threshold": <float>,
  "features": [...]}`. Call `catalogue_json()` directly; do not re-derive the list.
- AC2 — the route accepts an optional `recipe` query parameter and forwards it to
  `catalogue_json(recipe)`, so `?recipe=code-fix` narrows via `features_for()`.
- AC3 — the route is a plain unauthenticated read, matching the sibling read
  routes in `mini_ork/web/routes/fleet.py` (`GET /api/v1/health`,
  `GET /api/v1/runs/active`). Do NOT add `Depends(auth.require_token)`.
- AC4 — the router is registered in `mini_ork/web/app.py` the same way the
  `fleet` router is (import the module in the existing `from .routes import (...)`
  list and add an `app.include_router(features.router)` call).
- AC5 — a new unit test asserts 200 plus the `catalogue_json()` shape through
  FastAPI's `TestClient`, and that `?recipe=` narrows the feature list.

## Files in scope

- `mini_ork/web/routes/features.py` (new)
- `mini_ork/web/app.py`
- `tests/unit/test_web_features_route.py` (new)

## Out of scope

- Do NOT change `mini_ork/features/registry.py` or any feature definition.
- Do NOT add premium gating, auth, or a write path — this is a read-only listing.
- Do NOT touch `mini_ork/cli/main.py`, `mini_ork/cli/features.py`, or
  `tests/unit/test_native_dispatch_py.py`.
- Do NOT edit any existing route module.

## Verification command

`python3 -m pytest tests/unit/test_web_features_route.py -q`
