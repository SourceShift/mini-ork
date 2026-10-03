# K3 — spec-driven-delivery verifiers (replace stubs with real gates)

Design doc (authoritative, read first):
`/Users/admin/ps/mini-ork-sdd-wt/docs/plans/2026-10-03-spec-driven-delivery.md`
Prerequisites (already on this branch): K1 `mini_ork/specdir/`, K2 recipe
scaffold with stub verifiers under `recipes/spec-driven-delivery/verifiers/`.

## Goal

Implement the eight verifiers as deterministic Python (stdlib + jsonschema +
yaml only), each reading `${MINI_ORK_RUN_DIR}` artifacts and emitting a single
JSON verdict line `{"pass": bool, "reason": str, ...detail}` with exit 0 on
pass, 1 on fail, 2 on malformed input. Add pytest coverage with synthetic run
dirs.

## Feature scope

- `specdir-ingest.py` — shell out to `bin/mini-ork specs ingest <spec_dir>
  --out ${MINI_ORK_RUN_DIR}/spec-index.json`; pass iff exit 0, index validates
  against `schemas/spec-index.schema.json`, and spec count ≥
  `MO_SDD_MIN_SPECS` (default 1). The spec dir comes from the kickoff's
  `## Spec dir:` line (read `${MINI_ORK_KICKOFF}` env or the run's kickoff
  copy — follow how sibling verifiers locate kickoff metadata).
- `spec-lint.py` — run `mini_ork.specdir.lint` over the indexed specs; fail on
  any error-severity finding; write findings to
  `${MINI_ORK_RUN_DIR}/spec-lint.json`.
- `ratification-check.py` — for each `spec-cards/<spec_id>.json`: validate
  against `schemas/spec-card.schema.json`; verify `source_hash` still matches
  the source file bytes (spec drift = fail); verify every acceptance has
  exactly one gate and every deliverable's `acceptance_refs` resolve; verify
  the card's `ratification` list is empty OR every entry carries an
  `acknowledged: true` flag (unacknowledged uncovered requirement = fail).
- `test-validity.py` — for each gate of kind `cmd` in `gates/<spec_id>.json`:
  run the probe on the CURRENT tree with timeout `MO_SDD_SMOKE_TIMEOUT_S`
  (default 120); a probe tagged `precondition` must PASS now; any other probe
  that PASSES on the untouched tree is vacuous → fail (broken-baseline
  discipline). Static vacuity checks for all kinds: probe nonempty, not
  `true`/`exit 0`/`:`; `expect` nonempty. Results to
  `${MINI_ORK_RUN_DIR}/test-validity.json`.
- `dispatch-aggregator.py` — read `dispatch-results.json`; recompute
  `{total, delivered, failed, blocked, pending, pass_rate, deliverables[]}`
  into `aggregate-verdict.json`; verify every non-blocked deliverable has a
  child run id and a child verdict sourced from the child run's artifacts
  (not from the dispatcher's own claims — cross-check the referenced child
  run dir exists and contains a non-empty verdict artifact); blocked
  deliverables must each have an ASK artifact.
- `smoke-live.py` — execute every acceptance gate: kind `cmd` → subprocess
  with `MO_SDD_SURFACE_ENV` dotenv loaded, timeout enforced, pass iff exit 0
  AND stdout/stderr matches `expect` when `expect` is nonempty; kind `ui` →
  substitute the probe into `MO_SDD_UI_PROBE_CMD` template if set, else mark
  the gate `WAIVED` (not passed); kind `contract` → run probe like cmd.
  Atomic per-gate verdicts, no partial credit: node passes iff zero FAILED
  gates and WAIVED count is recorded. Results to
  `${MINI_ORK_RUN_DIR}/smoke-live.json`.
- `ui-craft-gate.py` — only for SpecCards with `ui_craft.required: true`: run
  `MO_SDD_UI_GATE_CMD` (template vars `{spec_id}` `{design_sources}`
  `{run_dir}`); empty env ⇒ emit WAIVED verdict (pass=true with
  `"waived": true` so the ledger's slide-back gauge counts it); nonzero exit
  ⇒ fail.
- `ledger-writer.py` — append `ledger.jsonl` rows
  `{ts, spec_id, clause_id, deliverable_id, child_run_id, commit, gate_id,
  verdict, waived}` from the artifacts above; compute slide-back gauges
  `{waived_gates, failed_gates, asks_open}` into
  `${MINI_ORK_RUN_DIR}/slide-back.json`; fail only on missing/corrupt inputs.
- Tests `tests/test_sdd_verifiers.py` — synthetic `${MINI_ORK_RUN_DIR}` trees
  exercising: pass path, spec-drift fail, unacknowledged ratification fail,
  vacuous-probe fail, aggregator catching a dispatcher claiming a verdict
  with no child artifacts, smoke expect-mismatch fail, ui-craft waived path,
  ledger gauge counts.

## Definition of Done (probes)

```bash
# P1: all eight verifiers are executable, non-stub
for v in recipes/spec-driven-delivery/verifiers/*.py; do grep -L NOT_IMPLEMENTED "$v" >/dev/null || { echo "stub remains: $v"; exit 1; }; done; echo no-stubs

# P2: verifier tests pass
python3 -m pytest -q tests/test_sdd_verifiers.py

# P3: adjacent suites still green (scoped; the full suite is flaky under concurrent campaigns)
python3 -m pytest -q tests/test_specdir_ingest.py tests/test_sdd_verifiers.py
```

## Hard rules

- Verifiers are deterministic: no LLM calls, no network beyond what a smoke
  probe command itself does.
- A verifier never trusts an LLM node's self-report — recompute from child
  artifacts (this is the authoritative-verdict rule).
- Timeouts on every subprocess; a hung probe is a FAIL with reason timeout,
  never a hang.
- Do not weaken K2 workflow edges or prompts; only replace stub bodies and
  add tests.

## Success command

```bash
python3 -m pytest -q tests/test_sdd_verifiers.py
```

## Verification command

- `python3 -m pytest -q tests/test_sdd_verifiers.py`

## Expected outputs

- `${MINI_ORK_RUN_DIR}/implementer-summary.json`
- tier evidence logs per recursive-validate-impl contract
