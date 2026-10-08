# A recover publishes exactly what its implementer produced: carry patch only when applied, plus the revived implementer's own edits

## Why (live, 2026-10-08)

Run `ide-orca-f2b-flow-20261008172728` (code-fix on the Zed fork):

1. **Round 2 timed out.** Rollback reverted the tree and saved `salvage.patch` (the round-2
   work).
2. **The carry patch was silently ignored.** The operator revived it with
   `mini-ork recover <run> --from-node implementer --carry-patch salvage.patch --force`.
   - `mini_ork/recovery/planner.py` `_needs_restore` (~:422) only applies a carry patch when
     the implementer is REUSED, so it was not applied.
   - The implementer resumed its session on the REVERTED tree and wrote the change again. The
     reviewer approved THAT tree.
3. **The publisher committed the wrong content.** `publisher_authored_patch.resolve_authored_patch`
   returns the carry patch FIRST whenever a `salvage.patch` file exists.
   - It committed the unreviewed round-2 salvage (zed `f70102a`).
   - The reviewed change was left behind as "130 foreign hunks".

   The operator fixed the Zed history by hand (`0bf6910`), after proving that the revival's
   own Edit calls replayed from the base reproduce the tree.
4. **Auto-repair ran on a published run.** The recover's auto-repair hook then fired, because
   `auto_repair.decide` treats a run as withheld from `verdict.json` levels even when the
   publisher node finished `done`. It spawned a `reverify` recover, which was refused, and
   left `repair.json` `gave_up`.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/restore.py`: ONLY `restore_carry_patch`
- `mini_ork/recovery/planner.py`: ONLY `_needs_restore`
- `mini_ork/cli/publisher_authored_patch.py`
  - The worktree already carries the operator's UNCOMMITTED start: `_record_ts`, the `since`
    filter in `_collect_edits`, `_edits_since`, `_attempt_started`, `_carry_tree`,
    `_compose_carry_and_edits`, and their use in the carry branch.
  - Keep them, review them, and fix them as needed.
- `mini_ork/recovery/auto_repair.py`: ONLY the withheld test in `decide` (rule 1)
- `tests/unit/test_recover_carry_publish.py` (new)
- `tests/unit/test_publisher_authored_patch.py`: add tests only

Do NOT touch any other file.

## Changes (exact)

1. **`restore.restore_carry_patch`.** On `applied` (not dry-run) and on `already_applied`,
   write `<run_dir>/carry-applied.json`:

   ```
   {"patch": <basename>, "sha256": <hex of the patch bytes>, "applied_at": <epoch float>, "target": <target>}
   ```

   Best-effort: an OSError never fails the restore.
2. **`planner._needs_restore`.** Also return True when the operator passed `--carry-patch`
   explicitly (the `carry_patch` argument is non-empty and resolves to a file), even when the
   implementer re-runs. The resumed implementer continues on top of its own earlier work, so
   the tree must hold it.
   - The implicit `salvage.patch` / `rolled-back.json` rule stays limited to the reuse case.
3. **`publisher_authored_patch.resolve_authored_patch`:**
   - **A carry patch counts as authored ONLY when `carry-applied.json` exists and its `patch`
     and `sha256` match the carry file.** A `salvage.patch` that merely sits in the run dir is
     not a source.
   - **When the carry was applied:** compose the carry patch with the edits recorded after
     the latest implementer attempt started (`_attempt_started`; use `applied_at` as the
     fallback floor). The operator's composition does this.
   - **When the run rolled back (`rolled-back.json`) but no carry was applied:** the tree was
     reset to the base before the latest attempt. Replay ONLY the edits recorded after the
     latest implementer attempt started, from `pre-implementer-ref` (`_replay_edits`). If that
     fails, abstain; never fall back to older sessions.
   - Everything else is unchanged.
4. **`auto_repair.decide` rule 1.** A run whose `publisher` node's LATEST `node_end` has
   `finish_reason == "done"` is published: not withheld, whatever `verdict.json` says →
   `action = "none"`. Read it from the events `decide` already loads. Keep the withheld override
   for a publisher that finished `levels_unverified` / `publish_abstain`.

## Tests

- **`tests/unit/test_recover_carry_publish.py`:**
  - `restore_carry_patch` writes `carry-applied.json` with the right sha. `already_applied`
    writes it too.
  - `_needs_restore` with `--carry-patch` and the implementer in the rerun set → True. Without
    `--carry-patch` and no implementer reuse → False (unchanged).
  - `decide` on a run whose publisher `node_end` is `done` while `verdict.json` has
    `levels_decision: abstain` → `action == "none"`. Remember `monkeypatch.delenv("MO_AUTO_REPAIR")`;
    `tests/conftest.py` sets it to 0.
- **`tests/unit/test_publisher_authored_patch.py` (temp git repo, synthetic run dir):**
  - **Carry not applied.** `salvage.patch` present, no `carry-applied.json`, `rolled-back.json`
    present, a transcript whose edits (timestamped after a fake implementer `node_start`) apply
    from the base → the authored patch is the transcript replay, NOT the salvage. The salvage's
    content is absent from the patch.
  - **Carry applied, implementer edited on top.** The authored patch = carry + edits, and
    `land_patch` of it on the base reproduces the tree.
  - **An abandoned attempt's edits are ignored.** Edits timestamped BEFORE the latest
    implementer start are ignored.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_recover_carry_publish.py tests/unit/test_publisher_authored_patch.py tests/unit/test_auto_repair.py tests/unit/test_recover_revival.py tests/unit/test_verdict_hygiene.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check` on the touched files → clean.
- `git diff --stat` touches only the files in scope.
