# P4a — Leaked-sandbox reaper + `sandbox-gc` subcommand

## Deliverable (ONE)
A backend-agnostic reaper that finds and removes leaked mini-ork sandbox
instances (Docker containers today, microVM best-effort), plus a
`mini-ork sandbox-gc` subcommand to run it on demand. This is the P4 safety
net: even when a run crashes before its `finally` teardown, no sandbox lives
forever. Additive — nothing on the hot path changes; the reaper only ever
touches instances that carry mini-ork's own labels/name-prefixes.

Design context: `internal-docs/research/2026-07-30-sandbox-shared-drive-design.md`
§5 "P4 — Lifecycle + scale" (the heartbeat/GC-sweep clause). This slice ships
the GC mechanism; per-node `finally` teardown, run-level drive create/destroy,
and an automatic heartbeat are separate follow-up slices (P4b+).

## Seams to reuse (already in the tree — do not rebuild)
- Docker sandboxes are labeled `mo.sandbox=1` — the constant `_RUN_LABEL` in
  `mini_ork/runtime/backends/docker.py:31` ("sweepable: `docker ps -qf
  label=mo.sandbox=1`"). Containers are named `mo-ws-<12hex>`.
- microVM sandboxes are named `mo-mvm-<12hex>` (`backends/microvm.py`).
- Subcommand pattern: `register_subcommand` / `_native_module_handler` in
  `mini_ork/cli/main.py`; a dashed name is registered explicitly exactly like
  `recipe-eval` (`main.py:694`) → `_native_module_handler("mini_ork.cli.…")`.
- Env convention: `MO_*` (design doc §5 env surface).

## Files in scope (ABSOLUTE paths — edit only these)
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/runtime/sandbox_reaper.py   (NEW)
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/cli/sandbox_gc.py           (NEW)
- /Volumes/docker-ssd/ps/mini-ork/tests/unit/test_sandbox_reaper.py    (NEW)
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/cli/main.py   (EDIT: ONE registry
  line for `sandbox-gc`, mirroring the `recipe-eval` line — nothing else)

Do NOT edit `backends/docker.py`, `backends/microvm.py`, `sandbox.py`, or the
execute/dispatch hot path in this slice.

## Reaping policy (the one real decision — TTL, not run-liveness)
"Leaked" = a labeled sandbox older than a max age. TTL keeps the reaper
decoupled from state.db and from the run lifecycle, and cannot mistakenly kill
work that a *different* live run started. Default max age is generous so a
long node is never reaped mid-flight; `--max-age` overrides. (A future slice
may add run-id-scoped reaping once docker.py carries the run-id in its label.)

## Acceptance criteria (concrete)
1. `mini_ork/runtime/sandbox_reaper.py` defines:
   - `reap_docker(*, max_age_s: int, dry_run: bool = False, now: float | None
     = None) -> list[str]` — lists containers with label `mo.sandbox=1` and
     their created-at (via `docker ps --filter label=mo.sandbox=1 --format
     '{{.ID}} {{.CreatedAt}}'` or `docker inspect`), removes (`docker rm -f`)
     only those older than `max_age_s`, and returns the reaped container ids.
     `dry_run=True` selects the same set but removes nothing. Missing docker CLI
     or a dead daemon → returns `[]` (never raises).
   - `reap_microvm(...)` with the same signature/return shape — best-effort;
     if the microsandbox SDK is unavailable it returns `[]`.
   - `reap_sandboxes(*, backend: str = "all", max_age_s: int, dry_run: bool =
     False) -> dict[str, list[str]]` — dispatches to the requested backend(s)
     (`all` = docker + microvm) and returns `{backend: [reaped ids]}`.
   - `DEFAULT_MAX_AGE_S` constant (a generous default, e.g. 6h) and honor
     `MO_SANDBOX_MAX_AGE` from the env as the fallback when a caller passes none.
   - The docker CLI is invoked through a single small runner indirection (e.g. a
     module-level `_run(argv) -> subprocess.CompletedProcess`) so tests can fake
     it WITHOUT a real daemon.
2. `mini_ork/cli/sandbox_gc.py` defines `main(rest: list[str], root: str) ->
   int` that argparses `--backend {all,docker,microvm}` (default `all`),
   `--max-age <seconds>` (default `DEFAULT_MAX_AGE_S`/`MO_SANDBOX_MAX_AGE`), and
   `--dry-run`; calls `reap_sandboxes`; prints a one-line-per-backend summary
   (`<backend>: reaped N (<ids>)` or `dry-run: would reap N`); returns 0.
3. `mini_ork/cli/main.py`: ONE added line in `_build_default_registry`
   registering `registry["sandbox-gc"] = _native_module_handler(
   "mini_ork.cli.sandbox_gc")`, mirroring the `recipe-eval` precedent.
4. `tests/unit/test_sandbox_reaper.py` (fake the docker CLI via monkeypatch on
   the module runner — NO real docker) covers:
   - lists only `mo.sandbox=1`-labeled containers;
   - reaps only those older than `max_age_s`, leaving younger ones alive;
   - returns the reaped ids; the correct `docker rm -f` calls were issued;
   - `dry_run=True` issues NO `rm` calls but returns the same candidate set;
   - missing docker CLI / non-zero `docker info` → `reap_docker` returns `[]`
     and raises nothing;
   - `reap_sandboxes(backend="docker")` shape is `{"docker": [...]}`;
   - `reap_microvm` returns `[]` when the SDK is absent (guarded import).
5. `mini-ork sandbox-gc --dry-run` runs end-to-end (exit 0) with no docker
   present (prints a zero/skip summary, never errors).

## Constraints
- Pure stdlib + existing `mini_ork` modules; the microsandbox SDK import is
  guarded (absent → best-effort `[]`), no new hard dependency.
- Fail-open and side-effect-free at import time (no `docker`/SDK calls on
  import; registering nothing but pure functions).
- Only ever act on instances matching mini-ork's own label/name-prefix — never
  a broad `docker rm`. Match the module/docstring style of `sandbox.py` and
  `backends/docker.py`.
