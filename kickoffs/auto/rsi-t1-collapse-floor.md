# RSI Tier-1 #12a: a diversity / pass@k floor for collapse detection, fed by run outcomes that exist today

## Goal

The R2K review (G01-T04, `internal-docs/research/2026-09-29-rsi-technique-review-2000.md` §3)
asks for a collapse guard on entropy and pass@k: "rollout entropy stays above
floor; pass@k does not flatten". `collapse_detector.detect` has only the
score-vs-anchor divergence. The status doc
(`docs/plans/2026-10-24-rsi-technique-status.md`, matrix F) lists "Diversity /
pass@k floor" as NOT BUILT.

Data sources, checked on the primary `.mini-ork/state.db` on 2026-10-27:

| Source | Rows | Usable for a floor? |
|---|---:|---|
| `collapse_history` | 17 (15 `rsi_cycle`, 2 `code_fix`) | No. Every `rsi_cycle` row has `anchor = 1.0` and `directives = 0`, so divergence cannot fire and directive diversity carries no information |
| `apply_attempts` | 43, newest 2026-10-13 | No. The apply gate is OFF |
| node-level `execution_traces` success | 4,520 lane-attributed node traces | No. Implementer success is 0.95–1.0 in almost every 20-group window (saturated) |
| `task_runs` terminal outcomes | **1,631** (909 published / 722 failed). 6 task classes have ≥ 40 | **Yes** |
| implementer lanes per run (`tr-implementer-%`, `agent_version_id`) | 370 of 435 `framework_edit` and 249 of 278 `code_fix` terminal runs | **Yes** |

The floor compares the last `window` terminal runs of a task class with the
`window` runs before them:

- **pass@1** is the published fraction.
- **pass@k** groups runs by `kickoff_path`, the same task re-run, and counts a
  task solved if any of its first k runs published.
- **Lane diversity** is the number of distinct implementer lanes.

It recommends `halt` only when pass@k fell by more than `drop` **and** the lane
set narrowed to at most `max_lanes`. Both conditions are needed: many recipes
pin one lane by design (code_fix is single-lane in 10 of 13 windows), so
diversity alone cannot be a floor.

**Backtest** of exactly this rule (window 20, k 3, drop 0.2, max_lanes 1),
evaluated at every terminal run of every task class:

- 1,131 evaluations: 975 `none`, 154 `watch`, **2 `halt`**.
- Both halts are `code_fix` on 2026-07-16. The lanes narrowed from
  {kimi, worker} to {worker}, and pass@k fell from 0.47 to 0.12.
- On today's data every task class reads `none`.

This kickoff builds the pure floor, a read-only DB reader and an operator CLI.
It changes no halt behaviour. Breaker wiring is `rsi-t1-collapse-floor-breaker`
(the breaker is a frozen path). `mini_ork/learning/collapse_precursor.py` (H4)
is untouched: it needs per-generation anchor counts that nothing writes.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/collapse_detector.py`

`detect`, `summarize` and every existing constant stay **byte-for-byte
unchanged**. The module stays free of DB, network and model code.

**New constants:** `FLOOR_WINDOW = 20`, `FLOOR_K = 3`, `FLOOR_DROP = 0.2`,
`FLOOR_MAX_LANES = 1`.

**New helper `_floor_half(rows: Sequence[dict], k: int) -> dict`.** For rows
`r` (`n = len(rows)`, always ≥ 1 when called):

- `pass_at_1 = _round(sum(1 for r in rows if bool(r.get("passed"))) / n)`.
- **Groups**, in first-appearance order. The key for the row at index `i` is:
  - `str(r.get("task_key") or "")` when non-empty;
  - else `str(r.get("run_id") or "")` when non-empty;
  - else `f"#{i}"`.

  Each group is the list of its rows' `bool(passed)` values, in order.
- `pass_at_k = _round(sum(1 for g in groups if any(g[:k])) / len(groups))`.
- `lanes` is the sorted distinct non-empty values of `str(x).strip()` for every
  `x` in every row's `r.get("lanes") or []`. `lane_count = len(lanes)`.
- **Returns** `{"n": n, "pass_at_1": ..., "pass_at_k": ..., "lane_count": ..., "lanes": [...]}`.

**New `floor(runs: Sequence[dict], *, window: int = FLOOR_WINDOW, k: int = FLOOR_K, drop: float = FLOOR_DROP, max_lanes: int = FLOOR_MAX_LANES) -> dict`.**

- **Input.** `runs` is oldest-first, each row
  `{"run_id": str, "passed": bool, "task_key": str, "lanes": Sequence[str]}`.
- **Coercions:** `window = max(1, int(window))`, `k = max(1, int(k))`,
  `drop = float(drop)`, `max_lanes = int(max_lanes)`, `n = len(runs)`.
- **Return keys, always:** `n, window, k, drop, max_lanes, prior, recent, pass_at_k_drop, fell, narrowed, breach, recommendation, reason`.
- **Insufficient history** (`n < 2 * window`):
  - `prior`, `recent` and `pass_at_k_drop` are `None`;
  - `fell`, `narrowed` and `breach` are `False`;
  - `recommendation` is `"none"`;
  - `reason` is `f"insufficient history (n < {2 * window})"`.
- **Otherwise:**
  - `prior = _floor_half(runs[n - 2*window : n - window], k)`;
  - `recent = _floor_half(runs[n - window:], k)`;
  - `pass_at_k_drop = _round(prior["pass_at_k"] - recent["pass_at_k"])`;
  - `fell = pass_at_k_drop > drop` (strict);
  - `narrowed = recent["lane_count"] < prior["lane_count"] and recent["lane_count"] <= max_lanes`;
  - `breach = fell and narrowed`.
- **Recommendation and reason:**

  | Case | `recommendation` | `reason` |
  |---|---|---|
  | breach | `"halt"` | `"pass@k fell while lane diversity narrowed (diversity floor breached)"` |
  | `fell` only | `"watch"` | `"pass@k fell but lane diversity did not narrow"` |
  | `narrowed` only | `"watch"` | `"lane diversity narrowed but pass@k did not fall"` |
  | neither | `"none"` | `"no floor breach"` |

### 2. `mini_ork/learning/collapse_floor.py` (new)

This is a read-only DB reader. It never writes or commits.

**`resolve_window() -> int`**

- Returns `int(os.environ["MO_CB_FLOOR_WINDOW"])` when that parses to an
  int ≥ 1.
- Otherwise returns `collapse_detector.FLOOR_WINDOW`.

**`floor_runs(con: sqlite3.Connection, task_class: str, *, limit: int) -> list[dict]`**

- **Runs.** Run
  `SELECT id, status, kickoff_path FROM task_runs WHERE task_class=? AND status IN ('published','failed') ORDER BY created_at DESC, id DESC LIMIT ?`,
  then reverse to oldest-first. Index positionally, so both tuples and
  `sqlite3.Row` work.
- **Lanes.** For those ids, run
  `SELECT run_id, agent_version_id FROM execution_traces WHERE run_id IN (<placeholders>) AND trace_id LIKE 'tr-implementer-%' AND TRIM(COALESCE(agent_version_id,'')) <> ''`.
- **Returns**
  `[{"run_id": str(id), "passed": status == "published", "task_key": kickoff_path or "", "lanes": sorted({lane.strip() ...})}]`.
- **Errors.** No runs gives `[]`. A `sqlite3.OperationalError` (missing table
  or column) gives `[]`.

**`evaluate(con: sqlite3.Connection, task_class: str, *, window: int | None = None) -> dict`**

- `w = max(1, int(window if window is not None else resolve_window()))`.
- `report = collapse_detector.floor(floor_runs(con, task_class, limit=2 * w), window=w)`.
- Set `report["task_class"] = task_class` and return it.

### 3. `mini_ork/cli/collapse.py`

The existing `<history.json>` mode is unchanged. The command always exits 0.

**New mode:** `mini-ork collapse-check --floor <task_class> [--json]`.

- **Argument parsing** iterates by index.
  - `--floor` takes the next argv element as the task class.
  - `--floor` as the last element: write `--floor requires a task class\n` to
    stderr, print the usage, return 0.
  - A positional path together with `--floor`: the existing
    `unexpected argument: …` stderr line, then the usage, then return 0.
- **DB path.** `os.environ.get("MINI_ORK_DB")`, else
  `os.path.join(os.environ.get("MINI_ORK_HOME") or ".mini-ork", "state.db")`.
  - Missing file: write `collapse-check: no state.db at <path>\n` to stderr and
    return 0.
  - Otherwise open it read-only:
    `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`.
- **Output.** `report = collapse_floor.evaluate(con, task_class)`, then close.
  - With `--json`, print `json.dumps(report, indent=2, sort_keys=True)`.
  - Without it, print `json.dumps(report, sort_keys=True)`.
  - Either way, follow with `\n`.
- **`_usage()`.**
  - Directly after its first line, add the line
    `       mini-ork collapse-check --floor <task_class> [--json]`.
  - Under `Options:`, add
    `  --floor <task_class>  Report the diversity / pass@k floor from state.db (read-only)`.

## Files in scope (touch ONLY these)

- `mini_ork/learning/collapse_detector.py`
- `mini_ork/learning/collapse_floor.py` (new)
- `mini_ork/cli/collapse.py`
- `tests/unit/test_collapse_floor.py` (new)

Do NOT modify any other file. In particular, do not touch
`mini_ork/recovery/circuit_breaker.py` (it is frozen) or
`mini_ork/learning/collapse_precursor.py`.

## Tests (`tests/unit/test_collapse_floor.py`: hermetic, temp sqlite, no network)

**Helpers:**

- `_rows(passes: list[bool], lanes: list[list[str]], keys: list[str] | None = None)`
  builds floor rows, with `run_id = f"r{i}"` and `task_key = keys[i]` or
  `f"k{i}"`.
- **DB seeding.** Copy the `db` fixture from
  `tests/unit/test_circuit_breaker_collapse.py` (`mig.init_db`). Insert task
  runs with
  `INSERT INTO task_runs (id, task_class, recipe, kickoff_path, workflow_version, created_at, updated_at, status) VALUES (?,?,?,?,'latest',?,?,?)`.
  Use explicit status and increasing integer `created_at`; do not reuse that
  file's `_seed_task_runs`, which hard-codes status `classified`. Insert lanes
  with
  `INSERT INTO execution_traces (trace_id, run_id, task_class, agent_version_id, status, verifier_output, created_at) VALUES (?,?,?,?, 'success', '{"node_type": "implementer"}', ?)`,
  using trace ids like `f"tr-implementer-implementer-{i:08x}"`.

**Pure `floor()`:**

- **Insufficient history.** 39 rows with the default window give `"none"`,
  `prior is None` and reason `"insufficient history (n < 40)"`.
- **Halt.**
  - Prior 20 rows: the first 10 passed, lanes alternate `["a"]`/`["b"]`.
  - Recent 20 rows: the first 2 passed, every lane `["a"]`.
  - Every row has a distinct `task_key`.
  - Result: `prior["pass_at_k"] == 0.5`, `recent["pass_at_k"] == 0.1`,
    `pass_at_k_drop == 0.4`, `narrowed is True`, `breach is True`,
    `recommendation == "halt"`, and the exact halt reason.
- **Watch, pass fell only.** Same as above but recent lanes alternate
  `["a"]`/`["b"]`. Result: `"watch"` and
  `"pass@k fell but lane diversity did not narrow"`.
- **Watch, narrowing only.** Equal pass rates, lanes 2 → 1. Result: `"watch"`
  and `"lane diversity narrowed but pass@k did not fall"`.
- **Strict boundary.** Prior pass@k 0.5 and recent 0.3 give
  `pass_at_k_drop == 0.2` and `fell is False`.
- **No narrowing without diversity.**
  - A single lane in both halves (1 → 1) with a big drop gives
    `narrowed is False` and `"watch"`.
  - No lanes at all (0 → 0) gives `narrowed is False`.
- **The pass@k estimator,** with `window=2`. The prior is two passing rows.
  The recent half is two rows with the same `task_key` `"t"`, failing then
  passing.
  - `recent["pass_at_1"] == 0.5`.
  - With `k=3`, `recent["pass_at_k"] == 1.0`.
  - With `k=1`, `recent["pass_at_k"] == 0.0`.
- **Group key fallback.** With empty `task_key` the key falls back to
  `run_id`. With both empty, each row is its own group.
- **`detect` is unchanged:** `detect([])["reason"] == "insufficient history (n < 4)"`.

**DB reader:**

- **`floor_runs`** returns only `published`/`failed` rows (not `superseded` or
  `executing`):
  - oldest-first;
  - the newest `limit` rows only;
  - `passed` mapped from status;
  - lanes only from `tr-implementer-%` traces (a `tr-reviewer-reviewer-…` row's
    lane is ignored), with blank lanes ignored.
- **Missing tables.** `floor_runs` on `sqlite3.connect(":memory:")` returns
  `[]`.
- **`evaluate`.**
  - With `MO_CB_FLOOR_WINDOW=2` it uses the last 4 runs and sets
    `report["task_class"]`.
  - With the variable set to `abc`, it falls back to 20.
  - On a DB seeded with the halt pattern above (40 runs, class `code_fix`), it
    returns `"halt"`.

**CLI:**

- **JSON mode.** With `MINI_ORK_DB` set to the seeded DB,
  `collapse.main(["--floor", "code_fix", "--json"])` returns 0, and
  `json.loads` of stdout has `recommendation == "halt"`.
- **Read-only.** The DB file's bytes are unchanged afterwards.
- **Missing DB.** Returns 0 and stderr contains `no state.db at`.
- **Missing task class.** `["--floor"]` returns 0 and stderr contains
  `--floor requires a task class`.
- **Existing mode.** `collapse.main([<path to a 4-row history.json>, "--json"])`
  still prints a `detect` report.

## Verification commands

- `pytest -q tests/unit/test_collapse_floor.py tests/unit/test_collapse_detector_py.py tests/unit/test_collapse_precursor_py.py`
- `pytest -q tests/unit/test_circuit_breaker_collapse.py tests/unit/test_collapse_history_writer.py tests/unit/test_collapse_history_shared_py.py tests/unit/test_rsi_learn_py.py tests/unit/test_native_dispatch_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_collapse_floor.py --junitxml="${MINI_ORK_RUN_DIR}/collapse-floor-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/collapse-floor-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/collapse_detector.py mini_ork/learning/collapse_floor.py mini_ork/cli/collapse.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
