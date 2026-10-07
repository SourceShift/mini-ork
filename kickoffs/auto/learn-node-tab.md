# IDE node Learning tab — show what this node was given and what was learned from it

## Why

`_learning_view` (`mini_ork/ide_pages/node.py:1515`) renders nothing useful:

1. It reads `context-pack.json`. That pack is built ONCE, for `workflow_node: "planner"`
   (`mini_ork/cli/plan.py:499`), and no prompt includes it. So every node (implementer, gates,
   shell nodes) shows the same planner-time pack, which this node never saw.
2. For each pack section it prints only `"<label> · N"` and the first item's `cite`, a storage key
   such as `gradient_records/cross_class:workflow.node.implementer`. The useful fields (`signal`,
   `suggested_change`, `title`, `suggested_fix`) are dropped.
3. Its "gradients" section selects `gradient_records` by time window + task class only
   (`node.py:1565-1576`). Gradients from OTHER runs of the same class that overlap in time show
   up as this node's.
4. A `Gates` row repeats the Gates tile shown right above the list.

A parallel worktree (`learn-inject`) makes execute write, for every researcher / implementer /
reviewer node, exactly what was injected into its prompt:

- `<run_dir>/learned/<node_id>.md`: the injected text (present only when something was injected)
- `<run_dir>/learned/<node_id>.json`: `{"node_id", "node_type", "lane", "task_class", "attempt",
  "written_at", "injected": bool, "reason": "" | "opt-out" | "nothing matched", "sources": [
  {"kind": "gradient", "id", "target", "signal", "suggested_change"} |
  {"kind": "pattern", "id", "text"} |
  {"kind": "steering", "id", "severity", "source", "message"} ]}`

Build against this contract. Runs from before that change have no `learned/` dir and must say so.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/node.py`: ONLY `_learning_view` and new private helpers it calls.
  Another worktree is editing other parts of this file.
- `tests/unit/test_ide_pages_node.py`

Do NOT modify any other file.

## New `_learning_view(run, node)` output (exact)

Return `{"list_title": "Learning", "list": [...], "markdown": {...}}`. The `markdown` key is only
included when there is injected text. The Zed panel (`render_facts`) already renders `kv`, then
`markdown` (title + text + Open button when `path` is set), then `list`.

1. **Injected into this prompt.** If `<run.run_dir>/learned/<node.id>.md` exists, set
   `markdown = {"title": "Injected into this node's prompt", "text": <file text capped at
   MD_FILE_CAP>, "path": <absolute file path>}`.
2. **Sources list.** If the `.json` exists, add one `S.item` per source, in order:
   - gradient: title = `signal` (first 200 chars), sub = `"fix: " + suggested_change` (first
     200 chars) + `" · " + target`, `m="✦"`, `mc="purple"`
   - pattern: title = `text` (first 200 chars), sub = `"pattern " + id`, `m="◆"`, `mc="purple"`
   - steering: title = `message`, sub = `"steering · " + severity + " · from " + source`,
     `m="→"`, `mc="blue"`
   If the `.json` has `injected: false`, add one item: title `"Nothing injected"`, sub = the
   reason (`"opt-out"` → `"MO_INJECT_LEARNINGS=0 for this run"`, `"nothing matched"` →
   `"no learned failure modes or lessons matched this task class"`).
3. **No record.** No `.json` file at all:
   - if `node.type` is not `researcher` / `implementer` / `reviewer`: a single item
     `"No learning is injected into this node type"`, sub `node.type or "shell"`;
   - otherwise a single item `"Not recorded"`, sub `"this run predates per-node learning
     records"`.
4. **Pending steering.** Keep today's section 2 (`_fetch_steer_rows` filtered by
   `_role_for_node`), but title each item `"Pending steering · [sev]"` so it can't be confused
   with steering that was already injected.
5. **Learned from this node.** Replace the time-window query. Join
   `gradient_records g JOIN execution_traces t ON t.trace_id = g.evidence WHERE t.run_id = ?`
   (bind `run.id`), then keep rows whose `trace_id` starts with `f"tr-{node.type}-{node.id}-"`.
   Do this prefix filter in Python: node ids contain `_`, which is a LIKE wildcard. Order by
   `g.created_at DESC`, cap at 10. Each row is an `S.item`: title = `signal` (first 200 chars),
   sub = `"fix: " + suggested_change` (first 160) + `f" · confidence {c:.2f}"`, `m="✦"`,
   `mc="green"`, with the existing `Open` action. If there are no rows and the node has finished,
   add one item: `"Nothing learned from this node yet"`, sub `"reflection writes gradients after
   the run's last node"`. Tolerate a missing `execution_traces` or `gradient_records` table.
6. **Remove** the context-pack section and the `Gates` row from this view entirely.
7. If the list would still be empty, keep the `"No learning signals yet."` item.

## Tests (`tests/unit/test_ide_pages_node.py`)

- Replace `test_learning_view_gradient_window_uses_seconds` with a run-scoped test. Seed two runs
  that overlap in time, each with a trace `tr-implementer-implementer-<hash>` and a gradient whose
  `evidence` is that trace. The view for run A shows only run A's gradient. A gradient on
  `tr-researcher-prior_art_lens-<hash>` in run A does NOT appear on the `implementer` node, and
  does appear on the `prior_art_lens` node.
- `.md` + `.json` present → `out["markdown"]["text"]` equals the file text; the sources render in
  order with the titles above.
- `.json` with `injected: false, reason: "nothing matched"` → the "Nothing injected" item.
- No `learned/` dir: implementer → `"Not recorded"`; a shell node → `"No learning is injected
  into this node type"`.
- No item titled `"Gates"` and no item from `context-pack.json`, even when that file exists.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_node.py tests/unit/test_ide_pages_node_changes.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/node.py tests/unit/test_ide_pages_node.py` → clean.
- Read-only proof: `python3.11 -c 'import json; from pathlib import Path; from mini_ork.ide_pages.node import build_node; print(json.dumps(build_node(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "ide-kickoff-search-r4-20261007111217", "prior_art_lens", view="learning"), indent=1)[:3000])'`
  shows the run's own prior_art_lens gradients (signal text, not storage keys) and `"Not recorded"`.
  Paste it.
- `git diff --stat` touches only the files in scope.
