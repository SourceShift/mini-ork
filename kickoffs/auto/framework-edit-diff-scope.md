# framework-edit.diff must be scoped to the run's declared scope — concurrent files poisoned a live run

## Why (live evidence, 2026-10-08)

Run `run-orchestrator-scaffold-20261008124718` (framework-edit, self-edit,
`MO_ALLOW_FRAMEWORK_CWD=1`) implemented correctly but EVERY verifier failed and the reviewer
returned verdict=fail — because its diff was a MIXED-BASE patch of the SHARED primary checkout:
7 files = the run's 3 legitimate files + 4 files written by OTHER sessions during/before the run
window (`kickoffs/auto/lane-preflight-hint.md` written 13:04:42 mid-run;
`kickoffs/auto/post-run-verify-reuse.md` + `mini_ork/cli/verify.py` +
`tests/unit/test_post_run_verify_reuse.py`, pre-existing WIP 12:58:06). No single base accepts
such a patch; attribution was only reconstructable via mtimes. The deliverable had to be landed
manually outside the run (commit 3a913a81) and the run stopped.

The hole is in `_write_implementer_summary` / the framework-edit diff derivation
(`mini_ork/cli/execute.py:2095-2160`, same baseline `_assemble_reviewer_inputs` uses):

- tracked changes: `git diff --name-only <pre-implementer-ref>` — correct for pre-implementer
  state, but any session's tracked edits DURING the run window enter the diff;
- untracked: `git ls-files --others` minus `pre-implementer-untracked` — excludes dirt that
  pre-existed the snapshot, but a file CREATED after the snapshot (not in the pre-list because it
  did not exist yet) is swept in as an "implementer change". That is exactly how a kickoff file
  written mid-run joined the patch.

The docstring promises "a concurrent session's pre-existing dirt is never swept" — pre-existing
is handled, concurrent-created is not. And the run KNEW its scope: `run_profile.json.scope_allow`
listed exactly the 3 legitimate files.

## Files in scope

- `mini_ork/cli/execute.py` — the derived change-list + reviewer diff inputs only
- `tests/unit/test_framework_edit_diff_scope.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **Intersect with declared scope.** The derived `files_changed` (and the reviewer's diff inputs)
   intersect with the run's declared scope — `run_profile.json` `scope_allow` when present. A
   change outside scope is dropped from the diff AND reported loudly (stderr + implementer
   summary `out_of_scope_changes` list) — never silently included, never silently discarded.
   When `scope_allow` is absent/unparseable, keep today's behavior (whole-tree delta) so
   non-scoped recipes are unaffected.
2. **Untracked hole.** A file created after the implementer snapshot that is NOT in the run's
   scope cannot enter `files_changed` — rule 1 already enforces this; pin it with the exact
   live shape as a test.
3. **Verifier-facing diff.** Whatever assembles `framework-edit.diff` uses the same scoped list,
   so the patch always applies to a single base.

## Tests (`tests/unit/test_framework_edit_diff_scope.py`, temp git repo, no network)

- Seed a repo with a run profile whose `scope_allow` names 2 files; during the "implementer
  window" create an out-of-scope untracked file + edit an out-of-scope tracked file → derived
  `files_changed` contains only the 2 in-scope files; `out_of_scope_changes` names the other 2.
- No `scope_allow` → today's whole-tree behavior (regression pin).
- The reviewer input assembly uses the same scoped set (assert on its file list).

## Verification command

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_framework_edit_diff_scope.py tests/unit/test_publisher_py.py   # must exit 0
```

(If `test_publisher_py.py` does not exist under that name, substitute the publisher test module
that asserts on `implementer-summary.json` `files_changed`.)

## Done when

- Verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/cli/execute.py tests/unit/test_framework_edit_diff_scope.py` → clean.
- Read-only proof: replay the live shape — in a temp clone, run the derivation with
  `scope_allow` = the 3 files of commit 3a913a81 and the 4 foreign files present → diff lists
  exactly the 3. Paste the output.
- `git diff --stat` touches only files in scope.
