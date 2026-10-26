# rev6 · R5a: the review loop converges — one verdict contract, findings with ids, review of the delta

## Goal

Measured on main, 2026-10-26:

1. **Two contracts conflict, and findings are lost.**
   - The classic reviewer's runtime envelope (`mini_ork/cli/execute_handlers.py`
     `_handle_reviewer`, the `else:` branch around line 2203) asks for
     `{"verdict": "pass|fail|needs_revision|escalate", "notes": []}`.
   - `recipes/framework-edit/prompts/reviewer.md` asks for
     `approve|revise|reject` plus a `findings` array.
   - Stored reviews follow the runtime and carry no `findings`.
     `_revise_failure_section` (`mini_ork/cli/execute.py` ~5165) forwards only
     `verdict` + `notes`/`reasons`, so the implementer never receives
     file/line findings.
2. **The reviewer re-reads everything each round.** `_assemble_reviewer_inputs`
   (`execute.py:3878`) always diffs against `pre-implementer-ref`.
   - The previous round's `review-diff.patch` is archived to `revise/round-N/`
     and never used again.
   - Findings have no ids, so neither "was F1 fixed?" nor "do not re-report"
     can be stated.
3. Implementer attempts per framework-edit run (`node_attempts`): 1→273, 2→78,
   3→32, 4→4, 5→4, 6→2.

This kickoff makes every classic reviewer (any `type: reviewer` node that is not
a panel gate or synthesizer) emit one contract, keeps a per-run findings ledger
with runtime-assigned ids, shows the reviewer what changed since its last
review, and gives the implementer structured, carried-over findings.

The round-aware severity block (`_reviewer_round_block`, `MO_REVIEW_ROUND_AWARE`)
is unchanged. The new behaviour is on by default and `MO_REVIEW_CONVERGE=off`
turns off the prior-findings and delta blocks (the contract fix and the ledger
stay on: they are bug fixes).

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/workflow/review_ledger.py` (new; stdlib only)

```
LEDGER_REL = "revise/findings.json"
def attempt_no(run_dir: str) -> int
def load(run_dir: str) -> dict
def record_review(run_dir: str, attempt: int, review: dict,
                  delta_files: list[str] | None = None, now: int | None = None) -> dict
def prior_block(run_dir: str) -> str
def feedback_lines(run_dir: str, attempt: int) -> list[str]
```

1. **`attempt_no`**: `revise/current.json` `round + 1` if that file parses,
   else 1.
2. **`load`** returns `{"findings": [], "rounds": []}` when the file is missing
   or unreadable.
3. **`record_review`** reads `review["findings"]` (a list of dicts; anything else
   is `[]`) and `review["resolved"]` (a list of strings; else `[]`). It then:
   - **Matches findings to prior ids.** A finding whose `id` equals an existing
     ledger id is a **re-report**: that entry stays `open` and gets
     `last_seen = attempt` and the new `severity`, `line` and `instances`.
     Any other finding is **new**: give it id `F<k>` (k = number of ledger
     findings + 1), `status = "open"`, `first_seen = last_seen = attempt`.
     Copy `file`, `line`, `severity` (lowercased; anything outside
     high/medium/low becomes `medium`), `issue` (≤ 400 chars) and `instances`
     (a list of `{file, line}`; default `[]`).
   - **Marks regressions and out-of-delta findings.** A new finding whose
     `(file, normalized issue)` equals a `fixed` entry gets `reopened: true`.
     Normalized means lowercased and runs of non-alphanumerics collapsed to
     one space. When `delta_files` is not `None` and a new finding's `file` is
     not in it, the finding gets `outside_delta: true`.
   - **Marks fixes.** Every id in `resolved` that names an `open` finding not
     re-reported in this review becomes `status = "fixed"`, `fixed_in = attempt`.
   - **Appends a round summary** to `rounds`:
     `{"attempt", "verdict": str(review.get("verdict", "")), "new", "reported_again", "fixed", "still_open", "outside_delta", "reopened", "at"}`.
     The counts cover this review; `still_open` is the number of ledger
     findings left `open`.
   - **Writes atomically** (tmp + `os.replace`) and returns the ledger.
   - Rewrite `review["findings"][i]["id"]` in place to the assigned id, so the
     caller can persist it.
4. **`prior_block`**: `""` when the ledger has no findings. Otherwise a block
   that starts `## Your earlier findings (ids are stable across rounds)`, then:
   - one line per `open` finding: `- [F1] high path.py:42 — issue`;
   - then `Already fixed (do not report again unless the new changes broke it):`
     with the same line format for `fixed` findings.
   Cap at 40 lines.
5. **`feedback_lines(run_dir, attempt)`**: for each `open` finding:
   `- [F1] high path.py:42 — issue`, plus `(also at a.py:3, b.py:9)` when it has
   instances. A finding with `last_seen - first_seen >= 1` (reported in two or
   more reviews) gets the suffix
   ` — STILL OPEN after <n> reviews: the earlier fix did not work; change the approach for this finding instead of patching the same lines again.`

### 2. Review-tree snapshots and the delta (`mini_ork/cli/execute.py`)

1. Add `_snapshot_review_tree(worktree: str, run_dir: str, specs: list[str]) -> str`.
   With `GIT_INDEX_FILE=<abs run_dir>/.review-index` in the subprocess env only,
   it runs `git -C <worktree> read-tree HEAD`, then
   `git -C <worktree> add -A -- <specs or ".">`, then
   `git -C <worktree> write-tree`. It writes the tree sha to
   `<run_dir>/review-tree.txt` and returns it.
   - On any failure it returns `""` and writes nothing.
   - It then removes the temp index file.
   - It never touches HEAD, the real index, refs or the stash.
2. `_assemble_reviewer_inputs` calls it right after `review-diff.patch` is
   written, when the worktree is a git work tree.
3. `_archive_revise_round` also moves `review-tree.txt` and `review-delta.patch`
   (when present) into `revise/round-<n>/`.
4. Add `_review_delta(run_dir: str, worktree: str, specs: list[str]) -> tuple[str, list[str]] | None`.
   - `prev` = the `review-tree.txt` in the highest-numbered `revise/round-<n>/`
     that has one; `curr` = `<run_dir>/review-tree.txt`.
   - Return `None` when either is missing or empty.
   - Otherwise write `git -C <worktree> diff --no-color <prev> <curr> -- <specs>`
     to `<run_dir>/review-delta.patch` and return
     `(patch_text, sorted names from git diff --name-only <prev> <curr> -- <specs>)`.
   - Any git failure → `None`.

### 3. Reviewer prompt (`mini_ork/cli/execute_handlers.py`, classic branch only)

1. Add a module-level `def reviewer_envelope(review_file: str) -> str` that
   returns the JSON instructions. They replace today's
   `'Respond with JSON: {"verdict": ..., "notes": []}'` sentence, and keep the
   existing `escalate` sentence and the
   "AND write that exact JSON object to {review_file} …" sentence verbatim:
   ```
   Respond with JSON: {"verdict": "pass|fail|needs_revision|escalate", "notes": [], "findings": [], "resolved": []}
   Each finding: {"id": "<an id from 'Your earlier findings' when you report one of those again, else empty>", "file": "<repo-relative path>", "line": <int>, "severity": "high|medium|low", "issue": "<one sentence: the defect and why it matters>", "instances": [{"file": "<path>", "line": <int>}]}
   needs_revision and fail MUST carry at least one finding. When one defect pattern appears in several places, report it ONCE and list every other location in instances.
   ```
   `_handle_reviewer` uses `reviewer_envelope(review_file)` in place of the
   inline strings. The panel-gate and synth branches are unchanged.
2. When `MO_REVIEW_CONVERGE` is not `off` and
   `review_ledger.prior_block(run_dir)` is non-empty, insert this text after the
   round block and before the envelope:
   - the prior block;
   - when `_review_delta(...)` returns a result, `## Changes since your last review`
     followed by the delta patch through `cap_block(..., label="review-delta.patch")`;
   - these rules, verbatim:
     ```
     Rules for this round:
     - Re-review ONLY the changes since your last review; the full diff above is reference.
     - For each open earlier finding: if the changes fix it, put its id in "resolved"; if not, report it again with the same "id".
     - Do not report an already-fixed finding unless the new changes broke it again.
     - Report a NEW finding only for code inside the changes since your last review, or at severity high when shipping without the fix would be wrong.
     ```
   Use the worktree and specs `_assemble_reviewer_inputs` used: factor the
   worktree/specs resolution at its top into
   `_review_worktree_and_specs(run_dir) -> tuple[str, list[str]]` and call it
   from both.
3. **After the verdict is extracted:**
   - parse the review file (the same JSON `_extract_verdict` read; on failure
     use `{"verdict": verdict}`);
   - `delta_files` = the second element of `_review_delta(...)`, or `None`;
   - call `review_ledger.record_review(run_dir, review_ledger.attempt_no(run_dir), data, delta_files)`;
   - if any ids were assigned, rewrite the review file with the updated
     `findings` (json, indent 2).
   Do this inside `try/except Exception: pass`; the verdict flow must never
   change because of the ledger.

### 4. Implementer feedback (`mini_ork/cli/execute.py`)

1. In `_revise_failure_section`'s reviewer branch, after notes/reasons:
   - if `data.get("findings")` is a non-empty list, add `findings:`, then one
     line per finding: `- [<id or ?>] <severity> <file>:<line> — <issue>`,
     with `(also at …)` for instances;
   - then append `review_ledger.feedback_lines(run_dir, attempt)` lines for
     open findings NOT already listed, under `still open from earlier reviews:`.
2. In `_write_revise_feedback`, when `round_no == max_rounds`, append to the
   header:
   `This is the last revision round. If a finding survived an earlier fix, do not patch the same lines again: choose a different approach for it.`

### 5. `recipes/framework-edit/prompts/reviewer.md`

- Replace the "Return one JSON object with:" list with the runtime vocabulary:
  `verdict` is `pass`, `needs_revision`, `fail` or `escalate`, plus `notes`,
  `findings`, `resolved`, `checked_criteria` and `artifact_ref`.
- In the findings table, add rows for `id` ("leave empty for a new finding;
  reuse the id from 'Your earlier findings' when reporting one again") and
  `instances` ("every other location of the same defect").
- Replace the `revise`/`reject` names in the paragraph under the table with
  `needs_revision`/`fail`.
- In "One complete pass", add: "From round 2, judge the earlier findings by id
  first and put fixed ones in `resolved`."

Leave every other sentence as is.

## Files in scope (touch ONLY these)

- `mini_ork/workflow/review_ledger.py` (new)
- `mini_ork/cli/execute.py`
- `mini_ork/cli/execute_handlers.py`
- `recipes/framework-edit/prompts/reviewer.md`
- `tests/unit/test_review_ledger.py` (new)
- `tests/unit/test_review_converge.py` (new)

Do NOT modify the retry/round-counter logic in `_dispatch_dependency_graph`,
`_round_cap`, `MO_REVISE_ROUNDS` semantics, the panel-gate or synth branches,
`lib/extract_verdict.py`, any migration, `docs/`, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (hermetic, no LLM)

`tests/unit/test_review_ledger.py`:
1. First review with two findings → ids F1, F2, both open, and one round summary with `new=2`.
2. Second review re-reporting `id=F1`, resolving F2, and adding one new finding → F1 open with `last_seen=2`, F2 fixed (`fixed_in=2`), F3 new; summary `reported_again=1, fixed=1, new=1`.
3. A new finding equal (after normalization) to a fixed one → `reopened: true`.
4. `delta_files=["a.py"]` and a new finding on `b.py` → `outside_delta: true`.
5. `resolved` naming an id that is also re-reported → stays open.
6. `feedback_lines` adds the STILL OPEN suffix only to findings seen in two or more reviews.
7. `prior_block` is empty for no findings and lists open before fixed.
8. A corrupt ledger file → `load` returns the empty shape.

`tests/unit/test_review_converge.py`:
9. In a tmp git repo: `_snapshot_review_tree` returns a tree sha and leaves `git status --porcelain`, `HEAD` and `.git/index` mtime unchanged.
10. Two snapshots around a file edit plus `_archive_revise_round` → `_review_delta` returns a patch containing only the second edit and its file name.
11. `reviewer_envelope("x.json")` contains `"findings"`, `"resolved"`, `instances` and `write that exact JSON object to x.json`.
12. `_revise_failure_section` for a reviewer field whose review JSON has `findings` renders a `[F1] high a.py:3 —` line.
13. `_write_revise_feedback` with `round_no == max_rounds` contains "This is the last revision round".
14. `recipes/framework-edit/prompts/reviewer.md` contains `needs_revision` and `resolved` and no longer contains "`approve`, `revise`, or `reject`".

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_review_ledger.py tests/unit/test_review_converge.py tests/unit/test_review_rounds.py tests/unit/test_revise_error_lines.py tests/unit/test_reviewer_no_reread.py tests/unit/test_reviewer_diff_scope_py.py tests/unit/test_framework_edit_capture_baseline_py.py tests/unit/test_recover_fixes.py tests/unit/test_ctx_cap_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_review_ledger.py tests/unit/test_review_converge.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r5a-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r5a-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/workflow/review_ledger.py mini_ork/cli/execute.py mini_ork/cli/execute_handlers.py` is clean.
- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_mini_ork_execute_py.py` passes.
- A first-round review prompt (no ledger) differs from today's only in the
  envelope text.
