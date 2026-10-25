# ECOSYSTEM §6 · K7: apply-loop candidate hygiene, and remove the dead metric-anchor arm

## Goal

The apply loop (`mini_ork/cli/apply.py`) stays opt-in until the probe harvest
(epic rsi-e2) gives it enough probes to measure a gain. Two defects would still
waste it once it is switched on, and one audit arm can never act:

1. **It keeps re-picking a dead source.** `pick_candidate` takes the top row of
   `pattern_records` → `emergent_patterns` → `gradient_records` with
   `ORDER BY … LIMIT 1` and no memory. `apply_run` then refuses that source
   because `_previously_failed` finds a quarantined/rejected `apply_attempts`
   row, and the sweep ends. Live data: 16 consecutive `rejected` rows
   (2026-10-05/06), each the same dead source picked again. The loop never
   advances to the next source.
2. **Candidate rows never leave `candidate`.** `apply.py` inserts
   `workflow_candidates` rows but never updates their `status`;
   `promotion_records` holds the decision. 25 of the 28 rows stuck at
   `candidate` were written by apply and were already decided.
3. **The metric-anchor arm of `mini_ork/learning/verifier_audit.py` can never
   act.** `_read_metric_history` projects `collapse_history` rows onto
   `scored` / `held_out` / `agreements` columns that table does not have, so
   the arm always reports `intact=None` and contributes no flag. Only tests
   pass a `metric_history=` override. Remove the arm. Keep
   `mini_ork/learning/metric_anchor.py` (the `mini-ork metric-anchor`
   command uses it).

All paths are relative to the repository root.

## Mechanism (exact spec)

### A. `pick_candidate` skips failed sources

In all three queries of `pick_candidate`, exclude any source id that already has
an `apply_attempts` row for the same `(task_class, target_name)` with
`decision IN ('quarantined','rejected')`:

```sql
AND <id column> NOT IN (
    SELECT source_id FROM apply_attempts
    WHERE task_class = ? AND target_name = ?
      AND decision IN ('quarantined','rejected')
      AND source_id IS NOT NULL)
```

(`pattern_id` for the first two queries, `gradient_id` for the third; bind
`task_class`, `target_name`.) `ensure_tables` already creates
`apply_attempts`; keep the `_previously_failed` check in `apply_run` unchanged
as defense in depth. Ordering, priority and the returned JSON shape stay the
same.

### B. Candidate status follows the gate decision

In `apply_run`, right after `record_promotion(...)` returns, set the
candidate's status from the final `gate_decision`:

| `gate_decision` | `workflow_candidates.status` |
|---|---|
| `promoted` | `promoted` |
| `quarantined` | `quarantined` |
| `rejected` | `deprecated` |
| anything else | unchanged |

(`rejected` maps to `deprecated` because the table's CHECK constraint allows
only `candidate, shadow, promoted, quarantined, deprecated`.) Implement as a
small helper `_set_candidate_status(candidate_id, decision, db)` that runs one
`UPDATE … WHERE candidate_id=?` and swallows `sqlite3.Error` (a status write
must never change the gate's decision). Do not backfill old rows.

### C. Remove the metric-anchor arm

In `mini_ork/learning/verifier_audit.py`:
- delete `_project_metric_row`, `_read_metric_history`, `_run_metric_anchor`;
- delete the `metric_history` keyword parameter of `audit()` and the
  `# ── H5: metric_anchor` block;
- update the module and `audit()` docstrings ("the three detectors";
  remove `metric_history` from the parameter list);
- `evidence` no longer carries a `metric_anchor` key.

In `tests/unit/test_verifier_audit_py.py`, delete the `_clean_metric_history`
helper, drop every `metric_history=` argument, and delete or adjust the
assertions on `evidence["metric_anchor"]` (keep every other assertion as is).

## Files in scope (touch ONLY these)

- `mini_ork/cli/apply.py`
- `mini_ork/learning/verifier_audit.py`
- `tests/unit/test_verifier_audit_py.py`
- `tests/unit/test_eco6_k7_apply_hygiene.py` (new)

Do NOT modify `mini_ork/learning/metric_anchor.py`, `mini_ork/gates/promotion_gate.py`,
the probe scorer, any migration, or `MO_APPLY_ENABLED` / `MO_AUTO_APPLY` defaults.

## Tests (`tests/unit/test_eco6_k7_apply_hygiene.py`, hermetic, tmp DB via `ensure_tables`)

- **A failed source is skipped.** Seed two `gradient_records` rows for
  `(task_class='tc', target='agent.reviewer.prompt')` with confidences 0.9
  (`g-hi`) and 0.5 (`g-lo`), and one `apply_attempts` row
  `(tc, agent.reviewer.prompt, source_id='g-hi', decision='rejected')`.
  `pick_candidate('tc', 'agent', 'agent.reviewer.prompt', db=…)` returns
  `source_id == 'g-lo'`. Without the attempts row it returns `g-hi`.
- **A failure for another target does not hide the source.** The same
  `apply_attempts` row but `target_name='other'` → `g-hi` is picked.
- **Same rule for `pattern_records`.** One observed `prompt_change` pattern
  `p-1` with a `quarantined` attempt and one `p-2` without → `p-2`.
- **Status follows the decision.** `_set_candidate_status` on a seeded
  `workflow_candidates` row: `promoted` → `promoted`, `quarantined` →
  `quarantined`, `rejected` → `deprecated`, `no_candidate` → unchanged.
- **The audit has no metric-anchor arm.** `verifier_audit.audit(None, db)`
  returns evidence without a `metric_anchor` key, and
  `inspect.signature(verifier_audit.audit)` has no `metric_history` parameter.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k7_apply_hygiene.py tests/unit/test_verifier_audit_py.py tests/unit/test_cli_apply_py.py tests/unit/test_apply_significance.py tests/unit/test_apply_harness_targets.py tests/unit/test_collapse_history_writer.py tests/unit/test_promotion_gate_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k7_apply_hygiene.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k7-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k7-junit.xml`

## Success criteria

- The verification command passes. Drop from it any listed test file that does
  not exist; do not create it.
- `ruff check --select F,E9 mini_ork/cli/apply.py mini_ork/learning/verifier_audit.py` is clean.
