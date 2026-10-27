# RSI Tier-1 #5a: a directive is only authored for the layer that failed

## Goal

`mini_ork/recovery/diagnose.py` assigns every diagnosed failed run a typed
`layer` (`LAYERS`: change, model, provider, environment, engine, kickoff, user,
infra, unclassified) and a `routes_to`. Directive authoring ignores it.
Reflection (`mini_ork/learning/reflection_pipeline.reflection_extract_gradients`)
stores every gradient the extractor returns through `gradient_extractor.store`.
Those gradients are then injected into prompts (`context_assembler`), promoted to
themes (`themes`), and applied to prompt files (`cli/apply.py`).

Every gradient target family (`workflow.*`, `agent.*.prompt`, `verifier.*`,
`harness.*`, plus `cross_class:` variants) edits the **recipe harness**. A
harness directive cannot fix a failure the diagnosis puts in another layer: a bad
kickoff, a dead lane, a missing credential, an engine bug. On the primary home,
106 of the 319 gradients from diagnosed failed runs come from `kickoff`- or
`engine`-layer runs.

(`mini_ork/learning/writeback.py`, which the TOP-20 note names, is the GRPO reward
writer. It authors no directive. The authoring choke point is the
reflection→store loop.)

Refuse a harness directive whose run was diagnosed in a non-harness layer, record
where the fix belongs instead, and leave every other gradient alone.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/layer_routing.py` (new)

Imports: `json`, `os`, `sqlite3`, `time`, `from pathlib import Path`,
`from typing import Any, Mapping`,
`from mini_ork.recovery.diagnose import DIAGNOSIS_FILE, LAYER_CHANGE, LAYER_UNCLASSIFIED`,
`from mini_ork.run_status import UNSUCCESSFUL`.

Constants:

- `EDIT_LAYER_HARNESS = "harness"`
- `HARNESS_TARGET_PREFIXES = ("workflow.", "agent.", "verifier.", "harness.", "prompts/")`
- `CROSS_CLASS_PREFIX = "cross_class:"`
- `HARNESS_FIXABLE_LAYERS = frozenset({LAYER_CHANGE})`
- `PASS_THROUGH_LAYERS = frozenset({"", LAYER_UNCLASSIFIED})`
- `REFUSALS_FILE = "layer-refusals.jsonl"`

Functions (all module-level, none raises):

- `mode() -> str`: `v = os.environ.get("MO_LAYER_ROUTING", "").strip().lower()`;
  return `"off"` when `v == "off"`, `"on"` when `v == "on"`, else `"shadow"`.
  An unset or unknown value is `"shadow"`: the rule records what it would refuse
  (106 of 319 diagnosed-failure gradients on 2026-10-27) without refusing, until
  an operator reviews the refusal log and sets `MO_LAYER_ROUTING=on`.
- `edit_layer(target: str) -> str`: strip one leading `CROSS_CLASS_PREFIX`, then
  return `EDIT_LAYER_HARNESS` when the rest starts with any of
  `HARNESS_TARGET_PREFIXES`, else `""`.
- `route(diagnosis: Mapping[str, Any] | None, target: str) -> dict`: returns
  `{"admit": bool, "edit_layer": str, "diagnosed_layer": str, "reroute": str, "reason": str}`.
  `edit = edit_layer(target)`. When `diagnosis` is a Mapping,
  `layer = str(diagnosis.get("layer") or "")`, else `layer = ""`. `reroute` is
  `""` unless refused. First match wins:
  1. `edit == ""` → admit, reason `"untyped target"`;
  2. `diagnosis` is not a Mapping → admit, reason `"no diagnosis"`;
  3. `layer in PASS_THROUGH_LAYERS` → admit, reason `"unclassified diagnosis"`;
  4. `layer in HARNESS_FIXABLE_LAYERS` → admit, reason `"layer match"`;
  5. otherwise refuse (`admit: False`), with
     `reroute = str(diagnosis.get("routes_to") or "")` and reason
     `f"layer mismatch: a {edit} directive cannot fix a {layer}-layer failure (routes_to={reroute or 'none'})"`.
     A layer outside `diagnose.LAYERS` also lands here, which fails closed.
  `diagnosed_layer` is `layer` in every case.
- `trace_run_id(db: str, trace_id: str) -> str`:
  `SELECT run_id FROM execution_traces WHERE trace_id=?`. Returns
  `str(run_id)`, or `""` when the row is missing, `run_id` is NULL, or on any
  sqlite error.
- `run_diagnosis(home: str, run_id: str, db: str) -> dict | None`:
  - `None` when `run_id` is empty.
  - `SELECT status FROM task_runs WHERE id=?`. `None` when the row is missing or
    on any sqlite error.
  - `None` when that status is not in `UNSUCCESSFUL`. A published, superseded or
    still-live run's diagnosis is absent or stale.
  - Read `Path(home) / "runs" / run_id / DIAGNOSIS_FILE` as JSON. Return the dict,
    or `None` when the file is missing, unreadable, or not a JSON object.
- `check_gradient(gradient: Mapping[str, Any], *, trace_id: str, db: str, home: str) -> dict`:
  `target = str(gradient.get("target") or "")`; `run_id = trace_run_id(db, trace_id)`;
  `dx = run_diagnosis(home, run_id, db) if run_id else None`; return
  `route(dx, target)` plus the keys `"trace_id": trace_id`, `"run_id": run_id`,
  `"target": target`.
- `record_refusal(home: str, run_id: str, refusal: Mapping[str, Any]) -> bool`:
  `False` when `run_id` is empty or `Path(home) / "runs" / run_id` is not a
  directory. Otherwise append `json.dumps(dict(refusal), sort_keys=True) + "\n"`
  to `<run_dir>/REFUSALS_FILE` and return `True`. `False` on `OSError`.

### 2. `mini_ork/learning/reflection_pipeline.py`

- **New helper** `_layer_check(gradient_json: str, trace_id: str) -> dict | None`:
  - `db = os.environ.get("MINI_ORK_DB", "")`; `None` when empty.
  - `home = os.environ.get("MINI_ORK_HOME") or str(Path(db).parent)`.
  - Parse `gradient_json`; `None` unless it is a JSON object.
  - Return `layer_routing.check_gradient(obj, trace_id=trace_id, db=db, home=home)`
    with `home` added to the returned dict under the key `"_home"`.
  - Return `None` on any exception. The gate fails OPEN: a bug in it must never
    stop reflection.
- **In `reflection_extract_gradients`**:
  - Compute `lr_mode = layer_routing.mode()` once, before the trace loop.
    Initialise `layer_refused = 0`.
  - In the inner `for gradient in grads:` loop, after the `if not gradient: continue`
    and before `_gradient_store(gradient)`: when `lr_mode != "off"`, call
    `verdict = _layer_check(gradient, tid)`. When `verdict` is not `None` and
    `verdict["admit"]` is false:
    - `layer_refused += 1`;
    - `home = verdict.pop("_home")`; `record = dict(verdict, mode=lr_mode, ts=int(time.time()))`;
      `layer_routing.record_refusal(home, verdict["run_id"], record)`;
    - print
      `f"reflection_extract_gradients: layer-refused {verdict['target']} from {tid}: {verdict['reason']}"`
      to stderr;
    - when `lr_mode == "on"`: `continue`. The gradient is not stored, not echoed,
      and not counted in `extracted`. When `lr_mode == "shadow"` it falls through
      and is stored exactly as today.
  - Before the final `extracted N gradients since S` stderr line, when
    `layer_refused` is non-zero, print
    `f"reflection_extract_gradients: {layer_refused} gradient(s) layer-refused (MO_LAYER_ROUTING={lr_mode})"`
    to stderr. The final line itself is unchanged.
- Import `layer_routing` as `from mini_ork.learning import layer_routing` at
  module top. Add `from pathlib import Path` if it is not already imported.
- Nothing else changes in this module.

## Files in scope (touch ONLY these)

- `mini_ork/learning/layer_routing.py` (new)
- `mini_ork/learning/reflection_pipeline.py`
- `tests/unit/test_layer_routing.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_layer_routing.py`: hermetic temp home + sqlite, no network, no LLM)

**Fixture home.** `home = tmp_path / "home"`, `db = home / "state.db"`, with:

```
CREATE TABLE execution_traces (trace_id TEXT PRIMARY KEY, run_id TEXT, task_class TEXT, created_at TEXT);
CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT);
```

Seed trace `('tr-1', 'run-1', 'code_fix', '2026-10-27 10:00:00')` and a
`task_runs` row `('run-1', <status>)`. Write
`home/runs/run-1/diagnosis.json` as
`{"layer": <layer>, "routes_to": <route>, "rule_id": "R0", "source": "rule"}`.
Set `MINI_ORK_DB=str(db)` and `MINI_ORK_HOME=str(home)` with monkeypatch, and
delete `MO_LAYER_ROUTING` unless a test sets it.

Unit tests:

- **`edit_layer`.** `"agent.reviewer.prompt"`, `"workflow.edge.verifier_to_reviewer"`,
  `"verifier.obs_smoke"`, `"workflow.recipe.code-fix"`, `"harness.code-fix.implementer"`,
  `"prompts/reviewer.md"` and `"cross_class:workflow.edge.x"` all map to
  `"harness"`. `"verifier@v1"` and `""` map to `""`.
- **`route`.**
  - `route(None, "agent.x.prompt")` admits with reason `"no diagnosis"`.
  - `{"layer": "change"}` admits with `"layer match"`.
  - `{"layer": "unclassified"}` and `{"layer": ""}` admit with
    `"unclassified diagnosis"`.
  - `route({"layer": "kickoff", "routes_to": "kickoff_repair"}, "agent.x.prompt")
    == {"admit": False, "edit_layer": "harness", "diagnosed_layer": "kickoff",
    "reroute": "kickoff_repair", "reason": "layer mismatch: a harness directive
    cannot fix a kickoff-layer failure (routes_to=kickoff_repair)"}`.
  - Each of `model`, `provider`, `environment`, `engine`, `user`, `infra` and
    `"bogus"` is refused for a harness target.
  - A refused layer with no `routes_to` gives `reroute == ""` and a reason ending
    in `(routes_to=none)`.
  - `route({"layer": "model"}, "verifier@v1")` admits with `"untyped target"`.
- **`mode`.** Unset → `"shadow"`; `"off"`, `"OFF "` → `"off"`; `"on"`, `" ON"` → `"on"`;
  `"shadow"` → `"shadow"`; `"nonsense"` → `"shadow"`.
- **`run_diagnosis`.** It returns the dict for a `failed` run with
  `diagnosis.json`. It returns `None` for: a `published` run with the same file,
  a `failed` run with no file, a run whose file holds a JSON list, an unknown
  `run_id`, and a DB with no `task_runs` table.
- **`record_refusal`.** It appends one sorted-key JSON line per call (two calls →
  two lines). It returns `False` for an empty `run_id` or a missing run dir.

Integration tests through `reflection_pipeline.reflection_extract_gradients(0)`.
Monkeypatch these on the `mini_ork.learning.reflection_pipeline` module:
`_gradient_ensure_table` to a no-op; `_gradient_extract` to
`lambda tid: [json.dumps({"target": "agent.reviewer.prompt", "signal": "s", "suggested_change": "c", "evidence": tid, "confidence": 0.7})]`;
`_gradient_store` to a function appending its argument to a list `stored`. There is
no `gradient_records` table, so the watermark check reads "not processed".

- **Refused when on.** With `MO_LAYER_ROUTING=on`, status `failed` and layer `kickoff`, `stored == []`;
  `home/runs/run-1/layer-refusals.jsonl` has exactly one line whose JSON has
  `admit == False`, `diagnosed_layer == "kickoff"`, `reroute == "kickoff_repair"`,
  `target == "agent.reviewer.prompt"`, `trace_id == "tr-1"`, `run_id == "run-1"`,
  `mode == "on"` and an integer `ts`, and no `_home` key. stderr contains
  `layer-refused agent.reviewer.prompt from tr-1` and
  `1 gradient(s) layer-refused (MO_LAYER_ROUTING=on)`.
- **Shadow stores and records.** `MO_LAYER_ROUTING=shadow` gives `len(stored) == 1`
  and one refusal line with `mode == "shadow"`.
- **Unset is shadow.** With `MO_LAYER_ROUTING` deleted, the same seed gives
  `len(stored) == 1` and one refusal line with `mode == "shadow"`.
- **Off does nothing.** `MO_LAYER_ROUTING=off` gives `len(stored) == 1` and no
  refusal file.
- **A matching layer passes.** Layer `change` gives `len(stored) == 1` and no
  refusal file.
- **A published run passes.** Status `published` with layer `kickoff` gives
  `len(stored) == 1`: a stale diagnosis is ignored.
- **No diagnosis passes.** With no `diagnosis.json`, `len(stored) == 1`.

## Verification commands

- `pytest -q tests/unit/test_layer_routing.py tests/unit/test_reflection_pipeline_py.py`
- `pytest -q tests/unit/test_gradient_extractor_py.py tests/unit/test_diagnose.py tests/unit/test_cross_epic_gradient_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_layer_routing.py --junitxml="${MINI_ORK_RUN_DIR}/layer-routing-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/layer-routing-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/layer_routing.py mini_ork/learning/reflection_pipeline.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
