# IDE node detail: what a node did (Changes view) and full-length documents

## Goal

In the IDE's run graph, a node's inspector must show what the node actually did and
let the user read whole documents:
1. A **Changes** view: the node's result (review verdict + findings, verifier checks,
   planner plan, what a rollback reverted, files a node wrote), the code changed by
   the run up to and including this node (files, +/−, unified diff), and the run's
   commits.
2. Prompts, agent text and reports in full (not 400-char snippets), as markdown.

## Files in scope

- `mini_ork/ide_pages/node_changes.py` — new: the `changes` view
- `mini_ork/ide_pages/node.py` — view routing + full-length text (below)
- `mini_ork/cli/board_cmd.py` — add `changes` to `board node --view` choices
- `tests/unit/test_ide_pages_node_changes.py` (new), `tests/unit/test_ide_pages_node.py`

No other file changes.

## 1. `board node <run> <node> --view changes` (exact)

Common node fields as for other views, plus:
- `result`: `{"title": str, "items": [spec item]}` by node type:
  - review/lens/synthesizer/eval/judge: verdict (`approve`/`needs_revision`/…) as the
    first item (green/yellow/red), then each finding (`m` severity mark, `t` text,
    `sub` = `file:line`, `path` = absolute file when it exists in the project/worktree);
    sources: `review-*.json`, `verdict.json`, `lens-*.md` headings, `synthesis.md`.
  - verifier/test/typecheck/static_check: each check with pass/fail mark, `sub` = rc and
    evidence log, `path` = the log; sources: `verifier_<node>.json/.log`,
    `evidence/<node>*.log`, `verifier-<x>.checks.tsv`.
  - planner/decomposer: the plan steps from `plan.json`.
  - rollback: what was reverted / saved (`rolled-back.json`, saved-work refs).
  - other nodes: files the node wrote in the run dir (by name match), with `path`.
- `files`: `[{path, added, removed, abs}]` — the run's code changes as of this node
  (cumulative): empty for nodes that ran before the first code-changing node (implementer
  /worker/writer/drafter), with `diff_note` "No code changed by this point.";
  otherwise the run's diff (prefer `acp-diffs.json`, then `review-diff.patch`,
  `framework-edit.diff`, else `git diff <base>..<branch>` of the run's workspace when it
  still exists). `abs` = existing absolute path in the project (reuse
  `board_cmd._project_file`).
- `diff`: unified diff text for those files (cap 300 KB; `diff_note` says when capped).
- `commits`: `[{sha, subject, when, author, files}]` — commits on the run's branch since
  its base (workspace record `base_sha..branch`, or the commit recorded by the
  publisher in `publish.json`/`verdict.json`/`run-verdict.json`); empty with
  `commits_note` when there are none ("No commits from this run.").
- Read-only. < 1.5 s on a real run.

## 2. Full-length documents (exact)

- Stream `user` entry: `arg` = the whole first prompt (cap 200,000 chars), plus
  `"md": true`. `text` entries: whole text (cap 200,000), `"md": true`.
- `output` view: when the node's main report is a markdown file (`lens-*.md`,
  `synthesis.md`, `cycle-report.md`, `*.md` it wrote), add
  `"markdown": {"title": <file name>, "text": <whole file, cap 200 KB>, "path": <abs>}`.
- `prompt` view: add `"markdown": {"title": "Rendered prompt · as dispatched", "text":
  <the whole rendered prompt>, "path": <prompt file if any>}` (keep `block` for
  compatibility).

## Tests (`tests/unit/test_ide_pages_node_changes.py`, extend `test_ide_pages_node.py`)

A temp home with a run whose DAG is planner → implementer → verifier → reviewer →
publisher, an `acp-diffs.json` with two files, a verifier JSON + log, a review JSON with
2 findings (`file:line`), a `plan.json`, and a git workspace record with 2 commits.
Assert per node: planner `changes` → plan items, no files; implementer → files + diff,
commits; verifier → checks with log paths; reviewer → verdict first then findings with
paths; publisher → commits. Full-length: a 10,000-char prompt comes back whole with
`md: true`; output view of a lens node returns its `.md` whole.

## Done when

- `/tmp/chunked-gate.sh tests/unit/test_ide_pages_node_changes.py tests/unit/test_ide_pages_node.py tests/unit/test_board_cmd.py`
  passes (chunks avoid the machine's CPU guard); paste its last line.
- On run `run-le-1791321099-64879-1` (home `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`):
  `board node … synthesizer --view changes` and `… implementer --view changes` return
  `ok: true`; paste item/file/commit counts and timings.
- ruff clean on touched files; diff touches only files in scope.
