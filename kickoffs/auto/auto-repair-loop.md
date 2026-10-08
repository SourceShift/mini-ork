# Auto-repair: a failed run fixes itself in a bounded feedback loop, then resumes from its failed node

## Why (user rule, 2026-10-08)

> "instead of just accepting that these failed and leaving them or re-creating new ones we
> should always revive and fix" … "there should be a technique that in a feedback loop fixes the
> failure".

Today:
- The **revise loop** (`MO_REVISE_ROUNDS`, default 2) runs only INSIDE a run.
- The **retry hint** (`mini_ork/recovery/retry_hint.py` `load_or_compute`) diagnoses a failed
  run.
- **`mini-ork recover <run> --from-node <id> [--lane a=b] [--ack-change]`** resumes it, reusing
  finished nodes.

But nothing ACTS: every failed run waits for a human click or is abandoned. In the last 4 days,
61 runs ended failed or rolled_back. Most were "reviewer still asked for a revision after 2
rounds" → rollback, and a human then launched a fresh `<slug>-r2` run. Others were a lane quota
or a withheld publish.

This run adds the loop:

1. diagnose;
2. pick a repair;
3. apply it;
4. resume the SAME run from the failed node;
5. repeat, bounded;
6. hand over to the human only when stuck.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/auto_repair.py` (new)
- `mini_ork/cli/repair_cmd.py` (new): the `mini-ork repair` subcommand
- `mini_ork/cli/main.py`: ONLY
  - one `"repair": "mini_ork.cli.repair_cmd"` entry in `_NATIVE_MODULE_SUBS`;
  - one hook right after the existing `retry_notify.notify(...)` side channel in the run flow
    (~:1026-1037).
- `mini_ork/recovery/planner.py`: ONLY one hook in `cli_main` after `execute_fn(exec_argv)`
  returns (~:1100), so a revived run that fails again re-enters the loop.
- `tests/unit/test_native_dispatch_py.py`: ONLY add `"repair"` to the exact-set `expected` in
  `test_all_former_exec_subs_registered_natively`. This guard asserts exact set equality.
- `tests/unit/test_auto_repair.py` (new)

Do NOT touch `mini_ork/cli/execute.py` (claimed by another worktree) or
`mini_ork/cli/publisher.py`.

## Design (exact)

### `auto_repair.decide(home, run_id) -> dict`

Pure policy, no side effects. Returns
`{"action", "from_node", "lanes": {alias: lane}, "ack_change": bool, "feedback": str, "reason": str, "signature": str}`.

Inputs:
- `retry_hint.load_or_compute(home, run_id, write=False)`;
- the run's `verdict.json` / `run-verdict.json` (`levels_decision`, `levels`, `levels_reasons`);
- `review-*.json` (findings/reasons);
- `run_events` node_end rows: the last failing node via an explicit failing finish_reason
  (not "done"/"skipped"/"abstain") or a failing verdict;
- `<run_dir>/repair.json` history.

**Policy, first match wins:**

1. **No auto-repair:** the run is not failed/rolled_back, or `MO_AUTO_REPAIR == "0"` →
   `action = "none"`.
2. **Stop: human needed.** Any of:
   - the attempts in `repair.json` reach `MO_AUTO_REPAIR_MAX` (default 2);
   - the run's cost since the first repair exceeds `MO_AUTO_REPAIR_BUDGET_USD` (default 10);
   - the new `signature` equals the last attempt's signature (no progress).

   The signature is `sha1(failed_node + reason + first finding issue/first failing check)[:12]`.
   Result: `action = "human"` with a reason.
3. **Lane:** hint `needs_change.kind == "lane"` with `suggestions` → `action = "lane"`,
   `lanes = {alias: suggestions[0].lane}`, `from_node = hint.from_node`.
4. **Infra:** the failing finish_reason is in {timeout, killed, sigterm, rc_137, rc_143, network,
   provider_error, overloaded, rate_limited}, or the hint kind is `infra` → `action = "infra"`,
   same node, same lane.
5. **Withheld publish:** `levels_decision == "abstain"` and no failing node →
   `action = "reverify"`, `from_node` = the test/verifier node (the first verifier in the
   workflow). Re-running verification lets a fixed instrument or a newly proven level publish.
   - If the previous attempt already did `reverify` with the same levels → instead
     `action = "prove"`, `from_node = <implementer>`, feedback = "Every check passed but level
     `<name>` is UNVERIFIED (`<levels_reasons[name]>`). Add the smallest test that exercises
     the change so the level can be proven; change nothing else."
6. **Code: revision exhausted.** The failing node is a reviewer/verifier/eval with
   needs_revision/fail, or the hint kind is `code` → `action = "revise"`,
   `from_node = <the implementer>` (the workflow node of type implementer; first if several),
   `feedback` = the composed round file (below), `ack_change = True`.
   - Escalate the implementer's lane on the SECOND revise attempt only:
     `lanes = {<implementer role_lane>: MO_AUTO_REPAIR_ESCALATE_LANE (default "opus")}`.
     The role lane comes from `mini_ork/ide_pages/run.py` `_load(home, run_id)` nodes.
7. **Owner-only fixes:** hint kind in {environment, credentials, budget} → `action = "human"`
   (the existing retry-notify gate covers it).
8. **Anything else** (`unknown`) → `action = "human"`, with the hint summary and the last log
   lines as the reason.

**Feedback file** (revise / prove):
- `revise/round-<n>.md`, where `<n>` = (the highest existing round number) + 1;
- header: `Repair round <n> (auto-repair): the run failed after its revise rounds; these
  problems are still open. Fix ONLY these, on top of the working tree; do not start over.`;
- then the reviewer's findings (issue · file:line · snippet) and reasons, plus failing verifier
  checks (first 8 lines of each failing check's log).

Reuse the round-file format `execute._write_revise_feedback` uses, but write it in
`auto_repair.apply`, not `decide`.

**Include the real error text.** Live case: ide-orca-b2b-story failed both revise rounds on a
Rust compile error (`post_rc=101`). Its round files only said "post-patch failing; see log",
because the cargo output lives in `verifier_test.log` and never reached the implementer. For a
failing test/typecheck verifier, append up to 40 lines from `<run_dir>/verifier_<id>.log`,
falling back to the newest `evidence/<id>-*.log`. Keep the lines matching
`^error|^warning: unused|^\s+-->|^\s+\||FAILED|Error:|Traceback|assert`, each with its next 2
lines of context. If both logs are empty, say so explicitly in the round file: "the build/test
output was not captured; re-run the verification command first and read its errors".

### `auto_repair.apply(home, run_id, decision, *, spawn=True) -> dict`

- **For revise/prove:**
  - write the feedback file;
  - write `revise/current.json` `{"round": n, "max_rounds": n, "feedback": path}`. This is the
    exact channel `execute_handlers._read_revise_feedback` reads, so the implementer appends it
    to its prompt.
- Append the attempt to `<run_dir>/repair.json`
  `{"attempts": [{"n", "ts", "action", "from_node", "lanes", "reason", "signature"}]}`, and emit
  a `run_events` row `auto_repair` with the same payload.
- **When `spawn`:** start detached (`start_new_session=True`, stdout to
  `<run_dir>/repair-<n>.log`):
  `mini-ork recover <run> --from-node <node> [--lane a=b …] [--ack-change]`.
  - Use the same python and env as the current process.
  - Pass `MO_AUTO_REPAIR_ATTEMPT=<n>`.
  - Return `{"pid", "log", "command"}`.
- **`action == "human"`:** do not spawn. Ensure the existing needs-you path exists: call
  `retry_notify`'s gate writer if one is not already pending for the run. Record the attempt
  with action human.

### `auto_repair.maybe_repair(home, run_id)`

- `decide` → `apply`.
- Never raises: log and swallow.
- Return at once when `MO_AUTO_REPAIR == "0"`.
- Re-entrancy guard: if `MO_AUTO_REPAIR_ATTEMPT` is set in the env and the spawned recover is
  still the same process tree, do nothing synchronously. The next terminal failure of the
  recovered run triggers the next decision through the same hooks. That IS the loop.

### Hooks

Both hooks call `auto_repair.maybe_repair(home, run_id)` UNCONDITIONALLY, in try/except, never
changing the exit code. `decide` reads the run's DB status itself. A withheld publish sets
`status='failed'` (`publisher.py:256`) while execute returns 0, so gating on rc would miss it.

- **`main.py` run flow:** right after the retry-notify block.
- **`recovery/planner.py` `cli_main`:** after `exec_rc = execute_fn(exec_argv)`, inside the
  `try` before the lease `finally`, or right after it. Use the run id from `handoff["run_id"]`
  and the home from env `MINI_ORK_HOME` (or `<root>/.mini-ork`).


### Sequencing with failure triage (agreed with the triage loop's owner)

`mini_ork/triage` (MO_FAILURE_TRIAGE / _PROMOTE, hooked in execute.py's fail branch) escalates a
failure to a separate framework-edit epic. Auto-repair fixes the SAME run first. To keep one owner
per trigger, so the same failure is never fixed twice:

- **Marker:** `repair.json` carries `"state"`, one of:
  - `"repairing"`: an attempt was spawned;
  - `"gave_up"`: the decision was `human` after at least one attempt, OR the stop rules fired;
  - `"repaired"`: a later decide() finds the run published/done.

  Write it on every apply.
- **On give-up:** when `context_env("MO_FAILURE_TRIAGE") == "1"`, call
  `mini_ork.triage.failures.triage_run(run_id, home=home, db=<db>, root=<root>,
  promote=context_env("MO_FAILURE_TRIAGE_PROMOTE") == "1")` exactly once. Record
  `"triaged": true` in `repair.json`. Fail-soft.
- **Default ON, explicit in the run flow.** User rule: always revive and fix. In `main.py`'s run
  flow, BEFORE execute is called, when `MO_AUTO_REPAIR` is unset, publish it as `"1"` via
  `mini_ork.context.apply_env_overrides({"MO_AUTO_REPAIR": "1"})`, the sanctioned mutator. An
  explicit `"0"` is never overridden.
  - The triage owner gates its execute.py hook opt-in: it skips when
    `context_env("MO_AUTO_REPAIR", "0") == "1"`.
  - So a normal `mini-ork run` has auto-repair owning the trigger. A direct `mini-ork execute`
    or `MO_AUTO_REPAIR=0` keeps triage firing as today.
  - Do NOT touch execute.py.
- `decide()` treats `MO_AUTO_REPAIR == "0"` as off. Anything else (incl. unset, e.g. the manual
  `repair` CLI) is on.
- **Record the triage outcome on every give-up**, even when `MO_FAILURE_TRIAGE != "1"`:
  `"triaged": false`, so absence is explicit.
- The hook in execute.py is named `_maybe_triage_failed_run(db, run_id, home, root)`; triage's
  signature is `triage_run(run_id, *, home=None, db=None, root=None, promote=False, dry_run=False)`.
- **Re-dispatch path:** revise/prove rounds go through `mini-ork recover` → the normal executor.
  Do not build your own agent dispatch. The executor's session-reuse fix (researcher-defects
  a45032c1, C9b) then applies automatically once merged.
- **Test:** a give-up with `MO_FAILURE_TRIAGE=1` calls `triage_run` once (stub it). A second
  give-up decision does not call it again.

### CLI `mini-ork repair`

- `mini-ork repair <run_id> [--dry-run] [--json]`: decide, print the decision; apply unless
  `--dry-run`.
- `mini-ork repair --sweep [--since-days N=4] [--dry-run] [--json] [--include-experiments]`:
  every failed/rolled_back run in the window.
  - **Skip experiments:** ids starting `vt` + digit, recipe names ending `__probe`, or recipe
    `refactor-audit`, unless `--include-experiments`.
  - **Skip runs whose retry gate is resolved as superseded** (`retry-gate.json` with
    `superseded` or `abandoned`).
  - Print one line per run: id, action, from_node, reason.
  - With `--dry-run`, nothing is written.

## Tests (`tests/unit/test_auto_repair.py`; temp home + DB fixtures like `tests/unit/test_ide_pages_outcome.py`; stub `retry_hint.load_or_compute` and `subprocess.Popen`)

- **lane hint** → action lane, `lanes={"codex_lens":"deepseek"}`; `apply` spawns `recover` with
  `--lane codex_lens=deepseek --from-node <node>`.
- **reviewer needs_revision after its rounds** → revise from the implementer:
  - the feedback file has the findings;
  - `revise/current.json` points to it;
  - the second revise attempt adds the escalation lane.
- **withheld** (abstain, no failing node) → reverify from the first verifier. A second identical
  attempt → prove from the implementer.
- **environment kind** → human, no spawn; a retry gate pending (or already pending → not
  duplicated).
- **No progress:** the same signature twice → human. Max attempts → human. Budget exceeded →
  human.
- **`MO_AUTO_REPAIR=0`** → none.
- **`--sweep --dry-run`:** skips `vt5-weak-…` and superseded runs, writes nothing (dir snapshot
  unchanged), and prints decisions.
- **Hooks:**
  - `recovery.planner.cli_main` with a stub `execute_fn` calls `maybe_repair` once (monkeypatch);
  - an exception inside it does not change the returned rc;
  - main's hook exists after `retry_notify` (assert by calling the extracted helper, or by
    reading source ordering if the flow is not unit-callable).

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_auto_repair.py tests/unit/test_native_dispatch_py.py tests/unit/test_retry_hint.py tests/unit/test_recover_cli_dispatch.py tests/unit/test_recover_verify.py; do [ -f "$f" ] || continue; env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/recovery/auto_repair.py mini_ork/cli/repair_cmd.py mini_ork/cli/main.py mini_ork/recovery/planner.py tests/unit/test_auto_repair.py` → clean.
- **Live proof (read-only):**
  `bin/mini-ork repair --sweep --since-days 2 --dry-run --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`
  prints one decision per failed run. Paste it. The `ide-orca-b2a-triage-20261008104922`
  decision must be `reverify`.
- `git diff --stat` touches only the files in scope.
