# In-place publish commits only the run's AUTHORED patch — never whole files, never a shared-index commit

## Why (live, 2026-10-08)

**What happened.** Run `ide-orca-b2b-story-20261008113340` (code-fix, in place on
`/Volumes/docker-ssd/ps/zed-mini-ork`) was revived with
`recover --from-node test --carry-patch salvage.patch`. Its publisher logged
`[warn] publisher: artifact_contract.yaml has no outputs[] — skipping publish` and then
`[publish] committed 4 file(s): 2f94dc61` (the M1 empty-outputs in-place implementer commit). It
did a whole-file `git add` of components.rs, graph.rs, page.rs and sections.rs.

**The damage.** Another session (tmux pane %218) was editing graph.rs and components.rs at
14:00–14:03 local, inside the run's window. Its uncommitted `ThrottledSpinExt` work got committed
under the run's name.

**The same root on the diff side.** The framework-edit diff swept a peer's files into the
reviewer diff (`kickoffs/auto/framework-edit-diff-scope.md` specs that side).

**Two kinds of foreign change** (agreed with the mini-ork-ef orchestrator session):
- **Pre-existing foreign hunks** live in `pre-implementer-ref`, so a pre→post tree diff already
  excludes them.
- **In-window foreign hunks** inside an in-scope file ride in ANY tree diff. That includes
  `framework-edit.diff` and `review-diff.patch`, which are both `git diff` against
  `pre-implementer-ref`. Only the run's AUTHORED patch separates them:
  - **a recover:** the carry patch (`salvage.patch` / `--carry-patch`);
  - **a fresh run:** the implementer's own Write/Edit calls replayed in order from its session
    transcript. Nothing else.

## Files in scope (touch ONLY these)

- `mini_ork/cli/publisher.py`: ONLY the in-place commit path. Today that is
  `_publisher_try_commit_files` (~:80, the whole-file `git add` behind `[publish] committed N
  file(s)` at ~:166), plus the M1 empty-outputs implementer commit that calls it.
- `mini_ork/cli/publisher_authored_patch.py` (new, net-new logic; publisher.py reads no
  pre-implementer-ref, carry or salvage today):
  - `resolve_authored_patch(run_dir) -> (patch_text, source)` or an abstain reason;
  - `land_patch(repo, patch_text, branch) -> sha` or an abstain reason (the private-index
    landing).
- `tests/unit/test_publisher_authored_patch.py` (new)

No execute.py or execute_handlers.py change is needed: the fresh-run patch comes from the
transcript, not a persisted file.

## Changes (exact)

1. **Resolve the authored patch.** Only patches the run itself wrote count:
   - **a recover:** the carry patch (`salvage.patch` / `--carry-patch`, named in the recover
     hand-off or `run_profile.json`);
   - **a fresh run:** the implementer's own Write/Edit calls replayed IN ORDER from its session
     transcript (`<run_dir>/sessions/…` / `agent-implementer.live.jsonl`) into a patch against
     `pre-implementer-ref`.
     - When the replay does not reproduce the final content of a touched file, a peer edited it
       between the implementer's Read and Write. Do not guess: abstain `publish-unattributable`,
       listing the file.

   `framework-edit.diff` and `review-diff.patch` are TREE deltas (`git diff` against
   `pre-implementer-ref`). They are NOT authored sources, not even as a fallback: use them only
   as the cross-check in step 3.

   Scope the authored patch to the run's declared files with the existing helper
   `mini_ork.memory.preferences.scope_paths(run_dir)` (`run_profile.json` → `scope_allow`, the
   same paths the prompt-injection path uses). Do not write a second scope parser. When it returns
   `[]` (no declared scope), do not filter by scope.
2. **Land through a private index, parented on the CURRENT HEAD.** The parent must never be
   `pre-implementer-ref`: a tree built from it and committed onto HEAD would orphan or revert
   every commit that landed in between.
   - `GIT_INDEX_FILE=<tmp> git read-tree HEAD` (the current HEAD).
   - `git apply --cached --check` the authored patch:
     - **it applies cleanly** → `git apply --cached`, `git write-tree`, `git commit-tree -p HEAD`,
       then `git update-ref refs/heads/<branch> <new> <old HEAD>` (compare-and-swap);
     - **it does not apply cleanly** (HEAD moved in a conflicting way) → abstain
       `publish-conflict`, HEAD unchanged. NEVER use a silent `--3way` auto-merge, and never fall
       back to `git add`.
   - Never touch the shared `.git/index`. Another session's `git commit` would sweep a staged
     shared index.
3. **Cross-check:**
   - Hunks in `git diff <pre-implementer-ref>` (working tree) that are NOT in the authored patch
     are foreign. List them in `<run_dir>/publish-foreign-hunks.txt` and print `[warn] publisher:
     N foreign hunk(s) left uncommitted`.
   - Never commit them, never revert them.
4. **The working tree is left exactly as it was.** Only a commit object and the ref move. The
   run's own changes are now committed, so they show as clean against the new HEAD. Foreign
   changes stay as uncommitted modifications.

## Tests (`tests/unit/test_publisher_authored_patch.py`, temp git repos)

- A run patch touching `a.py`, plus a foreign uncommitted edit in a DIFFERENT hunk of `a.py`
  made "in-window" → the commit contains only the run hunk; the foreign hunk is still a working
  tree modification and is listed in `publish-foreign-hunks.txt`.
- A foreign edit in an out-of-scope file → not committed.
- HEAD moved by an unrelated commit (a different file) → the commit is parented on the new HEAD,
  and that unrelated commit is still an ancestor (not orphaned, not reverted). A conflicting move
  → abstain `publish-conflict`, HEAD unchanged.
- A fresh-run fixture whose transcript Write/Edit replay does not match the final file (a peer
  edit in between) → abstain `publish-unattributable`.
- The shared index (`.git/index`) is byte-identical before and after.
- A recover with a carry patch → the commit equals the carry patch exactly.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_publisher_authored_patch.py tests/unit/test_mini_ork_publisher_py.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/cli/publisher.py tests/unit/test_publisher_authored_patch.py` → clean.
- `git diff --stat` touches only the files in scope.
