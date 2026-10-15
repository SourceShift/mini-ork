# SDE-3b2 diagnosis ledger + calibrated escalation bar (self-driving epic)

## Goal

The frozen escalation policy admits a model-made engine diagnosis above a bar:
0.85 until 30 labelled outcomes exist, then a calibrated Learn-Then-Test bar.
Today `escalation.gather_inputs` hard-codes `calibrated_bar=None` and
`labelled_count=0`, so the calibrated path can never activate. This step adds
the ledger that records every diagnosis the loop ACTS on, an API to label it
right or wrong once its outcome is known (SDE-4 will call it when a revived run
gets past — or fails again at — the node that failed), and wires the calibrated
bar into `gather_inputs`.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New migration `db/migrations/00NN_diagnosis_ledger.sql`, `NN` = the next free
   number in `db/migrations/` at implementation time (0073 when this was written;
   check for collisions): table `diagnosis_ledger` —
   `id INTEGER PRIMARY KEY`, `run_id TEXT NOT NULL`, `source TEXT NOT NULL`,
   `layer TEXT`, `routes_to TEXT`, `confidence REAL`, `model_lane TEXT`,
   `signature TEXT`, `action TEXT`, `fix_epic_id TEXT`, `created_at INTEGER NOT NULL`,
   `label INTEGER` (NULL unlabelled, 1 right, 0 wrong), `labelled_at INTEGER`,
   `label_reason TEXT`; unique `(run_id, source, signature)`.
2. New `mini_ork/stores/diagnosis_ledger.py`, sqlite like its sibling stores,
   creating the table if absent (so a home that has not migrated still works):
   - `record(db, run_id, diagnosis, *, action, fix_epic_id=None) -> int` —
     idempotent on the unique key (returns the existing id);
   - `set_label(db, ledger_id, correct: bool, reason: str)` — first label wins;
     relabelling is refused;
   - `labelled(db, source="model") -> list[tuple[float, bool]]`.
3. `mini_ork/recovery/escalation.py`:
   - `gather_inputs` sets `labelled_count = len(labelled(db, "model"))` and,
     when `labelled_count >= escalation_policy.MIN_LABELS`,
     `calibrated_bar = mini_ork.gates.abstain_gate._ltt_min_confidence(rows, alpha)`
     with `alpha = abstain_gate.alpha()` (that helper's existing tighten-only
     clamp); a helper that returns no certifiable bar yields `None`.
     Below `MIN_LABELS` it stays `None` (the policy then uses its bootstrap bar).
   - `escalate` records the acted-on diagnosis in the ledger (`action` =
     `escalate` or the refusal's `human`, with `fix_epic_id` when one was
     opened), best-effort — a ledger failure never blocks or raises.

## Files in scope

- db/migrations/00NN_diagnosis_ledger.sql (new)
- mini_ork/stores/diagnosis_ledger.py (new)
- mini_ork/recovery/escalation.py
- tests/unit/test_diagnosis_ledger.py (new)

## Tests (`tests/unit/test_diagnosis_ledger.py`, hermetic temp sqlite)

- `record` twice with the same key → one row, same id;
- `set_label` then a second `set_label` → refused, first label kept;
- `labelled` returns only labelled rows of the requested source;
- `gather_inputs` with 29 labelled model rows → `labelled_count == 29`,
  `calibrated_bar is None`; with 40 rows of which wrong ones sit at low
  confidence → a bar strictly between those and the right ones' confidences;
  with 40 rows where high-confidence rows are wrong → `None` (uncertifiable),
  and the frozen policy then refuses a model diagnosis;
- `escalate` writes one ledger row per escalated run, with the fix epic id;
- a broken ledger DB path never makes `escalate` raise.

Also run `tests/unit/test_escalation.py tests/unit/test_frozen_kernel.py`.

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_diagnosis_ledger.py tests/unit/test_escalation.py --junitxml="${MINI_ORK_RUN_DIR}/ledger-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/ledger-junit.xml`

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_diagnosis_ledger.py tests/unit/test_escalation.py tests/unit/test_frozen_kernel.py` passes.

## Rules

Edit files directly; do not emit unified diffs. Do not touch
escalation_policy.py, abstain_gate.py, diagnose.py, auto_repair.py, or any recipe.
