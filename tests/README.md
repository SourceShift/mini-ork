# mini-ork Tests

## Quick Start

```bash
# From the repo root:
python3 -m pytest -q
```

The suite is pure Python (the bash test layers were removed in the
2026-07 bash-removal — see `docs/plans/2026-07-26-bash-removal-plan.md`).
`pyproject.toml` sets `pythonpath=["."]` and `testpaths=["tests"]`.

## Layout

| Path | Contents |
|---|---|
| `tests/unit/` | Unit tests for `mini_ork/*` modules (`*_py.py`) |
| `tests/` (root) | Integration-style suites (dispatch, recovery, web smoke, run artifacts, …) |
| `tests/integration/` | Cross-component integration tests |
| `tests/e2e/` | End-to-end flows |
| `tests/optimize/` | GEPA/optimizer tests |
| `tests/live/` | Live-provider tests (skipped without creds) |

## Conventions

- DB-backed tests bootstrap the schema with
  `mini_ork.stores.migrate.init_db(db_path, root=repo)` — no subprocess,
  ~1s. Use a `tmp_path` db per test.
- Tests that drive CLIs prefer `python -m <module>` subprocesses or
  in-process `main(argv)` calls.
- The `conftest.py` autouse fixture snapshots/restores `os.environ` and
  cwd — never leak either (a past leak of `.mini-ork/state.db` into the
  suite cwd poisoned later tests).
- Long-history: parity tests that compared the Python port against the
  retired bash twins were converted to plain unit tests in Phase 3
  (2026-07-27); expectations are semantic, not recorded goldens.

## CI

`.github/workflows/ci.yml` runs ruff (blocking + advisory), pytest on
3.11/3.12 (sharded `unit-a`/`unit-b`/`rest`), the UI typecheck, and the
web smoke suite. The merge green-gate is `python3 -m pytest -q` (scope
per task with `MINI_ORK_TEST_CMD`).
