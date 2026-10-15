# SDE-3a repair — a run parked on a fix epic is inert to auto-repair

## Goal

Pass 1 (commit `b6aff800`) parks an escalated run with `repair.json`
`"state": "waiting_on_fix"` plus `fix_epic_id` / `fix_bug_id`. But a later
`decide` on the same run (any `mini-ork repair --sweep`, the board's retry, and
the SDE-4 supervisor that will revive parked runs) hits the stop rules first: the
same signature repeats, so it returns `human` / "no progress", and `apply` then
records a give-up — overwriting `waiting_on_fix`. The run is stranded: nothing
will ever revive it when its fix lands. The pass-1 test
`test_repeat_failure_is_stopped_not_re_escalated` asserts exactly this behaviour
and must change.

All paths are relative to the repository root.

## Mechanism (exact)

1. `mini_ork/recovery/auto_repair.py` `decide`: immediately after the
   `MO_AUTO_REPAIR == "0"` check and the run-dir/status load, if the run's
   `repair.json` has `state == "waiting_on_fix"`, return
   `_decision("none", reason=f"waiting on fix epic {fix_epic_id}")` — before the
   stop rules, the escalate rule and everything else. Include the `fix_epic_id`
   in the decision dict as an additive key.
2. `apply` called directly with that `none` decision, and `maybe_repair`, must
   leave `repair.json` byte-identical (no attempt appended, no state change)
   and spawn nothing. `_mark_repaired_if_done` must not clear `waiting_on_fix`.
3. Nothing else changes: an un-parked run behaves exactly as in pass 1.

## Files in scope

- mini_ork/recovery/auto_repair.py
- tests/unit/test_escalation.py

## Tests (extend `tests/unit/test_escalation.py`)

- Replace `test_repeat_failure_is_stopped_not_re_escalated` with: after the first
  escalate, a second `decide` returns `none` with "waiting on fix epic <id>";
  `apply` + `maybe_repair` on it leave `repair.json` byte-identical; still one
  bug row and one epic; no recover spawned.
- A `repair.json` with `state` other than `waiting_on_fix` keeps pass-1 behaviour
  (same-signature repeat → `human` / no progress).
- `MO_AUTO_REPAIR=0` still returns its own `none` reason first.

Also run `tests/unit/test_auto_repair.py tests/unit/test_triage_auto_repair_gate.py tests/unit/test_diagnose.py`.

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_escalation.py tests/unit/test_auto_repair.py
```

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_escalation.py tests/unit/test_auto_repair.py tests/unit/test_triage_auto_repair_gate.py tests/unit/test_diagnose.py` passes.

ALLOW_HIGH_BLAST_RADIUS: operator-authored repair of auto_repair.py (frozen only for auto-opened runs).

## Rules

Edit files directly; do not emit unified diffs. Do not touch escalation.py,
escalation_policy.py, triage/, or diagnose.py.
