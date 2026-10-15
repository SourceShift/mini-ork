# Report a finished intake run back to its ticket

## Goal

A ticket that intake turned into a run hears nothing back: `mini_ork/pm/writeback.py` exists but
nothing calls it, and `threads.service.on_run_terminal` has no production caller either, so a
thread's run stays `running` until the next message happens to settle it. Close the loop: once
per finished run, settle its thread and post the outcome to the ticket through the write-back
gate.

**Deliverable.** A new `mini_ork/intake/report.py`, wired into the intake pass and exposed as
`mini-ork intake report`, plus `tests/unit/test_intake_report.py`.

**Scope:**
- **`report_pass(home, *, db=None, dry_run=False) -> list[ReportOutcome]`** in
  `mini_ork/intake/report.py`. For every `pm_links` row of kind `run` (`issue_uid` →
  `run:<run_id>`, see `mini_ork/pm/store.py:links_for` and `mini_ork/threads/service.py:_link_issue`):
  1. Skip unless the run is terminal. Use the existing vocabulary bridge
     `_TERMINAL_TASK_STATUS_MAP` in `mini_ork/threads/service.py` (do not re-derive it).
  2. Skip if already reported: an `intake_events` row for `(provider, "report:<run_id>")`
     (`mini_ork/threads/store.py:record_intake_event` writes them; `mini_ork/intake/dispatch.py`
     shows the call).
  3. Settle the thread with `threads.service.on_run_terminal(home, run_id, status, db=…)` — its
     first production caller.
  4. Compose a short comment: the outcome (pass/fail from the run's `verdict.json` when present,
     else the terminal status), the run id, the recipe, and the published commit when the run
     dir records one. Plain prose — the Jira adapter converts it to ADF.
  5. Post it with `mini_ork.pm.writeback.comment(issue_uid, body)`.
     - `Unsupported` (write-back off, or an incapable source such as GitHub) → outcome
       `skipped` with the reason, **not recorded**, so enabling write-back later posts it.
       No exception escapes.
     - `ProviderError` → outcome `error`, not recorded, retried on the next pass.
     - Success → record the `report:<run_id>` event; outcome `reported`.
  - `dry_run=True` decides and reports what it would do, and writes nothing (no settle, no
    comment, no event).
- **Wiring.** `mini_ork/intake/poll.py`'s pass (`intake once` and `intake poll`) runs
  `report_pass` after dispatch, so the simple tier closes the loop with no new process; and
  `mini_ork/cli/intake_cmd.py` gains a `report` verb (`[--dry-run] [--json]`) for the secure tier,
  where a webhook-driven deployment runs it from its scheduler. Keep `--dry-run` threading the
  existing way. `tests/unit/test_ide_pages_actions.py` must still resolve every `intake` action.
- **Two tiers** (CLAUDE.md): identical logic; only who invokes it differs (the poll loop vs. a
  cron/scheduler calling `intake report`). Whether anything is posted is decided solely by the
  write-back gate — `write_back: true` plus a write-scoped token — so both tiers fail closed.

**Acceptance.** Offline tests on a temp home (every store call given the temp `db` explicitly),
with a fake provider registered for a fake source id:
- a terminal linked run with write-back enabled and a comment-capable provider → exactly one
  comment, the event recorded, the thread run settled; a second pass posts nothing;
- write-back off → zero provider calls, outcome `skipped`, thread still settled, nothing
  recorded; enabling it afterwards posts on the next pass;
- a provider raising `ProviderError` → outcome `error`, not recorded, retried next pass;
- a non-terminal run → untouched;
- `dry_run=True` → zero writes of any kind;
- `intake report --dry-run --json` prints the outcomes.

- max attempts: 2

---

## Scope Hint

- `mini_ork/intake/report.py`
- `mini_ork/intake/poll.py`
- `mini_ork/cli/intake_cmd.py`
- `tests/unit/test_intake_report.py`
- `mini_ork/pm/writeback.py` — read-only reference: the gate
- `mini_ork/threads/service.py` — read-only reference: `on_run_terminal`, `_TERMINAL_TASK_STATUS_MAP`
- `mini_ork/pm/store.py` — read-only reference: `links_for`
- `mini_ork/threads/store.py` — read-only reference: `record_intake_event`

## Verification commands
- `python3.11 -c "import mini_ork.intake.report"`
- `python3.11 -m pytest -q tests/unit/test_intake_report.py tests/unit/test_intake_poll.py tests/unit/test_ide_pages_actions.py`

Run these from the run's working tree (the isolated worktree), so `mini_ork` resolves to the tree under test. A syntax check is not acceptance.

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
