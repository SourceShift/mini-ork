# RSI gap: reward/evidence divergences enqueue once, not once per writeback pass

## Goal

`mini_ork/learning/writeback.py` re-scans the same trace window on every
learning writeback and hands every contradicted run it finds to
`reward_audit.enqueue_divergences`. That function inserts a new
`mo_inbox_gates` row for each entry on every call and never checks for an
existing row. The live DB holds 686 pending `reward_evidence_divergence` rows
that cover only 14 distinct `(feature, phase)` pairs; one run
(`run-1792400538-82050`) alone has 245 copies. The oversight inbox fills with
duplicates, which hides real items and is exactly the "abandoned oversight"
failure mode the inbox was built to measure.

Make the enqueue idempotent per divergence, and give the operator a one-shot
dedupe for the rows already written.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/learning/reward_audit.py`, `enqueue_divergences`:
   - Before calling `oversight_inbox.enqueue` for an entry, check whether a
     `mo_inbox_gates` row already exists with the same `gate_id`
     (`DIVERGENCE_GATE_ID`), `feature` (`f"reward/evidence divergence: {run_id}"`)
     and `phase` (the trace id string, or `""`), **in any status**. A divergence
     the operator already approved or rejected must not come back.
   - If one exists, skip the entry: it is not counted in the return value and
     does not consume the per-call cap.
   - Also skip an entry whose `(feature, phase)` key already appeared earlier in
     the same `divergences` list.
   - The existence check opens the DB itself (plain `sqlite3`, the same path
     the enqueue uses). If the table does not exist yet, treat it as "no row".
     A failure of the check must never raise out of `enqueue_divergences`; on a
     check failure, fall back to enqueueing (the old behaviour) for that entry.
2. Same file, new function `dedupe_divergence_rows(db_path: str) -> int`:
   - Within `gate_id = DIVERGENCE_GATE_ID` only, for each `(feature, phase)`
     group keep the row with the smallest `inbox_id` and DELETE the other rows
     whose `status = 'pending'`. Never delete a resolved row
     (`approved`/`rejected`), and never touch rows of any other `gate_id`.
   - Return the number of rows deleted. Add it to `__all__`.
3. Same file: a module CLI `python -m mini_ork.learning.reward_audit dedupe
   [--db PATH] [--dry-run]` that prints the number of rows it deleted (or, with
   `--dry-run`, would delete, without deleting). `--db` defaults to
   `MINI_ORK_DB`, then `.mini-ork/state.db`. Put the `if __name__ == "__main__":`
   guard at the very END of the file, after every function definition.

## Files in scope (touch ONLY these)

- `mini_ork/learning/reward_audit.py` — idempotent enqueue, `dedupe_divergence_rows`, CLI
- `tests/unit/test_reward_audit_py.py` — new tests below

Do NOT modify any other file. Do not touch `mini_ork/gates/oversight_inbox.py`
or `mini_ork/learning/writeback.py`.

## Tests (append to `tests/unit/test_reward_audit_py.py`, hermetic temp sqlite)

- calling `enqueue_divergences` twice with the same entries → the second call
  returns 0 and the table has one row per `(feature, phase)`;
- an entry whose existing row was resolved `rejected` is not re-enqueued;
- the same entry twice inside one `divergences` list → one row, return 1;
- duplicates already skipped do not consume the cap
  (`MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT=1`, first entry already present, second
  new → return 1, second entry enqueued);
- `dedupe_divergence_rows` on a table with 3 pending copies of key A, 2 pending
  copies of key B plus 1 `approved` row of key B, and 2 pending rows of another
  `gate_id` → deletes exactly 3 rows (2 of A, 1 pending of B, keeping B's
  smallest `inbox_id`), leaves the approved row and the other gate's rows;
- the CLI with `--dry-run` reports the count and deletes nothing (run it with
  `subprocess` and `sys.executable -m mini_ork.learning.reward_audit`, cwd =
  repository root, against a temp DB).

## Verification commands

- `pytest -q tests/unit/test_reward_audit_py.py tests/unit/test_writeback_validity_masking_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_reward_audit_py.py --junitxml="${MINI_ORK_RUN_DIR}/inbox-dedupe-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/inbox-dedupe-junit.xml`

## Success criteria

- `python3 -m pytest -q tests/unit/test_reward_audit_py.py` passes, including
  every pre-existing test in that file unchanged.
- `ruff check --select F,E9 mini_ork/learning/reward_audit.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Do not change the signature or
return contract of `enqueue_divergences` beyond what is specified above.
