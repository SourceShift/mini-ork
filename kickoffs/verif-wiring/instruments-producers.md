# Give the three unwired verification instruments a producer

## Goal

Three instruments in `docs/architecture/verification-stack.md` are consumed but never
produced: ⑦ oracle integrity ("nothing writes it"), ⑩ verifier mutation ("nothing writes
scores"), and ⑧ judge validity (off, with no way to measure before enforcing). Deliver a
single new subcommand, `mini-ork instruments`, that writes the ⑦ and ⑩ records at the exact
paths their consumers already read, and add a `shadow` mode to `MO_JUDGE_VALIDITY` that
measures and records without ever quarantining a promotion.

Nothing in this kickoff changes a verdict path by default: the new verb is opt-in, and
`shadow` is a new value of an already-off knob.

## Scope

Edit only these files:

- `mini_ork/cli/instruments.py` (new) — the `instruments` subcommand.
- `mini_ork/cli/main.py` — register `"instruments": "mini_ork.cli.instruments"` in
  `_NATIVE_MODULE_SUBS` (the table near line 60) and add one `_HELP` line.
- `mini_ork/gates/judge_validity.py` — add a `mode()` returning
  `"off" | "shadow" | "enforce"`, keeping `enabled()` as `mode() != "off"` for existing
  callers.
- `mini_ork/gates/promotion_gate.py` — in the judge-validity block near line 481, when the
  mode is `shadow`, run the audit and record it in the result dict, but never set
  `promoted = "quarantined"`.
- `tests/unit/test_native_dispatch_py.py` — add `"instruments"` to the pinned expected set.
- `tests/unit/test_instruments_cli_py.py` (new) — the verb's behaviour.
- `tests/unit/test_judge_validity_py.py` — shadow records and does not quarantine.
- `docs/architecture/verification-stack.md` — update the rows for ⑦, ⑧ and ⑩.

Do not modify `mini_ork/gates/oracle_integrity.py`, `mini_ork/gates/verifier_mutation.py`
or `mini_ork/verify/catalog.py`. Their public producers (`write_latest`, `write_score`,
`read_score`, `admit`) already exist and are correct; this task only adds callers.

### The command

```
mini-ork instruments oracle-integrity [--db PATH] [--json]
mini-ork instruments verifier-mutation --observable FILE [--card FILE] [--json]
```

`--db` defaults exactly as `mini_ork/context.py` resolves it: `MINI_ORK_DB`, else
`$MINI_ORK_HOME/state.db`, else the process-default home. Never invent a third fallback.

**`instruments oracle-integrity`** — call `oracle_integrity.run_default_audit()` (hermetic:
no network, no model, no sandbox), then `oracle_integrity.write_latest(db, result)`. Print
`pass`, `missed`, `controls_failed` and the written path. Exit codes: `0` when
`result["pass"] is True`, `1` when it is `False`, `2` when the audit could not run
(`RuntimeError` — the real judge or `ExecOutcome` was unavailable). A missing runtime is
exit 2, never a stub result and never exit 0.

**`instruments verifier-mutation`** — read the observable JSON file, call
`verifier_mutation.score(observable)`, and:
- with `--card`, write the sidecar via `verifier_mutation.write_score(card_path, result)`
  and print the sidecar path;
- without `--card`, print the score only and write nothing.

Exit codes: `0` when the score is `measured` and `admit(kill_score)` is true, `1` when it
is measured and admitted false, `2` when it is not measured (baseline not PROVEN) or the
observable file is unreadable/invalid. Unmeasured is exit 2 — distinct from a low score,
because a card with no measurement must never look like a failing one.

### Judge-validity shadow

`MO_JUDGE_VALIDITY` gains the value `shadow` (also accept `Shadow`/`SHADOW`). Semantics:

- `off` (unset, `0`, `false`, `no`) — today's behaviour, byte-identical result dict.
- `shadow` — run `promotion_check(db_path)`, write the envelopes, attach the audit under
  `result["judge_validity"]` with `"mode": "shadow"`, and **never** quarantine. A below-floor
  audit in shadow must still leave `promoted` exactly as it was.
- `enforce` (`1`, `true`, any other non-off value) — today's quarantine path.

`enabled()` keeps its current meaning for any other caller (`mode() != "off"`), so the
existing call sites do not need to change.

## Success criteria

Run from the worktree root, `/Volumes/docker-ssd/ps/mini-ork-worktrees/verif-wiring`:

```bash
python3.11 -m pytest -q tests/unit/test_instruments_cli_py.py tests/unit/test_judge_validity_py.py tests/unit/test_native_dispatch_py.py tests/unit/test_oracle_integrity_py.py tests/unit/test_verifier_mutation_py.py
```

All green. Then, with no network and no `state.db` present, in a temp `MINI_ORK_HOME`:

```bash
MINI_ORK_HOME=/tmp/mo-inst-smoke python3 bin/mini-ork instruments oracle-integrity --json
```

must exit 0 and leave `/tmp/mo-inst-smoke/oracle-integrity/latest.json` containing
`"pass": true`. A second run must overwrite it atomically (`.tmp` then `os.replace`), never
append and never leave a torn file.

And the shadow proof must be a test, not a comment: promote the same fixture twice, once
with `MO_JUDGE_VALIDITY=shadow` and once with `MO_JUDGE_VALIDITY=1`, over history whose
agreement is below the floor; the shadow result must be `promoted` with a
`judge_validity.mode == "shadow"` block present, and the enforce result must be
`quarantined`.

Also green:

```bash
ruff check --select F,E9 --line-length 110 mini_ork/cli/instruments.py mini_ork/cli/main.py mini_ork/gates/judge_validity.py mini_ork/gates/promotion_gate.py tests/unit/test_instruments_cli_py.py
```

## Constraints

- No new third-party dependency. `mini_ork/cli/instruments.py` imports stdlib at module
  scope and `mini_ork.*` only function-locally, matching `oracle_integrity.py`.
- Fail closed. A missing DB path, a missing observable file, an unparseable JSON and a
  `RuntimeError` from the audit are each a distinct non-zero exit with a message naming the
  cause. Never write a passing record you did not measure, and never treat "could not
  measure" as "passed".
- No behaviour change when the verb is not invoked and `MO_JUDGE_VALIDITY` is unset.
- Do not edit `.mini-ork/`, `config/kernel-frozen.yaml`, or anything under `db/migrations/`.
- One diff. Do not commit.
- `docs/architecture/verification-stack.md`: ⑦ and ⑩ become "consumer on, producer manual
  (`mini-ork instruments …`)"; ⑧ becomes "off by default; `shadow` measures without
  enforcing". Keep the table's existing column shape and the dated header line, and add the
  new verb to the document's prose where a reader would look for how to produce a record.
