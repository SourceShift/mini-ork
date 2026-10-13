# SDE-1 repair — diagnosis contract conformance + note rules

## Goal

Pass 1 (commit `9b8ed116`, `mini_ork/recovery/diagnose.py`) diagnoses the
motivating run correctly but drifted from the contract the next step routes on.
The next step (SDE-3) opens framework-edit fix epics automatically from
`layer == "engine"` diagnoses, so a wrong `engine` label now costs a fix epic.
A live read-only replay over the 50 most recent failed runs also left 54%
`unclassified`, mostly runs whose cause is plainly written in `task_runs.notes`.

All paths are relative to the repository root.

## Defects to fix (exact)

1. **R6 mislabels interruptions as engine faults.** "A node started and never
   ended" is `engine / execute / resume` today. Change: a run whose
   `task_runs.notes` contains a `killed-by-user` segment (notes are
   `"; "`-joined, test per segment, as `auto_repair.decide` does) → layer
   `user`, `routes_to: none`. Any other started-never-ended node → layer
   `provider`, `routes_to: retry`. Never `engine`.
2. **R1 is too wide.** `_ABSTAIN_FINISH` makes `levels_unverified` / `abstain`
   publisher finishes engine faults. Change: R1 matches only
   `finish_reason == "publish_abstain"` AND the run-level report has
   `levels_ok` true → `engine / deliver / publisher`, `routes_to: fix_epic`.
   A publisher that withheld with `levels_ok` false (or `levels_unverified`) →
   new rule, layer `change`, stage `verify`, `routes_to: reverify`.
3. **Contract fields.**
   - `source` is the literal `"rule"` for every rule result and `""` for the
     unclassified fall-through. The cited file stays in `evidence[].ref`.
   - Rule `confidence` keeps its per-rule prior but the docstring states it is
     the rule's prior, not a model claim.
   - Evidence items use the key `excerpt` (not `detail`).
   - `layer` ∈ `change | model | provider | environment | engine | kickoff | user | unclassified`.
     Rename `operator` → `environment` (R7 owner-only fixes → `routes_to: owner`).
     Retry-hint `needs_change.kind == "lane"` → layer `model`, `routes_to: swap_lane`
     (add this rule if absent).
   - `routes_to` ∈ `revise | reverify | swap_lane | retry | owner | fix_epic | kickoff_repair | none | human`.
     Replace `resume` with `retry` and `framework-edit` with `fix_epic`.
4. **Note rules (new, evaluated after the artifact/event rules, before the
   fall-through)**, each citing the `task_runs.notes` segment as evidence
   `{"kind": "note", "ref": "task_runs.notes", "excerpt": <segment>}`:
   - `reaped:` segment → `provider / <stage of the last node_start without a node_end, else "unknown"> / retry`;
   - `planner_dispatch_rc_<N>` → `provider / plan / planner / retry`;
   - `lifecycle exited on an exception` → `engine` ONLY when R2's traceback
     evidence also exists for the run; otherwise leave it `unclassified`.
   - `lifecycle exited without a verdict` stays `unclassified` (a model
     diagnoser handles it in SDE-3) — do not guess.
5. **Replay output** rows include `source` and `evidence` (not only the
   summary fields), and `--replay --out <path>` writes the JSON to `<path>`.

## Files in scope

- mini_ork/recovery/diagnose.py
- tests/unit/test_diagnose.py

Do NOT touch auto_repair.py or retry_hint.py in this repair.

## Tests (extend `tests/unit/test_diagnose.py`, hermetic)

- interrupted node + `killed-by-user` note → `user / none`; without the note →
  `provider / retry`; neither is ever `engine`;
- publisher `publish_abstain` + levels_ok true → `engine / fix_epic`;
  publisher `levels_unverified` (levels_ok false) → `change / verify / reverify`;
- every rule result has `source == "rule"`; fall-through has `source == ""`;
  evidence items carry `excerpt`;
- `reaped:` and `planner_dispatch_rc_43` notes classify as `provider / retry`;
- `lifecycle exited without a verdict` alone stays `unclassified`;
- all `layer` / `routes_to` values across the suite are inside the enums above.

## Self-application measurement

`MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork python3 -m mini_ork.recovery.diagnose --replay --limit 50 --json --no-write --out ${MINI_ORK_RUN_DIR}/diagnosis-replay.json`
Accepted iff `run-1791914481-48337` is `engine/deliver/publisher` with
`routes_to: fix_epic`, no row is `engine` without traceback or publish-abstain
evidence, and the unclassified share drops below the pass-1 54%.

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/diagnosis-replay.json`

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_diagnose.py` passes.

## Rules

Edit files directly; do not emit unified diffs.
