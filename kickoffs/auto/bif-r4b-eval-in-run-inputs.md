# R4b: the reviewer eval shows the reviewer what a real run shows it — verifier evidence included

## Goal

The first reviewer-eval score (fingerprint 382cb8d46be94da2) was 5/5
incidents caught and 0/6 false blocks. In their real runs, though, the same
reviewer PASSED eco6 K10, K12 and K13.

The difference is in the inputs:
- **In a real run**, the classic reviewer gets the block that
  `_assemble_reviewer_inputs(run_dir)` (`mini_ork/cli/execute.py` ~4057-4205)
  builds:
  - `implementer-summary.json`;
  - `verifier_test.json` / `verifier_typecheck.json`;
  - every other `verifier_*.json` (static check, spec tests, kickoff
    contract: all green on those incidents);
  - `verdict.json`;
  - the diff, framed by `--- Reviewer inputs (assembled by mini-ork-execute) ---` …
    `--- End reviewer inputs ---` and the REVIEWER NOTE.
- **The eval's `build_prompt`** (`mini_ork/review/reviewer_eval.py`) sends only
  `# Reviewer inputs\n# review-diff.patch\n<diff>`.

A reviewer that anchors on green verifiers is exactly the failure the eval
must measure. Make the eval send the same block, built from evidence stored
with each case.

`mini_ork/cli/execute.py` is held by another live worktree, so this change
must NOT edit it. Instead, the eval carries its own renderer, and a drift test
pins it byte-for-byte to the real `_assemble_reviewer_inputs` output on a
fixture run.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/review/reviewer_eval.py`

1. **`EVIDENCE_DIR = "evidence"`.** A case may hold `evidence/` with any of:
   `implementer-summary.json`, `verifier_*.json`, `verdict.json`,
   `pre-retirement-parity.json`, `integration-map.json`,
   `static-feature-ledger.json`, `self-migrate.diff`.
2. **`render_reviewer_inputs(evidence_dir: Path, diff_text: str) -> str`**
   reproduces the block `_assemble_reviewer_inputs` builds:
   - the same section order;
   - the same `_sec` rule: `"\n# <name>\n<cap_block(text, label=name)>\n"`
     when the file exists and is non-empty, else `"\n# <name>\n(not available)\n"`;
   - the same fixed-verifier names (`verifier_test.json`,
     `verifier_typecheck.json`, sorted), then the other `verifier_*.json`
     sorted, then the optional names;
   - `# review-diff.patch` with `cap_block(diff, label="review-diff.patch")`,
     or `(no diff)` when the diff is empty;
   - the same end marker and REVIEWER NOTE text.
   Use `mini_ork.context_assembler.cap_block`. The no-op branch never applies
   to eval cases.
3. **`build_prompt`.** Replace the
   `"\n\n# Reviewer inputs\n# review-diff.patch\n" + capped diff` part with
   `"\n\n" + render_reviewer_inputs(case_dir / EVIDENCE_DIR, diff)`.
   Everything else is unchanged.
4. **`fingerprint`.** Add `"eval_inputs": 2` to the hashed object, so scores
   taken with the old inputs are never compared with new ones (`status` and
   `garden` will report the reviewer as un-scored until the next
   `reviewer-eval run`).

### 2. `mini_ork/cli/reviewer_eval_cmd.py`

1. **`add-case --run RUN_ID`** also copies, from `<home>/runs/<RUN_ID>/`, the
   evidence files listed in §1.1 that exist into `<case>/evidence/`.
2. **New subcommand `refresh-evidence [--case ID]...`.** For every case (or
   the given ones) whose `source.run` is set and whose run dir exists,
   (re)copy the evidence files. Print
   `<id>: <n> evidence file(s)` / `<id>: no run dir` / `<id>: commit case, no evidence`.
   rc 0.
3. Add it to the parser's help and to `_HELP`'s `reviewer-eval [...]` usage
   line if that line lists subcommands. Check `mini_ork/cli/main.py`: if
   changing the line would need an edit there, skip it; do not edit
   `main.py`.

## Files in scope (touch ONLY these)

- `mini_ork/review/reviewer_eval.py`
- `mini_ork/cli/reviewer_eval_cmd.py`
- `tests/unit/test_reviewer_eval_inputs.py` (new)

Do NOT modify `mini_ork/cli/execute.py`, `mini_ork/cli/main.py`,
`evals/reviewer/` (the evidence backfill is an operator step after merge), or
any path in `config/kernel-frozen.yaml`. Every existing test must pass
unchanged except where it asserts the exact old fingerprint id or the old
`# Reviewer inputs\n# review-diff.patch` prompt fragment. If so, update only
that assertion in `tests/unit/test_reviewer_eval.py`, and list it in the
implementer summary.

## Tests (`tests/unit/test_reviewer_eval_inputs.py`, hermetic, no LLM)

1. **Drift test.** Build a real fixture run:
   - a tmp git repo (one file, committed) as the worktree, and a tmp run dir;
   - in the run dir, `pre-implementer-ref` = the base commit,
     `implementer-summary.json` = `{"worktree_path": <repo>, "files_changed": ["a.py"]}`,
     `verifier_test.json`, `verifier_static-check.json` and `verdict.json`
     with small JSON;
   - then edit `a.py` in the worktree.
   Call the real `mini_ork.cli.execute._assemble_reviewer_inputs(str(run_dir))`.
   Copy the same evidence files into an evidence dir, and read
   `<run_dir>/review-diff.patch` as the diff. `render_reviewer_inputs(evidence_dir, diff)`
   must equal the real block exactly.
2. With an empty evidence dir: the block contains
   `# implementer-summary.json\n(not available)` and `# review-diff.patch`
   with the diff.
3. `build_prompt` for a case with evidence contains `--- Reviewer inputs (assembled by mini-ork-execute) ---`,
   the verifier JSON text, and `--- End reviewer inputs ---`.
4. The fingerprint id differs from the same tree's id computed without
   `eval_inputs` (patch the dict construction or compare against a recorded
   old id).
5. `add-case --run <id>` with a tmp home whose run dir holds
   `verifier_x.json` → `<case>/evidence/verifier_x.json` exists.
6. `refresh-evidence` prints the three outcomes for a run case, a commit case,
   and a run case whose run dir is gone.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_reviewer_eval_inputs.py tests/unit/test_reviewer_eval.py tests/unit/test_reviewer_diff_scope_py.py tests/unit/test_reviewer_no_reread.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_reviewer_eval_inputs.py --junitxml="${MINI_ORK_RUN_DIR}/bif-r4b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-r4b-junit.xml`

## Success criteria

- The verification command passes.
- `ruff check --select F,E9 mini_ork/review/reviewer_eval.py mini_ork/cli/reviewer_eval_cmd.py tests/unit/test_reviewer_eval_inputs.py` is clean.
- For any case built from a run, the eval's reviewer inputs are
  byte-identical to what that run's reviewer would have been given.
