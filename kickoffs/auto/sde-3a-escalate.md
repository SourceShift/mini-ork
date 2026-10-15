# SDE-3a escalate engine faults to a linked fix epic (self-driving epic, step 3a)

## Goal

A failed run whose diagnosis says the ENGINE is at fault opens a `framework-edit`
fix epic linked back to that run, and the run waits for the fix instead of being
handed to a human. The operator-authored policy
`mini_ork/recovery/escalation_policy.py` (`should_escalate`, already committed)
decides; this step gathers its inputs and acts on its answer.

Motivating case: `run-1791914481-48337` diagnoses
`engine / deliver / publisher`, `routes_to: fix_epic`
(`python3 -m mini_ork.recovery.diagnose run-1791914481-48337 --json --no-write`),
yet `auto_repair.decide` still returns `human`. And `triage.failures.triage_run`
cannot be reused as-is: it re-decides blame from Python traceback frames
(`triage/blame.py`), so a delivery-stage engine fault with no traceback is filed
as `consumer`/`unknown` and never promoted.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. **`mini_ork/triage/failures.py`** gains
   `file_diagnosis(run_id, diagnosis: dict, *, home, db, root, promote: bool) -> TriageResult`.
   It reuses the existing emit → `bug_report_sweep` → fingerprint lookup →
   `bug_report_promote(recipe="framework-edit", bug_ids=[id])` →
   `_epic_for_bug` sequence of `triage_run`, but takes the attribution from the
   diagnosis instead of `blame.attribute`:
   - `agent_role="self-driving"`, severity `high`, `confidence` = the diagnosis
     confidence, `observed_in` = the run dir;
   - title `f"[{layer}/{stage}] {node}: {first evidence excerpt, 80 chars}"`;
   - description = diagnosis summary + every evidence item (`kind ref: excerpt`).
   Re-filing the same fingerprint reuses the stored row (no frequency inflation),
   exactly as `triage_run` does. `triage_run` itself is unchanged.

2. **New `mini_ork/recovery/escalation.py`**:
   - `gather_inputs(home, run_id, diagnosis) -> EscalationInputs`:
     - `layer`, `source`, `confidence` from the diagnosis;
     - `calibrated_bar=None`, `labelled_count=0` (labelling lands in SDE-3b);
     - `fix_epics_last_24h` = `bug_reports` rows with `agent_role='self-driving'`,
       `promoted_to_epic_id IS NOT NULL`, `updated_at >= now - 86400`;
     - `daily_cap` = int env `MO_FIX_EPIC_DAILY_CAP`, default 3 (non-int → 3, negative → 0);
     - `signature_fix_failed_before` = a `self-driving` bug row with the same
       fingerprint whose promoted epic's `epics.status` is `failed`, `escalated`
       or `abandoned`;
     - `collapse_tripped` = the last `MO_FIX_EPIC_BREAKER_N` (default 3)
       self-driving fix epics, ordered by `epics.created_at`, all have a terminal
       unsuccessful status (`failed | escalated | abandoned`). Fewer than N such
       epics → False.
   - `escalate(home, run_id, diagnosis) -> dict`: calls
     `escalation_policy.should_escalate(gather_inputs(...))`. On `escalate=True`:
     `file_diagnosis(..., promote=True)`, then records in `repair.json`
     `"state": "waiting_on_fix"`, `"fix_epic_id"`, `"fix_bug_id"`,
     `"triaged": true` (so the give-up triage path never files a second report),
     emits a `run_events` row `auto_repair.escalate` with the decision, and
     enqueues a NON-blocking oversight item
     (`oversight_inbox.enqueue("self-driving-escalation", "fix-epic", context={run_id, fix_epic_id, reason})`).
     On `escalate=False`: returns the refusal reason; nothing is filed.
     Any exception → returns `{"escalated": false, "reason": "escalation error: ..."}`;
     never raises into the run.

3. **`mini_ork/recovery/auto_repair.py`**:
   - `decide`: after the stop rules (attempts / budget / no-progress) and before
     the lane rule, when the diagnosis `layer == "engine"` and
     `routes_to == "fix_epic"` and `MO_AUTO_ESCALATE` is not `"0"` (default on —
     operator decision), return action `escalate`.
   - `apply` for `escalate`: call `escalation.escalate`; on success the attempt is
     recorded with `action: escalate`, no `recover` is spawned, and the run's
     `repair.json` state is `waiting_on_fix`. On refusal, record the attempt as
     `human` with the policy's reason verbatim (e.g. "daily fix-epic cap reached
     (3/3)").
   - `escalate` is NOT in `_SPAWNABLE`.

4. Do NOT modify `mini_ork/recovery/escalation_policy.py` (operator-owned, frozen).

## Files in scope

- mini_ork/recovery/escalation.py (new)
- mini_ork/recovery/auto_repair.py
- mini_ork/triage/failures.py
- tests/unit/test_escalation.py (new)

ALLOW_HIGH_BLAST_RADIUS: this operator-authored step intentionally edits auto_repair.py, which is on the frozen-kernel list for auto-opened runs only.

## Tests (`tests/unit/test_escalation.py`, hermetic: temp home + sqlite with `task_runs`, `run_events`, `bug_reports`, `epics`, `mo_inbox_gates`; no LLM, no network)

- the `run-1791914481-48337` shape → `decide` returns `escalate`; `apply` files
  one `self-driving` bug, promotes one `framework-edit` epic whose kickoff starts
  with `<!-- mini-ork:origin=auto-triage -->`, writes `repair.json`
  `state=waiting_on_fix` with `fix_epic_id`, `triaged=true`, one oversight item,
  and spawns no recover;
- same run applied twice → still one bug row, one epic;
- 3 self-driving promotions in the last 24 h with cap 3 → refused, recorded as
  `human` with the cap reason, nothing filed;
- last 3 self-driving epics `failed` → refused (breaker);
- same fingerprint whose earlier epic `failed` → refused;
- `MO_AUTO_ESCALATE=0` → `decide` returns what it returned before this step;
- a `change`-layer diagnosis never escalates;
- `file_diagnosis` attributes from the diagnosis even when no traceback exists;
- the policy as wired (commit `d1606e13`): a `model`-source engine diagnosis at
  confidence 0.90 with 0 labels escalates (bootstrap bar 0.85), at 0.80 is
  refused with the bar in the reason, and with ≥30 labels and
  `calibrated_bar=None` is refused as uncertifiable — `gather_inputs` must pass
  `source` / `confidence` through unchanged so these paths are reachable.

Also run `tests/unit/test_auto_repair.py tests/unit/test_triage_failures_py.py tests/unit/test_triage_auto_repair_gate.py tests/unit/test_diagnose.py tests/unit/test_frozen_kernel.py`.

## Self-application measurement

Against a COPY of the live state DB (never the live file):
`cp /Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db ${MINI_ORK_RUN_DIR}/state-copy.db`,
then run `auto_repair.decide` for each of the 50 most recent failed runs with
the copy as the DB and `write=False` diagnosis. Accepted iff exactly the
engine-layer `fix_epic` diagnoses decide `escalate` (today: one,
`run-1791914481-48337`) and no other run changes action.

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/escalation-replay.json` — per run: diagnosis layer,
  decision action before/after, and the policy reason.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_escalation.py tests/unit/test_auto_repair.py tests/unit/test_triage_failures_py.py tests/unit/test_triage_auto_repair_gate.py tests/unit/test_diagnose.py tests/unit/test_frozen_kernel.py` passes.

## Rules

Edit files directly; do not emit unified diffs. Do not touch
escalation_policy.py, execute.py, publisher*.py, or any recipe.
