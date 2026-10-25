# ECOSYSTEM §6 · K11: a verify that fails after publish is marked, never silent; step-rules stops misreading paths

## Goal

A run is marked `published` before the post-run verify runs (`publisher_node`
for recipes with a publisher, `execute.py`'s `finalize_passed` for recipes
without one; the post-run verify, `python -m mini_ork.cli.verify`, runs
afterwards from `mini_ork/cli/main.py`).

The 30 most recent published runs whose post-run verify ended `fail`/`partial`
break down as:
- **1 real defect** (`run-1791733875-66984`); a workflow verifier caught it, and
  `publisher._publish_truth_refusal` now refuses that case before publish.
- **15 environment failures inside workflow verifiers** that the post-run
  verify only re-read.
- **14 cases where the post-run verify checked the wrong thing**: 7 step-rules
  gate failures on misread paths (`"$MINI_ORK_RUN_DIR/source-corpus.json"` →
  `MINI_ORK_RUN_DIR/source-corpus.json`; `<run_dir>/*.sidecar.json` →
  `.sidecar.json`; `${MINI_ORK_ROOT}/recipes/…` → `/recipes/…`), 6 stale traces
  from failed first attempts a later recover published, 1 plan output with a
  prose suffix.

A hard pre-publish gate would have blocked 8 good publishes and caught nothing
new, so none is added. This kickoff instead:

1. **Fixes the live false failure.** `step_rules.rule_named_test_path_exists`
   expands variables it can resolve, ignores words it cannot read (unset
   variables, `<placeholders>`, globs), and also looks up relative paths under
   the run dir.
2. **Makes a failure after publish visible.** Today a `partial` exits 0 and
   leaves no mark, and a `fail` only sets the exit code (`retry_hint` returns
   `None` for a `published` row, so no NEEDS-CHANGE.md and no inbox item). A
   published run whose post-run verify ends `fail`/`partial` now gets:
   `task_runs.verdict = 'ESCALATE'` (status stays `published`), a `notes` line,
   `<run_dir>/post-verify-after-publish.json`, and one non-blocking
   oversight-inbox item.

Constraints: `task_runs.status` CHECK allows classified, planned, executing,
verifying, reviewing, published, rolled_back, failed, not_needed, superseded —
do not add a status. `task_runs.verdict` CHECK allows APPROVE, REQUEST_CHANGES,
ESCALATE, CRASH or NULL; there are no triggers on `task_runs`.
`publisher._mark_published` writes (`published`, `APPROVE`).
`orchestration/active_state_index.py` reads only `verdict='APPROVE'`, so an
escalated run drops out of the planner's facts and stays delivered.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/gates/step_rules.py`

1. Regexes:
   - `_ASSIGN_FLAG = re.compile(r"-{1,2}[\w-]+=[\"']?[\w./-]+")` (optional quote after `=`).
   - `_REDIRECT = re.compile(r"\d?>>?\s*[\"']?[\w./-]+")` (adds `>>` and an optional quote).
   - New `_VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")`.
   - New `_UNRESOLVED_WORD = re.compile(r"\S*[$<>*?{}]\S*")`.
2. New `_expand_refs(text: str, env: dict) -> str`: replace every `${NAME}` /
   `$NAME` whose `NAME` has a non-empty value in `env` with that value; leave
   all other references unchanged.
3. `_command_paths(plan, env=None)`: same inputs as today
   (`verifier_contract.checks[].command` + `artifact_contract.success_verifiers`).
   Per command, in order: `_expand_refs(cmd, env or {})` → `_ASSIGN_FLAG` scrub
   → `_REDIRECT` scrub → `_UNRESOLVED_WORD` scrub (replace with a space) →
   `_PATH_TOKEN.findall`. De-duplicate, keep order.
4. `_resolve(token, workspace, root, run_dir="")`: an absolute token passes if
   it exists; otherwise try the bases `(workspace, root, run_dir)`; then the
   existing dispatcher lookup, unchanged.
5. `rule_named_test_path_exists(plan_path, workspace, root, run_dir="")`:
   - `env = {k: v for k, v in os.environ.items() if v}`; if `root`,
     `env["MINI_ORK_ROOT"] = root`; if `run_dir`, `env["MINI_ORK_RUN_DIR"] = run_dir`;
     if `workspace`, `env.setdefault("MO_TARGET_CWD", workspace)`.
   - `tokens = _command_paths(plan, env)`.
   - The "no root to resolve" defer applies only when `workspace`, `root` and
     `run_dir` are all empty.
   - A token is missing when `_resolve(t, workspace, root, run_dir)` is false.
   - Every verdict and detail string keeps its current wording.
6. `run_rules(artifact_path, plan_path="", workspace="", root="", run_dir="")`;
   each `_RULES` callable takes `(a, p, w, r, d)`; rule 1 ignores `d`.
7. `evaluate`: pass `run_dir=str(ctx.get("run_dir") or "")` to `run_rules`.

Do not change rule 1, `gate_verdict` or `MO_STEP_RULES`.

### 2. `mini_ork/cli/verify.py`

1. Add `"run_dir": run_dir or ""` to the gate context JSON passed to
   `gate_registry.gate_run_all`.
2. Inside the existing `try`, compute
   `failed_gates = sorted(g.get("gate_id", "") for g in summary.get("gates", []) if g.get("verdict") == "fail")`
   (`[]` in the `except` branch). When the gates failed and `failed_gates` is
   non-empty, write the `__gates__` row as
   `json.dumps({"verifier": "__gates__", "pass": False, "evidence_path": "gate_registry", "detail": "gate(s) failed: " + ", ".join(failed_gates)}, separators=(",", ":"))`.
   Otherwise keep the existing fail and pass row strings byte-for-byte.
   `__gates_unmeasured__` is unchanged.
3. Add `_parse_rows(results)`: `json.loads` each string; anything that does not
   parse to a dict becomes `{"raw": <string>}`.
4. Add `_record_post_run(...)`, called directly after the terminal
   `_safe_trace_write`, inside `if dry_run == 0:`. It must never raise.
   - **Result file.** If `run_dir` is an existing directory, write
     `<run_dir>/verify-result.json` (indent 2) with `trace_id`, `verdict`,
     `artifact_path`, `task_class`, `pass_count`, `fail_count`, `results` (the
     parsed rows) and `written_at` (int epoch). Ignore `OSError`. The last verify
     call wins (the lifecycle's post-run verify is always last).
   - **Flag.** If `verdict in ("fail", "partial")`, call
     `post_publish_flag.mark(db, os.environ.get("MINI_ORK_TASK_RUN_ID") or context_env("MINI_ORK_RUN_ID", ""), run_dir or "", verdict, trace_id=trace_id, failing=[r for r in rows if r.get("pass") is False])`,
     importing lazily, with the import and call inside `try: … except Exception: pass`.
5. stdout and the return code are unchanged apart from item 2.

### 3. `mini_ork/verify/post_publish_flag.py` (new)

```
GATE_ID = "post_verify_after_publish"
FLAG_FILE = "post-verify-after-publish.json"
FLAGGED_VERDICTS = frozenset({"fail", "partial"})
def mark(db, run_id, run_dir, verdict, *, trace_id="", failing=None, now=None) -> dict | None
```

1. Return `None` and write nothing when `verdict` is not in `FLAGGED_VERDICTS`,
   `db` or `run_id` is empty, or `db` is not a file.
2. `SELECT status, verdict FROM task_runs WHERE id = ?` (connect timeout 15;
   `PRAGMA busy_timeout = 15000`). Return `None` on `sqlite3.Error`, a missing
   row, or `status != 'published'`.
3. `names` = sorted unique `str(row["verifier"])` of the dict rows in `failing`.
   Note: `f"post-run verify {verdict} after publish ({trace_id}): {', '.join(names) or 'no failing row recorded'}"`.
4. `UPDATE task_runs SET verdict = 'ESCALATE', notes = COALESCE(notes || '; ', '') || ?, updated_at = ? WHERE id = ? AND status = 'published' AND (verdict IS NULL OR verdict = 'APPROVE')`;
   `escalated = rowcount == 1`. Never touch `status`, `ended_at`,
   `duration_ms`, `cost_usd`. A `CRASH` verdict is never overwritten.
5. Inbox item: `feature = f"post-run verify failed after publish: {run_id}"`.
   If a `mo_inbox_gates` row with `gate_id = GATE_ID AND feature = feature`
   exists (any status), reuse its `inbox_id` (missing table = no row).
   Otherwise `oversight_inbox.enqueue(GATE_ID, feature, phase="verify", context={"run_id", "verdict", "trace_id", "failing": names, "run_dir"}, db_path=db)`
   with no `blocks_dispatch_for` (never blocks a dispatch). Any exception →
   `inbox_id = None`.
6. Record: `run_id`, `verdict`, `trace_id`, `"status": "published"`,
   `"task_runs_verdict"` (`"ESCALATE"` if escalated, else the verdict read in
   step 2), `"failing"` (each failing row's `verifier`, `detail`,
   `evidence_path`, `rc` when present), `"inbox_id"`, `"flagged_at"` (`now` or
   `int(time.time())`). If `run_dir` is an existing directory, write it to
   `<run_dir>/FLAG_FILE` (indent 2); ignore `OSError`.
7. Return the record. Any unexpected exception returns `None`.

## Files in scope (touch ONLY these)

- `mini_ork/gates/step_rules.py`
- `mini_ork/cli/verify.py`
- `mini_ork/verify/post_publish_flag.py` (new)
- `tests/unit/test_eco6_k11_post_publish_verify.py` (new)

Do NOT modify `mini_ork/cli/publisher.py`, `mini_ork/cli/main.py`,
`mini_ork/cli/execute.py`, `mini_ork/gates/gate_registry.py`,
`mini_ork/gates/oversight_inbox.py`, `mini_ork/recovery/retry_hint.py`, any
migration or schema, or any path in `config/kernel-frozen.yaml`. Every existing
test must pass unchanged.

## Tests (`tests/unit/test_eco6_k11_post_publish_verify.py`, hermetic)

Autouse fixture: delete `MINI_ORK_RUN_DIR`, `MINI_ORK_RUN_ID`,
`MINI_ORK_TASK_RUN_ID`, `MINI_ORK_DB`, `MINI_ORK_HOME`, `MINI_ORK_ROOT`,
`MINI_ORK_PLAN_PATH`, `MINI_ORK_TASK_CLASS`, `MINI_ORK_DRY_RUN`,
`MINI_ORK_RECIPE`, `MINI_ORK_TARGET_REPO`, `MO_TARGET_CWD`,
`MO_MUTATION_REPORT`, `MO_MUTATION_TEST_CMD`, `MO_VERIFY_RERUN`,
`MO_STEP_RULES`, `RUN_ID`; set `MO_MUTATION_ADVERSARY=0`. Step-rules tests use
tmp dirs `ws`, `root`, `rd` and a `_plan(command=...)` helper writing
`{"task_class": "code_fix", "verifier_contract": {"checks": [{"id": "c1", "command": ...}]}}`.

Step rules:
1. `python3 tool.py verify --corpus "$MINI_ORK_RUN_DIR/source-corpus.json"` with
   `ws/tool.py` and `rd/source-corpus.json` present →
   `rule_named_test_path_exists(plan, ws, root, run_dir=rd) == ("pass", "2 named path(s) resolved")`.
2. `jq -e . "${MINI_ORK_RUN_DIR}/source-corpus.json"` with the file absent →
   `"fail"`; detail contains `f"{rd}/source-corpus.json"` and not `"MINI_ORK_RUN_DIR/"`.
3. `python3 ${MINI_ORK_ROOT}/recipes/r1/verifiers/v.py` with that file under
   `root` → `"pass"`.
4. `python3 -c "import sys" <run_dir>/*.sidecar.json` → `"defer"`.
5. `test -s ".mini-ork/runs/$RUN_ID/review.json"` → `"defer"`.
6. `grep -rln path_globs recipes/*/task_class.yaml` → `"defer"`.
7. With `run_dir=rd` and no files: `python3 -m pytest -q > "$MINI_ORK_RUN_DIR/run.json"`
   and `python3 -m pytest -q --report="$MINI_ORK_RUN_DIR/r.json"` → `"defer"`.
8. `test -s verifier-result-x.json` with `rd/verifier-result-x.json` → `"pass"`
   with `run_dir=rd`, `"fail"` without it.
9. `python3 -m pytest tests/unit/test_ghost.py` → `"fail"`, detail names it.
10. Case-1 setup through
    `sr.evaluate("", json.dumps({"plan_path": plan, "workspace": str(ws), "run_dir": str(rd)}), "", str(root))`
    → `"pass"`.

Verify level: a module-scoped template DB from
`mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0), copied per
test. A helper writes `root/verifiers/vpass.py` (`print("pass evidence")`) and
`vfail.py` (`import sys`, `print("fail evidence")`, `sys.exit(1)` on separate
lines) and the plan
`{"task_class": "k11", "artifact_contract": {"success_verifiers": [...]}}`; sets
`MINI_ORK_HOME`, `MINI_ORK_RUN_DIR=rd` (existing), `MINI_ORK_RUN_ID=run-k11`;
monkeypatches `verify_cli.gate_registry.gate_run_all` with a fake that records
the context JSON and returns `{"any_fail": False, "gates": []}` unless
overridden; calls
`verify_cli.main(["--plan", plan, "--task-class", "k11"], db=db, root=root)`
with no artifact (as the lifecycle does); returns `(json.loads(stdout), rc)`.
`_insert_run(db, status, verdict)` inserts `task_runs` `id='run-k11'`,
`task_class='k11'`, `kickoff_path='k.md'`, `created_at=1`, `updated_at=1`.

- V1. `('published','APPROVE')`, vpass + vfail → verdict `"partial"`, rc 0; row
  status `published`, verdict `ESCALATE`, notes contain
  `post-run verify partial after publish` and `vfail.py`;
  `rd/post-verify-after-publish.json` has `verdict "partial"`, `trace_id`
  starting `"tr-verify-"`, failing verifiers `["vfail.py"]`,
  `task_runs_verdict "ESCALATE"`, int `inbox_id`; exactly one `mo_inbox_gates`
  row (`gate_id='post_verify_after_publish'`,
  `feature='post-run verify failed after publish: run-k11'`, `phase='verify'`,
  `status='pending'`, `blocks_dispatch_for` in `("", None)`);
  `rd/verify-result.json` has `"partial"` and the same `trace_id`.
- V2. `('published','APPROVE')`, vfail only → rc 1, verdict `ESCALATE`, flag
  file `"fail"`.
- V3. V1 twice → one inbox row; `notes.count("post-run verify") == 1`.
- V4. `('executing', NULL)`, vpass + vfail → row unchanged, no flag file, no
  inbox row; `verify-result.json` has `"partial"`.
- V5. `('published','APPROVE')`, vpass only → `"pass"`, row unchanged, no flag
  file or inbox row.
- V6. A DB holding only the `execution_traces` DDL (copy `_TRACE_DDL` from
  `tests/unit/test_verify_partial_trace.py`), vpass + vfail → `"partial"`, rc 0,
  nothing raises, no flag file.
- V7. `('published','CRASH')`, vpass + vfail → verdict stays `CRASH`; flag file
  and inbox row exist.
- V8. The fake's recorded context has `run_dir == str(rd)`.
- V9. Fake returns `{"any_fail": True, "gates": [{"gate_id": "step-rules-gate", "verdict": "fail"}, {"gate_id": "oracle-liveness", "verdict": "pass"}]}`
  with vpass only → `"partial"`; the `__gates__` row equals
  `{"verifier": "__gates__", "pass": False, "evidence_path": "gate_registry", "detail": "gate(s) failed: step-rules-gate"}`.
- V10. Default fake → the `__gates__` row has exactly `{verifier, pass, evidence_path}`, `pass` true.
- M1. `post_publish_flag.mark(db, "run-k11", str(rd), "vacuous")` and `"pass"`
  → `None`; the published row is unchanged.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k11_post_publish_verify.py tests/unit/test_step_rules_py.py tests/unit/test_mini_ork_verify_py.py tests/unit/test_verify_partial_trace.py tests/unit/test_post_run_verify_reuse.py tests/unit/test_verify_vacuous_verifier.py tests/unit/test_verify_evidence_unique.py tests/unit/test_overlay_recipe_verify.py tests/unit/test_run_check_placement.py tests/unit/test_oversight_calibration_py.py tests/unit/test_mutation_adversary_gate_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k11_post_publish_verify.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k11-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k11-junit.xml`

## Success criteria

- The verification command passes (drop any listed test file that does not
  exist; do not create it); every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/gates/step_rules.py mini_ork/cli/verify.py mini_ork/verify/post_publish_flag.py` is clean.
- No new hard gate: publisher, lifecycle and execute paths are untouched. A
  passing post-run verify writes nothing to `task_runs` or `mo_inbox_gates`.
- verify's stdout and exit code are unchanged except for the new `detail` on a
  failing `__gates__` row.
