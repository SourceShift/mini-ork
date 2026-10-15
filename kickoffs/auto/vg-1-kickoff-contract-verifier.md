# VG-1 kickoff-contract verifier for framework-edit (verifier gap F1 + F2)

## Goal

A framework-edit run must fail when it does not deliver what its kickoff
declared. Today it can pass without it. Evidence from four SDE runs on
2026-10-13/14 (`sde-1-diagnose-20261013-232707`, `sde-2-kernel-20261013-232708`,
`sde-2-kernel-20261014-000931`): each kickoff had a "Required artifacts" block
(`diagnosis-replay.json`, `kernel-refusal.json`) and a "Self-application
measurement" command; none of those artifacts was produced, and every run ended
`verdict: pass`. Cause: `overlay_plan` (`mini_ork/planning/recipe_plan.py`)
replaces the plan's outputs with the recipe's own contract, so kickoff-declared
artifacts never reach `plan.json`; and nothing runs the measurement command —
the implementer is trusted to. The measurement is what exposed a real bug the
verifiers missed (pass 1 of SDE-1 labelled interrupted runs as engine faults).

This adds one verifier node to the framework-edit recipe. It does not touch
`execute.py` or the plan overlay.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New `recipes/framework-edit/verifiers/kickoff-contract.py`, following the
   conventions of the sibling verifiers (`static-check.py`, `test.py`: same env
   inputs `MINI_ORK_RUN_DIR` / `MO_TARGET_CWD` / `MINI_ORK_ROOT`, an evidence log
   in the run dir, a `verifier_kickoff-contract.json`
   `{"verifier", "pass", "evidence_path", "measures": [...], "artifacts": {...}}`,
   exit 0 on pass, 1 on fail). It does NOT write or change `verdict.json`.
2. **Kickoff source:** the pre-run snapshot,
   `mini_ork.context_v2.load_pack(RUN_DIR).get("kickoff_snapshot")`; only when
   there is no pack/snapshot, fall back to the file at `run_profile.json`'s
   `kickoff_path`. Never read the live kickoff file when a snapshot exists.
3. **Auto-opened kickoffs:** if the snapshot's FIRST line is
   `<!-- mini-ork:origin=auto-triage -->`, execute nothing; pass with
   `"note": "auto-triage kickoff: no operator contract"`. (Those kickoffs embed
   raw log excerpts; a log line must never become an executed command.)
4. **Measure blocks:** under a heading matching
   `^#{2,4}\s*Self-application measurement` (case-insensitive) up to the next
   heading of the same or higher level, run each fenced block whose info string
   is exactly `measure` (```` ```measure ````). Each non-empty, non-`#` line is one
   command, run with `bash -c`, cwd = `MO_TARGET_CWD` (fallback `MINI_ORK_ROOT`),
   env passes `MINI_ORK_RUN_DIR` / `MINI_ORK_HOME` through, timeout
   `MO_KICKOFF_MEASURE_TIMEOUT_S` (default 900). Any non-zero rc or timeout →
   fail. Inline backtick commands in prose are never executed — measure blocks
   are opt-in, so existing kickoffs keep working.
5. **Required artifacts:** under a heading matching
   `^#{2,4}\s*Required artifacts` (case-insensitive, any suffix), collect every
   backtick-quoted token on a bullet line that starts with `${MINI_ORK_RUN_DIR}/`;
   expand against `MINI_ORK_RUN_DIR`. After the measures ran, each must exist and
   be non-empty, else fail naming the missing paths. Tokens not under
   `${MINI_ORK_RUN_DIR}/` are ignored (run-local only).
6. No measure block and no required-artifacts block → pass with
   `"note": "no kickoff contract declared"`.
7. `--no-measure` CLI flag: skip step 4 (artifact check only) — used to replay
   the verifier against old run dirs without executing anything.
8. **`recipes/framework-edit/workflow.yaml`:** add node
   `kickoff_contract_verifier` (`type: verifier`, `model_lane: verifier`,
   `prompt_ref: null`, `verifier_ref: verifiers/kickoff-contract.py`,
   `dispatch_mode: serial`) with edges mirroring `test_verifier`:
   `implementer → kickoff_contract_verifier (verifies)`,
   `kickoff_contract_verifier → reviewer (depends_on)`,
   `kickoff_contract_verifier → rollback (escalates_to)`,
   `kickoff_contract_verifier → implementer (retries, max_rounds: 2)`.

## Files in scope

- recipes/framework-edit/verifiers/kickoff-contract.py (new)
- recipes/framework-edit/workflow.yaml
- tests/unit/test_framework_edit_kickoff_contract.py (new)

## Tests (`tests/unit/test_framework_edit_kickoff_contract.py`, hermetic: temp run dir with `context-pack.v2.json` + `run_profile.json`, temp target dir; run the script as a subprocess like `tests/unit/test_framework_edit_static_check.py` does)

- required artifact present and non-empty → pass; missing → fail, path named;
  zero-byte → fail;
- a measure block whose command writes the required artifact → pass; a measure
  command exiting 1 → fail; a command exceeding a 2 s timeout → fail;
- auto-triage marker on the snapshot's first line → a measure command that would
  create a sentinel file is NOT run (sentinel absent), verifier passes with the note;
- an inline backtick command in prose (no fence) is not run;
- snapshot has no measure block but the live kickoff file was edited to add one →
  not run (snapshot wins);
- no contract sections → pass with the note;
- `--no-measure` skips commands but still fails on a missing artifact;
- `workflow.yaml` compiles (`mini_ork.workflow.compiler.compile_workflow`) and
  contains the node and its four edges.

Also run `tests/unit/test_framework_edit_static_check.py tests/unit/test_framework_edit_verifier_target_root.py`.

## Self-application measurement

Replay the verifier with `--no-measure` against the finished SDE run dirs
(read-only; set `MINI_ORK_RUN_DIR` to each):
`/Volumes/docker-ssd/ps/mini-ork/.mini-ork/runs/sde-1-diagnose-20261013-232707`,
`.../sde-2-kernel-20261013-232708`, `.../sde-2-kernel-20261014-000931` must FAIL
(missing `diagnosis-replay.json` / `kernel-refusal.json`), and
`.../sde-1-diagnose-20261014-000929` must PASS (its `diagnosis-replay.json` exists).

```measure
python3 recipes/framework-edit/verifiers/kickoff-contract.py --replay-check /Volumes/docker-ssd/ps/mini-ork/.mini-ork/runs > "${MINI_ORK_RUN_DIR}/kickoff-contract-replay.json"
```

Implement `--replay-check <runs_dir>` as: run the `--no-measure` check for the
four run ids above, print `{"<run_id>": {"pass": bool, "missing": [...]}}`, exit 0
iff the three pass-1/SDE-2 runs fail and `sde-1-diagnose-20261014-000929` passes.

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/kickoff-contract-replay.json`

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_framework_edit_kickoff_contract.py tests/unit/test_framework_edit_static_check.py tests/unit/test_framework_edit_verifier_target_root.py` passes.
- Kickoffs without the two sections behave exactly as before.

## Rules

Edit files directly; do not emit unified diffs. Do not touch execute.py,
execute_handlers.py, planning/, static-check.py, test.py, or verdict.json logic.
