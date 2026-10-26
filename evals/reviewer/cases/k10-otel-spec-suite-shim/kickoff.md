# ECOSYSTEM §6 · K10: export every finished run to Langfuse/OTLP (two tiers), and delete the dead buffer

## Goal

`mini_ork/otel_export.py::export(task_run_id, db_path)` already builds the
run → agent → llm_call span tree from `state.db` and POSTs it as OTLP/JSON. It
is the only way to get run data into an external tracing tool, but nothing calls
it: it is reachable only by hand (`python -m mini_ork.otel_export <run_id>`).
Meanwhile:

- `docs/architecture/otel-langfuse.md` claims "Live in-process span emission is
  wired: `MO_OTEL=1` makes `cli/execute.py` buffer…" — nothing does;
  `mini_ork/observability/otel.py` (the buffer) has no caller and
  `lib/mo_otel.sh` no longer exists.
- `mini_ork/observability/langfuse_score_mapper.py` has no caller.
- The IDE setup page shows a green "exporting to {endpoint}" whenever the
  Langfuse keys are set, though nothing exports.
- `_deliver` reads keys straight from `os.environ`, bypassing the vault.

Make the export live after every run, resolve the keys through the vault chain
(simple tier: environment / owner-only `secrets.local.sh`; secure tier: a
sops / command / openbao backend — the same `vault_environment()` every other
credential uses), fail loudly on a half-configured key pair, and delete the
dead pieces.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/otel_export.py`

1. Add `from mini_ork.dispatch.vault import vault_environment` (module-level
   name, so tests can monkeypatch it).
2. New `_credentials(environment=None) -> tuple[str, str, str] | None`:
   - `env = environment if environment is not None else vault_environment()`;
     any exception building it → return `None` and print one stderr line
     `otel_export: vault unavailable — no export (<ExceptionName>)` (never the
     message text, which may carry a path with a secret name only — fine, but
     keep it to the class name).
   - `pk = env.resolve("LANGFUSE_PUBLIC_KEY")`, `sk = env.resolve("LANGFUSE_SECRET_KEY")`,
     `endpoint = env.resolve("LANGFUSE_OTLP_ENDPOINT") or DEFAULT_ENDPOINT`.
   - Neither key set → return `None` (export is simply off).
   - Exactly one set → print
     `otel_export: only one of LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY is set — no export (set both, or neither)`
     to stderr and return `None` (fail closed, visibly).
   - Both set → return `(pk, sk, endpoint)`.
3. `_deliver(payload, dry_run, *, environment=None) -> bool`: dry-run behaviour
   unchanged; otherwise get credentials from `_credentials(environment)`
   (`None` → return `False`) and POST exactly as today (same headers, timeout
   15, same stderr success line). Never print a key.
4. New `export_after_run(task_run_id: str, db_path: str, *, environment=None) -> bool`:
   returns `False` without building anything when `MO_OTEL_EXPORT` is `"0"`;
   otherwise `_credentials(environment)`; `None` → `False`; else
   `build_payload(db_path, task_run_id)` and `_deliver(...)`. Catch **every**
   exception (network, SQL, payload) → print
   `otel_export: export of <task_run_id> failed: <ExceptionName>` to stderr and
   return `False`. It must never raise.
5. Delete `build_payload_from_jsonl`, `export_jsonl`, the `--from-jsonl`
   argument and its branch in `main`; `main` again requires `task_run_id`.
6. `export(...)` keeps its signature and behaviour (manual path).

### 2. `mini_ork/cli/execute.py`

At the very end of `_post_run_learning` (after the auto-apply sweep), add a
best-effort step:

```python
    # 5. OTel / Langfuse export of this run (off unless both Langfuse keys
    #    resolve through the vault chain; MO_OTEL_EXPORT=0 opts out).
    try:
        from mini_ork import otel_export  # noqa: PLC0415
        otel_export.export_after_run(run_id, db)
    except Exception:  # noqa: BLE001 — telemetry must never fail a run
        pass
```

Add a line `5. OTel / Langfuse export …` to the function docstring. Change
nothing else in `execute.py`.

### 3. Delete

- `mini_ork/observability/otel.py` and `tests/unit/test_mo_otel_py.py`
- `mini_ork/observability/langfuse_score_mapper.py` and
  `tests/unit/test_langfuse_score_mapper_py.py`
- In `tests/test_otel_export.py`: the JSONL tests and the
  `build_payload_from_jsonl` / `export_jsonl` imports only.

### 4. `mini_ork/ide_pages/setup.py`

The OTel/Langfuse status line: resolve through
`otel_export._credentials()` (catching any exception as "not configured")
instead of `os.environ`. Both keys → `S.ok("OTel / Langfuse", f"each finished run is exported to {endpoint}")`;
one key → a yellow/red status `"only one of LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY is set — nothing is exported"`
(use whichever warning/failure helper `S` provides; check the module);
neither → today's "not configured" dot. Update
`tests/unit/test_ide_pages_setup.py` for the new wording only.

### 5. Docs

- `docs/architecture/otel-langfuse.md`: rewrite the Status block — after every
  run, `_post_run_learning` exports the run's span tree when both Langfuse keys
  resolve through the vault (simple tier: environment / `config/secrets.local.sh`;
  secure tier: a vault backend, see `config/vault.example.yaml`);
  `MO_OTEL_EXPORT=0` opts out; manual: `python -m mini_ork.otel_export <run_id> [--dry-run]`.
  Remove every mention of `MO_OTEL=1`, the JSONL buffer, `lib/mo_otel.sh` and
  `--from-jsonl` (around lines 12 and 94).
- `docs/migration/parity-ports.md`: the `langfuse_score_mapper.py` row →
  `removed 2026-10 (no production caller)`.

## Files in scope (touch ONLY these)

- `mini_ork/otel_export.py`
- `mini_ork/cli/execute.py` (the `_post_run_learning` step and docstring only)
- `mini_ork/observability/otel.py`, `mini_ork/observability/langfuse_score_mapper.py` (delete)
- `tests/unit/test_mo_otel_py.py`, `tests/unit/test_langfuse_score_mapper_py.py` (delete)
- `tests/test_otel_export.py` (remove JSONL tests/imports only)
- `mini_ork/ide_pages/setup.py`, `tests/unit/test_ide_pages_setup.py` (status wording only)
- `docs/architecture/otel-langfuse.md`, `docs/migration/parity-ports.md`
- `tests/unit/test_eco6_k10_otel_live.py` (new)

## Tests (`tests/unit/test_eco6_k10_otel_live.py`, hermetic, no network)

Use a stub environment object with `resolve(key)` (None when missing).

- **Both keys → one POST with the run's root span.** Seed a tmp `state.db`
  (`mig.init_db`) with one `task_runs` row `run-x`; monkeypatch
  `otel_export._deliver` to record `(payload, kwargs)` and return `True`;
  `export_after_run("run-x", db, environment=stub(both keys))` returns `True`,
  `_deliver` called once, and the payload's root span carries `run-x` (find the
  attribute the builder uses; assert on it).
- **Neither key → no build, no POST.** Monkeypatch `build_payload` to raise if
  called; returns `False`.
- **One key → no POST and a stderr line** containing `only one of`; returns
  `False`; the key value never appears in stderr.
- **`MO_OTEL_EXPORT=0` → `False`** without calling `_credentials`.
- **Never raises.** `_deliver` raising `OSError` → `False`, stderr contains
  `export of run-x failed: OSError`.
- **Post-run hook calls it.** Monkeypatch `otel_export.export_after_run` with a
  recorder and call `mini_ork.cli.execute._post_run_learning(db, str(tmp_path), "run-x", "k10", fail_count=0)`
  (stub/monkeypatch the earlier side channels if they need a fuller DB) → the
  recorder got `("run-x", db)` exactly once; a recorder that raises does not
  propagate.
- **Dead pieces are gone.** `importlib.util.find_spec` is `None` for
  `mini_ork.observability.otel` and `mini_ork.observability.langfuse_score_mapper`;
  `otel_export` has no `export_jsonl` / `build_payload_from_jsonl`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k10_otel_live.py tests/test_otel_export.py tests/unit/test_ide_pages_setup.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k10_otel_live.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k10-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k10-junit.xml`

## Success criteria

- The verification command passes; pre-existing tests unchanged except the
  removed JSONL cases and the setup-page wording.
- `ruff check --select F,E9 mini_ork/otel_export.py mini_ork/cli/execute.py mini_ork/ide_pages/setup.py` is clean.
- With no Langfuse keys configured, a run's behaviour and output are unchanged.
