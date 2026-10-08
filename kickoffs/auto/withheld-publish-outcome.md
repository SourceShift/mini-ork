# A run whose publish was withheld is "needs you", not "failed at implementer"; the failing node is never guessed

## Why (live, 2026-10-08)

Run `ide-orca-b2a-triage-20261008104922` (code-fix): every node passed. That means typecheck,
the build test, reviewer pass (round 2), rubric 8/8 and eval ran. Then the publisher logged
`[ABSTAIN] publisher: level vector not proven (applies=PROVEN executes=PROVEN target=UNVERIFIED
preserve=PROVEN contract=n/a) — publish withheld` (`mini_ork/cli/publisher.py:250-255`), and
the run row became `status='failed'`. Its `verdict.json` holds
`{"verdict":"pass", "levels_decision":"abstain", "levels":{…}}`. There is no
`run-verdict.json`.

The IDE then told the user: **"Failed at implementer; the cause was not classified. The change
itself must be revised."** Every part of that is wrong, and there is no action button. Two
causes:

1. **`mini_ork/acp/task_state.py` `_failing_node` (~:138)** treats a `node_end` with no
   `finish_reason` as failing (`"unknown"`). code-fix implementer `node_end` events carry no
   `finish_reason`, so the implementer gets blamed. This shows in the sidebar row and in the
   run page.
2. **`mini_ork/ide_pages/outcome.py` `resolve`** has no notion of a withheld publish. It falls
   into the failed rule and the unclassified retry hint.

4 runs in the live home have `levels_decision: abstain`. The user's rule: a run that did the
work must never look like a dead end. Show the true state and the next action; if it truly
failed, offer retry from the failed node.

## Files in scope (touch ONLY these)

- `mini_ork/acp/task_state.py`: ONLY `_failing_node`, `task_state` (one new rule) and
  `run_mark` (the same rule, cheap)
- `mini_ork/ide_pages/outcome.py`: `resolve` + one new helper
- `mini_ork/recovery/retry_hint.py`: ONLY the classification entry: a withheld-publish branch
- `tests/unit/test_withheld_publish_outcome.py` (new)

## Changes (exact)

### 1. `_failing_node`

Only a `node_end` whose payload says it failed counts:
- an explicit `finish_reason` other than `done` / `skipped` / `abstain`; or
- a `verdict` in {`REQUEST_CHANGES`, `ESCALATE`, `CRASH`, `needs_revision`, `fail`, `failed`}; or
- an `error` field.

A missing `finish_reason` with no failure signal is NOT failing. Also, a node's later clean
`node_end` (a revise round that passed) clears an earlier failure of the same node id: keep
per-node state, and return the last node still failing.

### 2. Withheld publish in `task_state`

A new rule placed BEFORE the terminal-failed/worktree rule 3:
- Condition: `status in ("failed", "rolled_back")` and
  `<run_dir>/verdict.json` (or `run-verdict.json`) has `levels_decision == "abstain"` and no
  failing node (per the fixed `_failing_node`).
- Result: `TaskState(state="needs_you", detail="Not published: <names of non-PROVEN levels>
  unverified — review and decide", …)`.
- Read the JSON once; fail soft.

`run_mark` mirrors it: for failed/rolled_back with a run_dir, read `verdict.json` only when it
exists. The tile count and the list must agree; reuse one helper for both.

### 3. `outcome.resolve`: a withheld branch

Evaluated before the failed rule, with the same condition:

- **state** `needs_you`, **tone** `orange`, **icon** `?`
- **text:** `Not published — <level> unverified` (join the non-PROVEN, non-n/a level names)
- **detail:** `Every step passed (<reviewer verdict>, checks <p>/<q>). mini-ork publishes only
  when every level is PROVEN.` plus one line per unproven level from `levels_reasons` when
  present (cap 160 chars each).
- **actions:**
  - **Certify this change** (primary; `S.page_link("verify","certify",run=id)`): proving the
    target is how to unblock.
  - **Publish again** (`S.cli("board","retry",id, confirm="Re-run the publisher? It publishes
    only if the levels are now proven.")`).
  - Merge into <base> / Discard when a workspace exists (reuse `run._actions`' buttons).
- **counts:** as today (level badges included).
- **Explicit refutes:** if any level is REFUTED, use the failed rule instead, with text
  `Not published — <level> refuted`.

The failed rule's "Failed at <node>" now uses the fixed `_failing_node`. When none is found,
the text is "Failed" plus the hint summary, never a guessed node.


### 4. Landed elsewhere (`landed.json`)

49 failed runs in the last 4 days did deliver their change through a later revision or a direct
commit. The user's rule is to never leave delivered work looking failed. A run dir may carry
`landed.json` `{"commit": "<sha>", "repo": "<path>", "note": "<str>"}`, written by the operator
or a later tool.

- **`task_state`:** when `status in ("failed", "rolled_back")` and `landed.json` exists with a
  non-empty `commit`, return `TaskState(state="done", detail=f"Landed via {commit[:9]}" + (f" —
  {note}" if note else ""), …)`. Evaluate this rule FIRST among the terminal rules: it beats the
  retry gate and the withheld rule.
- **`run_mark`:** same rule → the done mark.
- **`outcome.resolve`:** same rule → state `done`, tone `green`, icon `✓`, text `Landed via
  <sha>`, detail = note.
  - Actions: **Open commit** when `repo` is a git dir. Use
    `S.cli("board","…")` only if such a verb exists; otherwise omit it. Keep the menu.
- **Test:** a failed run with `landed.json` → done everywhere (task_state, run_mark, outcome),
  with the sha in the text.


### 5. `retry_hint`: a withheld publish is retryable (verify)

`mini-ork recover ide-orca-b2a-triage-20261008104922 --from-node test` was REFUSED: "retry-hint
refuses this run (strategy='none'); summary: Failed at implementer; the cause was not classified".
The run had every node green and only the publisher abstained.

Add the same withheld condition early in `load_or_compute`'s classification:
- `levels_decision == "abstain"` and no failing node (same helper as `task_state`);
- return `retryable: True`, `strategy: "verify"`, `from_node: <first verifier node of the
  workflow>`;
- `needs_change: {"kind": "levels", "summary": "Not published: <levels> unverified", "detail":
  <levels_reasons lines>}`;
- notes: `["re-verify; publishes when every level is PROVEN or n/a"]`.

Test: the live-shaped fixture → strategy verify, retryable, from_node = the test verifier.

## Tests (`tests/unit/test_withheld_publish_outcome.py`; reuse fixtures from `tests/unit/test_ide_pages_outcome.py` / `tests/unit/test_needs_you_truth.py`)

- **Events like the live run** (implementer node_end with no finish_reason; reviewer
  needs_revision, then pass; eval done; publisher node_start only) + `verdict.json` abstain with
  target UNVERIFIED + status failed:
  - `task_state` → needs_you, with detail naming `target`;
  - `run_mark` → the needs-you mark;
  - `outcome.resolve` → orange `Not published — target unverified`, with Certify (primary) and
    Publish again.
- `_failing_node`:
  - a missing finish_reason → not failing;
  - an explicit `finish_reason="error"` → failing;
  - a reviewer needs_revision followed by a pass → not failing.
- A refuted level → failed state, `Not published — target refuted`.
- A genuinely failed run (`finish_reason="timeout"` on implementer, no abstain) keeps "Failed at
  implementer (timeout)" and its retry actions.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_withheld_publish_outcome.py tests/unit/test_ide_pages_outcome.py tests/unit/test_acp_task_state.py tests/unit/test_needs_you_truth.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/acp/task_state.py mini_ork/ide_pages/outcome.py tests/unit/test_withheld_publish_outcome.py` → clean.
- **Live proof (read-only):**
  `MINI_ORK_IDE_SPEC=2 bin/mini-ork board page run --arg run=ide-orca-b2a-triage-20261008104922 --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`
  prints the triage text / tone / action labels: orange "Not published — target unverified",
  with Certify and Publish again. Also print the run's `fleet_rows` state. Paste both.
- `git diff --stat` touches only the files in scope.
