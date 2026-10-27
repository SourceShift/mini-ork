# RSI Tier-1 #5b: the apply loop never promotes a gradient from the wrong layer

## Goal

`rsi-t1-layer-routing-1-authoring` stops NEW layer-mismatched gradients at
reflection. The 15k gradients already in `gradient_records` were stored before
that gate existed. `mini_ork/cli/apply.py` `pick_candidate` can still pick one of
them (priority 3, `source_kind == "gradient_records"`) and promote it into a recipe
prompt. Apply the same routing decision at the apply gate, so the stored backlog
cannot be promoted. The decision is recorded as a `rejected` `apply_attempts` row,
which `_FAILED_SOURCE_EXCLUSION` then excludes from future picks for that target.

Depends on `rsi-t1-layer-routing-1-authoring` (`mini_ork/learning/layer_routing.py`
must exist) and lands after `rsi-t1-kernel-hole-2-apply-guard` (same file).

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/cli/apply.py`

- **New helper** `_layer_route(gradient_id: str, target_name: str, db: str | None) -> dict | None`:
  - Returns `None` when `layer_routing.mode() == "off"` or `gradient_id` is empty.
  - `path = _db_path(db)`; `SELECT evidence FROM gradient_records WHERE gradient_id=?`.
    Returns `None` when the row is missing or on any sqlite error.
  - `home = os.environ.get("MINI_ORK_HOME") or os.path.dirname(path)`.
  - Returns `layer_routing.check_gradient({"target": target_name}, trace_id=str(evidence), db=path, home=home)`
    with the key `"_home": home` added.
  - Returns `None` on any exception (fails open, like the authoring gate).
  - Import `layer_routing` locally inside the helper:
    `from mini_ork.learning import layer_routing`.
- **In `apply_run`**, directly after the `_previously_failed(...)` block (the one
  that returns with the GRAO edit-memory rationale) and before
  `# 2. Materialize the candidate.`:
  - when `source_kind == "gradient_records"`: `lr = _layer_route(source_id, target_name, db)`;
  - when `lr` is not `None` and `lr["admit"]` is false:
    - `home = lr.pop("_home")`; `mode = layer_routing.mode()`;
    - `layer_routing.record_refusal(home, lr["run_id"], dict(lr, mode=mode, ts=int(time.time()), boundary="apply"))`;
    - when `mode == "on"`:
      `aid = attempt_record(task_class, target_kind, target_name, source_kind, source_id, "", "", "", "", "", "", "rejected", "layer-mismatch: " + lr["reason"], dry_flag, apply_enabled, db=db)`;
      write `aid + "\n"` and then
      `f'{{"decision":"rejected","task_class":"{task_class}","target":"{target_name}","source_id":"{source_id}","reason":"layer_mismatch"}}\n'`
      to stdout; `return 0`;
    - when `mode == "shadow"`: write
      `f"[layer-routing] shadow: {lr['reason']}\n"` to stderr and continue the
      run unchanged.
  - `pattern_records` and `emergent_patterns` sources are not routed. They carry
    no single trace, and nothing here changes for them.

## Files in scope (touch ONLY these)

- `mini_ork/cli/apply.py`
- `tests/unit/test_apply_layer_routing.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_apply_layer_routing.py`: hermetic temp home + sqlite, no network)

**Setup.** `import mini_ork.cli.apply as ap`. Scrub `_APPLY_ENV` (copy the list
from `tests/unit/test_cli_apply_py.py`) plus `MO_LAYER_ROUTING`. Use the `db`
fixture shape of `tests/unit/test_cli_apply_py.py`: copy its `FIXTURE_DDL` and
`db` fixture, and reset `ap._SCHEMA_INIT`. The DB must live at
`home / "state.db"` with `MINI_ORK_HOME=str(home)`. Add to the fixture DB:

```
CREATE TABLE IF NOT EXISTS execution_traces (trace_id TEXT PRIMARY KEY, run_id TEXT, task_class TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, status TEXT);
```

Seed a `gradient_records` row `gradient_id='gr-1'`, `target='agent.reviewer.prompt'`,
`task_class='reviewer'`, `evidence='tr-1'`, `confidence=0.8`, non-empty
`signal`/`suggested_change`, `created_at=1`. Seed trace `('tr-1','run-1','reviewer','2026-10-27 10:00:00')`,
`task_runs ('run-1','failed')`, and `home/runs/run-1/diagnosis.json` =
`{"layer": "model", "routes_to": "swap_lane"}`. Set `MO_APPLY_SCORER=mock`.

Tests (call `ap.apply_run("reviewer", "prompt_file", "agent.reviewer.prompt", "", db=db)`):

- **Refused when on.** Returns `0`. The newest `apply_attempts` row has
  `decision='rejected'`, `source_kind='gradient_records'`, `source_id='gr-1'`, and
  a `rationale` starting with
  `"layer-mismatch: layer mismatch: a harness directive cannot fix a model-layer failure"`.
  No `workflow_candidates` row was created. `home/runs/run-1/layer-refusals.jsonl`
  has one line with `boundary == "apply"` and `mode == "on"`.
- **The rejection sticks.** A second `apply_run` with the same arguments does not
  create a `workflow_candidates` row either.
- **Shadow continues.** With `MO_LAYER_ROUTING=shadow`, no `rejected` row carries
  a `layer-mismatch:` rationale, and the refusal line has `mode == "shadow"`.
- **Off and matching layers continue.** With `MO_LAYER_ROUTING=off`, or with the
  diagnosis layer `change`, no row carries a `layer-mismatch:` rationale and no
  refusal file is written.
- **A published run is not routed.** With `task_runs` status `published`, no
  `layer-mismatch:` row is written.

## Verification commands

- `pytest -q tests/unit/test_apply_layer_routing.py tests/unit/test_layer_routing.py tests/unit/test_cli_apply_py.py`
- `pytest -q tests/unit/test_apply_harness_targets.py tests/unit/test_apply_significance.py tests/unit/test_eco6_k7_apply_hygiene.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_apply_layer_routing.py --junitxml="${MINI_ORK_RUN_DIR}/apply-layer-routing-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/apply-layer-routing-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/apply.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
