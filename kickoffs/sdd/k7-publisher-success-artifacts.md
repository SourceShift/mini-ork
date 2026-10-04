# K7 — recursive-validate-impl artifact contract must not demand failure artifacts on success

## Goal

A PASSING recursive-validate-impl run currently cannot publish/commit in some
installs: the artifact contract lists `reflector.json`/`replan.json` as
required outputs, but those only exist after a failed tier. Make
failure-path artifacts conditional (required only when a tier failed /
an iteration reflected).

## Evidence

sdd-10x child run-sdd10x-0931-c01-s09mm-d1 (2026-10-04): panel APPROVE,
tier4 4/4, tiers green — publisher refused; the outer per_spec_dispatcher
had to commit on the child's behalf (see worktree commit dfb9267e1's
trailer in the researcher repo).

## Definition of Done (probes)

```bash
python3 -m pytest -q tests/test_artifact_contract_conditional.py
```

## Hard rules

- NEVER run the repo-wide full pytest suite as a probe.
- Failure-path runs must still REQUIRE reflector.json/replan.json.

## Verification command

- `python3 -m pytest -q tests/test_artifact_contract_conditional.py`
