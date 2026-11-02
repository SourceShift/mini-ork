# Make the oracle refuse self-contradictory execution evidence

## Goal

`mini-ork instruments oracle-integrity` currently measures the real judge at
`pass: false`: three of the seven injected evidence defects are reported as
`PROVEN`. Every one of them is the same shape — the base run is honestly red,
the candidate run is a *forged pass*, and the judge reads only `post.status`.

Add one small, testable coherence predicate for execution evidence and apply it
at every point in `mini_ork/certify/oracle.py` where a `passed` status is
converted into proof. After the change, `run_default_audit()` on the shipped
judge must return `pass: true` with `missed: []` and all three controls green,
without weakening any real verdict.

## Scope

Edit only these files:

- `mini_ork/certify/evidence.py` (new) — the coherence predicate.
- `mini_ork/certify/oracle.py` — call it at the three trust points below.
- `tests/unit/test_certify_evidence_py.py` (new) — unit tests plus one
  integration test that runs the shipped judge through `run_default_audit()`.

Do not modify `mini_ork/gates/oracle_integrity.py`, its cases, or its expected
decisions. The audit is the acceptance test; the judge is the thing to fix.
Do not edit `mini_ork/runtime/engine.py` — `ExecOutcome` keeps its current
fields and the real runtime keeps setting no `meta`.

### The predicate

`mini_ork/certify/evidence.py` exposes:

```python
def defect(outcome) -> str | None
def is_trustworthy_pass(outcome) -> bool   # defect(outcome) is None
```

`defect()` returns `None` for any outcome whose status is not `"passed"` — a
failed/error/apply_fail outcome is already handled by the judge and must not be
re-labelled. For a claimed pass it returns a short reason string when the
outcome's own fields contradict the claim:

- `ran` is exactly `False` — the outcome says passed but reports it never ran.
- `returncode` is present, is not `None`, and is not `0`.
- `meta["apply_status"]` is present and not one of the success words
  (`ok`, `passed`, `pass`, `applied`, `clean`, `success`, `succeeded`).
- `meta["final_attempt_status"]` is present and not a success word.
- `meta["attempts"]` is a non-empty sequence and none of its entries is a
  success word (every attempt errored, yet the aggregate says passed).
- `meta["executed"]` is present, is an `int`, and is `0`.
- `meta["test_defect"]` is truthy.
- both `meta["input_digest"]` and `meta["candidate_input_digest"]` are present,
  non-empty strings, and differ (evidence for a different input was replayed).

Everything is defensive: a missing attribute, a non-mapping `meta`, an absent
key, or an unknown value never raises. Only a field the caller actually
populated can contradict the status, so a real `ExecOutcome` (which sets no
`meta` and always uses `returncode=0` on a pass) is never flagged. Keep the
module stdlib-only at import time, mirroring `mini_ork/gates/verifier_mutation.py`.

### The three trust points in `oracle.py`

1. **Term 2, the delta gate** (immediately after
   `post: ExecOutcome = runner.run_test(poc_src, patch=patch)`): if
   `post.status == "passed"` and `evidence.defect(post)` is not `None`, return
   `Verdict(UNVERIFIED, "<reason> — cannot judge", poc_plus=poc_src)`. A
   harness fact is an abstention, never `REFUTED` (never blame the patch).
2. **Term 2.5, hidden trials**: a trial counts as `measured` only when its
   status is `passed` *and* `evidence.defect(trial) is None`; an incoherent
   pass counts as `unmeasured` and must not trigger the identity-dependence
   `REFUTED` branch.
3. **Term 3, the metamorphic loop**: when an invariant's outcome claims
   `passed` but is defective, record `outcome: "unmeasured"` with the reason,
   count it in a new `unmeasured` counter, append the record, and `continue`
   without re-running it on base. `n_eff = holds + broken` keeps its meaning.
   Add `"unmeasured": unmeasured` to the returned `detail` dict. If any
   invariant was unmeasured and the computed verdict is `PROVEN`, withhold it
   (`UNVERIFIED`) with a reason naming how many could not be measured — this is
   a withhold-only move and can never create `PROVEN` or `REFUTED`.

Wire nothing else: relations, differential, and discrimination already receive
their outcomes through paths that start from the delta gate.

## Success criteria

Run from the worktree root, `/Volumes/docker-ssd/ps/mini-ork-worktrees/oracle-evidence`:

```bash
python3.11 -m pytest -q tests/unit/test_certify_evidence_py.py tests/unit/test_certify_oracle.py tests/unit/test_oracle_integrity_py.py tests/unit/test_instruments_cli_py.py
```

All green. The new file must contain a test that asserts the shipped judge now
passes the shipped audit:

```python
def test_shipped_judge_passes_the_shipped_audit():
    result = oracle_integrity.run_default_audit()
    assert result["controls_failed"] == []
    assert result["missed"] == []
    assert result["pass"] is True
```

and unit tests pinning each defect rule above, plus one asserting an honest
pass (`status="passed", ran=True, returncode=0, meta={}`) has `defect() is None`.

Then the real smoke, with no network and no `state.db`:

```bash
MINI_ORK_HOME=/tmp/mo-oracle-evidence-smoke python3 bin/mini-ork instruments oracle-integrity --json
```

must exit `0` and print `"pass": true`.

Also green:

```bash
python3.11 -m ruff check --select F,E9 --line-length 110 mini_ork/certify/evidence.py mini_ork/certify/oracle.py tests/unit/test_certify_evidence_py.py
```

## Constraints

- No new third-party dependency. No network, no model, no sandbox in any test.
- Withhold-only: the new predicate can turn `PROVEN` into `UNVERIFIED` and can
  turn a would-be proof step into an abstention. It must never emit `REFUTED`
  and must never create `PROVEN`. A runner that reports coherent evidence must
  produce byte-identical verdicts to today — pin that with a test comparing the
  honest path's verdict before/after semantics (the existing
  `test_three_of_three_invariants_hold_is_proven` must stay green unchanged).
- Fail closed on the audit: do not special-case case names, do not read
  `case.name`, and do not weaken an injection to make it pass.
- One diff. Do not commit. Do not touch `.mini-ork/`, `config/kernel-frozen.yaml`,
  `db/migrations/`, or any file outside the scope list.
