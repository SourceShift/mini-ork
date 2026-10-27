# RSI Tier-1 #6: mine contrastive lane pairs into the existing pattern → lesson → verify path

## Goal

MGM (Mendel Gödel Machine, arXiv 2608.07645; survey
`internal-docs/research/2026-09-12-rsi-agent-survey-miniork-adoption.md` item 4)
shows an editor a failing attempt next to a *different lineage's successful
attempt on the same task*. The survey calls this the "cheapest high-signal
source in the whole survey — zero new infrastructure". mini-ork already stores
the lane per node attempt: `execution_traces.agent_version_id` is the dispatch
lane, and `trace_id` is `tr-<node_type>-<node_id>-<8hex>`
(`mini_ork/cli/execute.py` `_make_trace_fn`). Nothing mines the contrast.

A **contrastive pair** here is one run and one node where lane W has a
`success` trace and a different lane L has a `failure` trace. Reviewer verdict
failures and moved-base failures are excluded, because they describe the
artifact or the base, not the lane. Measured on the primary
`.mini-ork/state.db` on 2026-10-27 with exactly the rules below:

- **18 contrastive pairs** (23 before the exclusions), in **12 distinct**
  `(task_class, node, winner, loser)` keys.
- **7 quality pairs** in 2 keys (loser reasons `no_op`, `impl_diff_unusable`).
  The other **11 are infra** (`error`, `timeout`, `cost_limit`).
- **1 key clears the 3-independent-run floor**: `framework_edit` reviewer,
  where `glm` succeeded and `opus_lens` returned `no_op`, in 6 runs. Only 1
  pair falls in the last 7 days.
- For context only: the same traces give 115 cross-run lane pairs in 23
  `(task_class, node)` groups. Different runs are different tasks, so this
  kickoff does not mine them.

Write each quality key that clears the floor as a `pattern_records` row. From
there, the **existing** path takes over and nothing new can reach a prompt:

1. `pattern_induction.induce_pending` authors `lesson_text`. It applies the
   provenance and independence guardrails in `consolidate` and the fail-closed
   `lesson_conflict` check.
2. `reflection_suggest_promotions` / `reflection_persist_suggestions` create
   an `emergent_patterns` row with status `proposed`.
3. `reflection_verify_patterns` approves only when strength ≥
   `MO_EMERGENT_VERIFY_MIN_STRENGTH` (3), evidence spans ≥
   `_INDEPENDENT_EVIDENCE_FLOOR` (3) distinct runs, and a lesson exists.

That is the only-verified rule: the `mini_ork/context_assembler.py` module
docstring says "Only verified learnings reach prompts (user rule 2026-10-07):
approved+lessoned patterns". Prompt files are guarded separately by
`mini_ork/learning/prompt_directives.py` and `FABRICATING_SCORERS`
(`mini_ork/cli/apply.py`). This miner writes neither prompt files nor
`emergent_patterns`.

Infra keys are **not** written. They are returned as report-only routing
evidence. The lane router already penalises those failures through
`reward_from_status`, and a lane-health note in a prompt is something the
prompt cannot act on.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/contrastive_lanes.py` (new)

**Module constants:**

- `EXCLUDED_REASONS = frozenset({"verdict_revise", "verdict_fail", "impl_moved_base"})`
- `INFRA_REASONS = frozenset({"error", "timeout", "cost_limit"})`
- `MIN_RUNS_FLOOR = 3`
- `_TRACE_RE = re.compile(r"^tr-(?P<node_type>[a-z_]+)-(?P<node_id>.+)-[0-9a-f]{8}$")`

**`enabled() -> bool`** returns `os.environ.get("MO_MGM_MINER", "1") != "0"`.

**`find_pairs(con: sqlite3.Connection, *, since_iso: str | None = None) -> list[dict]`**

- **Query.** Run
  `SELECT trace_id, run_id, task_class, agent_version_id, status, verifier_output FROM execution_traces WHERE status IN ('success','failure')`.
  When `since_iso` is truthy, add ` AND created_at >= ?`. Always end with
  ` ORDER BY created_at, trace_id`. Index the rows positionally. A
  `sqlite3.OperationalError` returns `[]`.
- **Skip a row** when any of these holds:
  - `verifier_output` does not parse with `json.loads` to a dict;
  - `node_type = vo.get("node_type")` is not a str;
  - `_TRACE_RE` does not match `trace_id`, or the match's `node_type` differs
    from `vo["node_type"]`;
  - `lane = (agent_version_id or "").strip()` is empty;
  - `run = str(run_id or "").strip()` is empty;
  - `status == "failure"` and `finish_reason = str(vo.get("finish_reason") or "")`
    is in `EXCLUDED_REASONS`.
- **Grouping.** Group the remaining rows by
  `(run, task_class, node_type, node_id)`.
- **Pairs.** In each group, for every lane W with ≥1 success and every lane
  L ≠ W with ≥1 failure, emit one pair dict:
  - `run_id`, `task_class`, `node_type`, `node_id`, `winner` (W), `loser` (L);
  - `winner_trace_ids`: the sorted success trace ids of W in the group;
  - `loser_trace_ids`: the sorted failure trace ids of L in the group;
  - `loser_reasons`: the sorted distinct `finish_reason` strings of those
    failures (`""` is kept as `""`);
  - `kind`: `"quality"` if any loser reason is non-empty and not in
    `INFRA_REASONS`, else `"infra"`.

  A lane that both succeeded and failed in one group is never paired with
  itself.
- **Order.** Return the pairs sorted by
  `(task_class, node_type, node_id, winner, loser, run_id)`.

**`summarize(pairs: list[dict]) -> list[dict]`**

- One dict per key `(task_class, node_type, node_id, winner, loser)`, with:
  - those five keys;
  - `run_ids`: sorted distinct run ids;
  - `n_runs`: `len(run_ids)`;
  - `kind`: `"quality"` if any pair of the key is quality, else `"infra"`;
  - `reasons`: the sorted union of `loser_reasons`;
  - `trace_ids`: for each run in `run_ids` (ascending), that run's
    `winner_trace_ids` then its `loser_trace_ids`, deduplicated keeping the
    first occurrence.
- Sorted by the five-key tuple.

**`pattern_id_for(task_class: str, node_type: str, node_id: str, winner: str, loser: str) -> str`**

- Returns `"pat-mgm-" + hashlib.sha256(f"{task_class}|{node_type}|{node_id}|{winner}|{loser}".encode()).hexdigest()[:12]`.

**`describe(key: dict) -> str`**

- Returns, truncated with `[:480]`:

  ```
  f"cluster: task_class={tc} contrastive node={nt}/{nid} winner={w} loser={l} (runs={n}): in each run the {w} attempt succeeded where the {l} attempt at the same node failed ({', '.join(r or 'unknown' for r in reasons)}); the lesson is what the succeeding attempt did that the failing one did not"
  ```

  Here `tc, nt, nid, w, l, n, reasons` are the key's `task_class`,
  `node_type`, `node_id`, `winner`, `loser`, `n_runs` and `reasons`.
- **Two contracts depend on this string:**
  - it must start with `cluster: task_class=<tc> `, because
    `mini_ork.context_assembler.pattern_task_class` scopes on it;
  - it must contain neither `status=failure` nor `status=vacuous`, because
    `mini_ork/orchestration/watchdog.py` `_fetch_failing_patterns` matches
    those substrings and can request a run stop.

**`mine_into_patterns(*, db_path: str | None = None, window: str | None = None, min_runs: int | str = 3) -> dict`**

- **Report.** Start from
  `{"enabled": enabled(), "pairs": 0, "keys": 0, "quality_keys": 0, "written": 0, "held": 0, "routing": []}`.
  Return it unchanged when the miner is disabled.
- **DB resolution.** Use the exact expression in
  `pattern_induction.induce_pending`:
  `db_path or context_env("MINI_ORK_DB", "") or os.path.join(context_env("MINI_ORK_HOME", "") or os.path.join(os.getcwd(), ".mini-ork"), "state.db")`.
  When it is not an existing file, return the report. Never create the file.
- **Window.**
  - `None` or `""` means all history (`since_iso=None`).
  - Otherwise match `^(\d+)([dh])$` and set
    `since_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(int(time.time()) - n * (86400 if unit == "d" else 3600)))`.
  - An unparseable window also means all history.
- **Floor.** `floor = max(MIN_RUNS_FLOOR, int(min_runs))`, and
  `MIN_RUNS_FLOOR` on `TypeError`/`ValueError`. The floor may be raised, never
  lowered.
- **Counting.** Compute `pairs = find_pairs(con, since_iso=...)` and
  `keys = summarize(pairs)`. Set `pairs`, `keys` and `quality_keys` (the number
  of quality keys).
- **Per key:**
  - **infra:** append
    `{"task_class", "node_type", "node_id", "winner", "loser", "n_runs", "reasons"}`
    to `routing`. Nothing is written.
  - **quality with `n_runs < floor`:** `held += 1`.
  - **quality with `n_runs >= floor`:** upsert the row and `written += 1`.
- **Upsert** of `pid = pattern_id_for(...)`:
  - **If the row exists:** merge its stored `evidence_trace_ids` (bad JSON
    counts as `[]`) with the key's `trace_ids` via
    `list(dict.fromkeys(old + new))[:200]`. Then run
    `UPDATE pattern_records SET frequency=?, evidence_trace_ids=?, description=?, last_seen=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE pattern_id=?`
    with `frequency = n_runs` and `description = describe(key)`. Never touch
    `lesson_text`, `status`, `output_type` or `promoted_to`.
  - **Otherwise:** run
    `INSERT INTO pattern_records (pattern_id, description, evidence_trace_ids, frequency, first_seen, last_seen, output_type) VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'best_practice_rule')`
    with `evidence = json.dumps(trace_ids[:200])`.
  - The status defaults to `observed`.
  - The output type is **not** `prompt_change`. `apply.pick_candidate` uses
    `prompt_change` rows as raw directive text.
- **Finish.** Commit and close.
- **Errors.** Any `sqlite3.Error` sets
  `report["error"] = f"{type(exc).__name__}: {exc}"` and returns the report.
  The function never raises.

### 2. `mini_ork/cli/reflect.py`

Insert a new side-channel immediately after the `MO_PATTERN_MINER` block,
whose last line writes `  [pattern_miner] wrote …`, and before the
`# ── side-channel: pattern induction` comment. Placing it there means
induction authors the new rows in the same pass.

```
mgm_report = None
if os.environ.get("MO_MGM_MINER", "1") != "0":
    try:
        from mini_ork.learning import contrastive_lanes
        mgm_report = contrastive_lanes.mine_into_patterns(
            db_path=db_path,
            window=os.environ.get("MO_MGM_MINER_WINDOW") or None,
            min_runs=os.environ.get("MO_MGM_MINER_MIN_RUNS", "3"),
        )
    except Exception as exc:  # a side-channel must never crash reflect
        sys.stderr.write(f"  [mgm_miner] skipped: {exc}\n")
        mgm_report = None
if mgm_report is not None:
    sys.stdout.write(
        f"  [mgm_miner] {mgm_report.get('pairs', 0)} contrastive pair(s), "
        f"wrote {mgm_report.get('written', 0)} pattern_records row(s), "
        f"{len(mgm_report.get('routing') or [])} routing-only key(s)\n"
    )
```

Nothing else in `reflect.py` changes.

## Files in scope (touch ONLY these)

- `mini_ork/learning/contrastive_lanes.py` (new)
- `mini_ork/cli/reflect.py`
- `tests/unit/test_contrastive_lanes.py` (new)

Do NOT modify any other file. In particular, do not touch
`mini_ork/learning/pattern_induction.py`, `mini_ork/learning/reflection_pipeline.py`,
`mini_ork/stores/pattern_store.py` or `mini_ork/context_assembler.py`.

## Tests (`tests/unit/test_contrastive_lanes.py`: hermetic temp sqlite, no network, no model)

**Fixture and seeding:**

- **`db` fixture.** Copy the `db` fixture from `tests/unit/test_pattern_store_py.py`.
  It calls `mini_ork.stores.migrate.init_db(db=dbp, root=<repo root>)` and gives
  the full migrated schema. Foreign keys are off, so a text `run_id` like
  `"r1"` inserts fine.
- **`_trace(...)` helper.** Seed traces with
  `INSERT INTO execution_traces (trace_id, run_id, task_class, agent_version_id, status, verifier_output, created_at) VALUES (?,?,?,?,?,?,?)`.
  - `verifier_output = json.dumps({"node_type": nt, "finish_reason": fr})`.
  - `created_at` is relative to now, never hard-coded:
    `(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")`.
  - Trace ids look like `tr-reviewer-reviewer-0000000a`: unique, with an
    8-hex suffix.

**Pair finding:**

- **One pair.** In run `r1`, reviewer/reviewer has `glm` `success`/`done` and
  `opus_lens` `failure`/`no_op`. `find_pairs` returns exactly one pair:
  winner `glm`, loser `opus_lens`, `kind == "quality"`,
  `loser_reasons == ["no_op"]`.
- **No self-pair.** `glm` success plus `glm` failure in one run and node gives
  no pair.
- **Exclusions.**
  - A loser failure with `verdict_revise` gives no pair.
  - A loser `timeout` gives `kind == "infra"`.
  - These rows are ignored:
    - a non-matching trace id such as `tr-plan-1793-42` with node_type
      `planner`;
    - a node_type mismatch;
    - an empty lane;
    - an empty run_id;
    - `verifier_output` that is `'"x"'` (valid JSON, but not a dict).
- **Same run only.** `glm` success in `r1` and `opus_lens` failure in `r2` give
  no pair.

**Summaries and mining:**

- **`summarize`.** The same key in runs `r1`, `r2`, `r3` gives one key:
  - `n_runs == 3` and `run_ids == ["r1", "r2", "r3"]`;
  - `trace_ids` ordered per run, winner ids first.
- **A row is written at the floor.** With 3 quality runs,
  `mine_into_patterns(db_path=db)` returns `written == 1`, and the row with
  `pattern_id_for("framework_edit", "reviewer", "reviewer", "glm", "opus_lens")`
  has:
  - `output_type 'best_practice_rule'`, `status 'observed'`, `frequency 3`;
  - `lesson_text` NULL;
  - a description equal to `describe(key)`.
  - The description starts with `"cluster: task_class=framework_edit "` and
    contains neither `status=failure` nor `status=vacuous`.
  - `mini_ork.context_assembler.pattern_task_class(description) == "framework_edit"`.
- **Below the floor.**
  - Two runs give `held == 1`, `written == 0` and no row.
  - `min_runs=1` still writes nothing for a 2-run key (the clamp).
- **Infra is never written.** A 3-run infra key writes no row. It appears in
  `report["routing"]` with `n_runs == 3`.
- **Re-mining is idempotent.** Call twice and the row count is unchanged. Then
  set `lesson_text='When x: y'` by hand, add a 4th run and re-mine:
  - `frequency` becomes 4;
  - the new trace ids are appended to the evidence;
  - `lesson_text` is unchanged.
- **Opt-out.** With `MO_MGM_MINER=0` (monkeypatch),
  `report["enabled"] is False`, `written == 0` and no row exists.
- **Window.** Traces created 40 days ago are excluded with `window="30d"` and
  included with `window=None`.
- **Missing DB.** A non-existent `db_path` returns a zero report, raises
  nothing and creates no file.

**End-to-end through the existing verify path:**

- Set `MINI_ORK_PROMOTION_MIN_FREQ=3` and unset `MO_EMERGENT_VERIFY*`. After
  mining a 3-run quality key, call
  `mini_ork.learning.reflection_pipeline.reflection_suggest_promotions(db_path=db)`,
  pass its returned list as `json.dumps(...)` to
  `reflection_persist_suggestions(..., db_path=db)`, then call
  `reflection_verify_patterns(db_path=db)`.
- **No lesson, no approval.** The `emergent_patterns` row with that pattern_id
  is `proposed`, with `task_class == "framework_edit"`.
- **With a lesson.** Run
  `UPDATE pattern_records SET lesson_text='When reviewing: emit a verdict file' WHERE pattern_id=?`
  and repeat suggest → persist → verify. The row becomes `approved`.

**Reflect wiring:**

- Copy the `temp_db` fixture, `_run_py`, `FIXED_SINCE` and `_seed_two_traces`
  from `tests/unit/test_mini_ork_reflect_py.py`.
- `_run_py(["--since", FIXED_SINCE], env_extra={"MINI_ORK_DB": temp_db}, wrap_gradients=True)`
  exits 0 and its stdout contains `[mgm_miner]`.
- With `"MO_MGM_MINER": "0"` added to `env_extra`, `[mgm_miner]` is absent and
  `[pattern_miner]` is still present.

## Verification commands

- `pytest -q tests/unit/test_contrastive_lanes.py tests/unit/test_pattern_store_py.py tests/unit/test_pattern_induction_py.py`
- `pytest -q tests/unit/test_mini_ork_reflect_py.py tests/unit/test_reflection_pipeline_py.py tests/unit/test_pattern_task_scope.py tests/unit/test_lesson_conflict.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_contrastive_lanes.py --junitxml="${MINI_ORK_RUN_DIR}/mgm-lane-mining-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/mgm-lane-mining-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/contrastive_lanes.py mini_ork/cli/reflect.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
