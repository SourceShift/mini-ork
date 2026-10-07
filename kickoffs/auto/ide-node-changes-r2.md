# IDE node changes + full documents — revision 2 (Opus review of run ide-node-changes-20261007091357)

The WIP commit 4ff6a5fe holds the first cut. Fix only what is listed; keep the payload shape
(`result`, `files`, `diff`, `diff_note`, `commits`, `commits_note`) unchanged.

## Files in scope

- `mini_ork/ide_pages/node_changes.py`, `mini_ork/ide_pages/node.py`
- `tests/unit/test_ide_pages_node_changes.py`, `tests/unit/test_ide_pages_node.py`

## Fixes (exact)

1. **Nodes before the first code change get no diff** (`node_changes.py:358-383`
   `_show_diff_for`). When either start time is missing, do not return True: fall back to
   workflow order (index in `run.nodes`) — a node shows the diff only if a code-changing node
   sits at or before it. A planner with `start=None` must return `files == []` and
   `diff_note == "No code changed by this point."`.
2. **Verifier artefacts by verifier stem, not node id** (`:191-236`). Derive the stem from the
   node's verifier ref (`node.prompt` basename without `.py`, e.g. `static-check`, `test`) and
   also try the node id. Read, in order: `verifier_<stem>.json` (its `checks` array),
   `verifier-<stem>.checks.tsv`, `evidence/<stem>*.log`. Each check item: `t` = check name,
   `sub` = `rc <n> · <log name>`, `path` = absolute log path when it exists, colour by pass/fail.
3. **Lens nodes** (`:109-120`, `:152-190`). Treat a node as a review node when its type is in
   the review set OR `"lens" in node.id` OR `"review" in node.id` (same rule as
   `run.py:614`). Find its report with `node._report_paths(run_dir, node.id)` (lazy import;
   it strips `_lens` → `lens-code_impact.md`). Findings come from the markdown headings/bullets.
4. **Patch-file fallback returns the patch** (`:426-500`). When the source is
   `review-diff.patch` / `framework-edit.diff` (prefer `review-diff.patch`, then
   `framework-edit.diff`), `diff` = the patch text (capped as today) and each file's
   `added`/`removed` = the `+`/`-` line counts from its hunks (exclude `+++`/`---` headers).
   If neither a patch nor acp diffs exist but the run has a workspace branch, use
   `git -C <project> diff <base>..<branch>` (timeout 5 s; base = the run's recorded base or
   `merge-base origin/main <branch>`); otherwise the existing note.
5. **Load diffs once** (`:432`, `:500`): `build_changes_view` calls
   `cached_or_computed` once and passes the entries to `_files_and_note` and `_diff_text`.
6. **Finding paths** (`:287-315`): resolve relative paths against the project root, then the
   run's workspace path when it has one; set `item["path"]` to the absolute path when the file
   exists, and keep the Open act. Never resolve against the process cwd.
7. **Reuse `board_cmd._project_file`** via a lazy import inside the function (as `run.py:732`
   does); delete `_resolve_project_file` (`:608`) and the wrong layering note in the module
   docstring (lines 26-29).
8. **Prompt markdown** (`node.py:~1354`): when there is no transcript prompt, read the file that
   `_resolve_prompt_file` finds and use its contents as `markdown.text`; fall back to the ref
   only when no file resolves.
9. **Payload size** (`node.py:~847`, `~901`): keep `lines` trimmed to the legacy cap; only
   `arg` (+ `md: true`) carries the full text. Delete the orphan comment at `node.py:59-63`.

## Tests (each must fail on 4ff6a5fe)

- Planner with `start=None`, implementer later in `run.nodes` with a diff → planner gets no
  files and the "No code changed by this point." note; the implementer gets the files.
- Verifier node `static_check_verifier` with ref `.../verifiers/static-check.py` and
  `verifier_static-check.json` + `verifier-static-check.checks.tsv` + `evidence/static-check.log`
  → items list the checks with rc and log path.
- `code_impact_lens` (type `researcher`) with `lens-code_impact.md` → findings from the report.
- Only `framework-edit.diff` present (no acp diffs) → non-empty `diff`, per-file +/- equal to
  the hunk line counts.
- A reviewer finding with a repo-relative path that exists in the project → `item["path"]` is
  absolute; run the test from a different cwd (`monkeypatch.chdir(tmp_path / "elsewhere")`).
- `cached_or_computed` is called once per `build_changes_view` (spy).
- A node with no transcript → prompt `markdown.text` equals the prompt file's contents.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node_changes.py tests/unit/test_ide_pages_node.py tests/unit/test_board_cmd.py` passes — paste the summary line.
- `ruff check` on the touched files is clean.
- On this repo's run `ide-node-changes-20261007091357`
  (`MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork bin/mini-ork board node ide-node-changes-20261007091357 <node> --view changes`)
  for `planner`, `code_impact_lens`, `implementer`, `static_check_verifier`, `reviewer`:
  paste item / file / commit counts and wall time for each (each < 1.5 s).
- Diff touches only files in scope.
