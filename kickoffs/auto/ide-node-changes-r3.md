# IDE node changes — revision 3 (Opus review of run ide-node-changes-r2-20261007100450)

WIP commit 0fccc7ba passed fixes 1, 3, 5, 6, 7, 8, 9 (verified by Opus: 83 tests pass, all new tests
fail on the old HEAD, perf OK). Fix only the list below.

## Files in scope

- `mini_ork/ide_pages/node_changes.py`
- `tests/unit/test_ide_pages_node_changes.py`

Never add `uv.lock` or any file outside scope (r2 committed an 887 KB untracked `uv.lock`; delete it
from the tree before finishing if it reappears).

## Fixes (exact)

1. **Executor-shape verifier JSON regressed** (`node_changes.py:~248`). When neither a `checks[]`
   JSON nor a TSV resolves, keep the old reader as the fallback: a verdict row from
   `verifier_<stem>.json` keys `pass` / `post_rc` / `error_summary` / `evidence_path`, plus the log
   tail. `verifier_vnode.json = {"pass": false, "post_rc": 2, "error_summary": "pytest failed"}` must
   render a red row "pytest failed" (rc 2) and the log tail, never "No verifier artefacts found".
2. **Tolerant verifier JSON read.** Real `verifier_static-check.json` starts with a
   `DeprecationWarning` line, so `json.load` fails. Parse from the first line that starts with `{`
   (to the end of the file); only when that fails, fall back to the TSV.
3. **Real-shape check rows.** TSV/JSON check rows get `sub = "rc <n> · <log name>"` when an rc or
   log is known (from the TSV columns or the matching `evidence/<stem>*.log`), `path` = that log's
   absolute path. Build the kickoff's fixture: node `static_check_verifier` with ref
   `verifiers/static-check.py`, `verifier_static-check.json` (with a leading DeprecationWarning
   line), `verifier-static-check.checks.tsv` (copy the real columns from
   `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/runs/ide-node-changes-r2-20261007100450/verifier-static-check.checks.tsv`)
   and `evidence/static-check.log`; assert names, rc/log subs and paths.
4. **Git-diff fallback** (`:~633`). Records live at `<home>/worktrees/<run_id>.json`
   (`mini_ork/workspaces.py:246 _record_path`), not `run_dir.parent / "worktrees"`, and never take
   another run's record. Pass `run` into `_load_diffs` and use `run.workspace` (base sha, branch);
   `git -C <project> diff <base>..<branch>` (timeout 5 s); no recorded base →
   `merge-base origin/main <branch>`. Count +/- per file from the hunks, keep the text.
5. **One diff source, one text.** `_load_diffs` returns `(entries, source_text, source_name)`;
   `_diff_text` returns that text (capped as today) — never concatenate `review-diff.patch` and
   `framework-edit.diff`. Prefer `framework-edit.diff` when `review-diff.patch` is truncated
   (shorter and ends mid-hunk, or the reviewer truncated it at 50 KB); otherwise `review-diff.patch`.
   `files[]` counts and the text must come from the same source.
6. Delete the dead placeholder at `:320` (`mark = "✓" if not passed else "✓"`) and fix the payload
   docstring at `:83` to `{path, added, removed, abs}`.

## Tests (each must fail on 0fccc7ba)

Fixes 1-5 each get a test: executor-shape JSON row + tail; DeprecationWarning-prefixed JSON parses;
the static-check fixture rows (sub + path); git fallback with a tmp repo + `<home>/worktrees/<run>.json`
record (and a second run's record that must be ignored); both patch files present → the diff text
holds exactly one `diff --git` per file and matches the counted source.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node_changes.py tests/unit/test_ide_pages_node.py` passes — paste the summary line.
- `ruff check mini_ork/ide_pages/node_changes.py tests/unit/test_ide_pages_node_changes.py` is clean.
- `MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork bin/mini-ork board node ide-node-changes-r2-20261007100450 static_check_verifier --view changes` and the same for `implementer`: paste item counts, the first 3 item rows, file count, `diff` length and wall time (< 1.5 s each).
- `git status --short` shows only files in scope.
