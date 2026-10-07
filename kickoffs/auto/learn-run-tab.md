# IDE run Learnings tab — show the text, scope "produced" to this run

## Why

`_learnings_tab` (`mini_ork/ide_pages/run.py:788`) has the same defects as the node view:

1. **"Available to the run"** prints `"<N> items · e.g. <cite>"` per `context-pack.json` section.
   The cite is a storage key (`gradient_records/cross_class:workflow.node.implementer`). The real
   content is in the pack and never shown:
   - `known_failure_modes[]`: `signal`, `suggested_change`, `target`, `confidence`
   - `similar_lessons[]`: `title`, `suggested_fix`, `score`
   - `verified_emergent_patterns[]`: `cluster_label`, `suggested_change`, `strength_score`, and
     optionally `lesson_text`
   - `prior_similar_runs[]`: `trace_id`, `status`, `cost_usd`, `duration_ms`, `created_at`
   - `constraints[]`, `user_preferences` (list or dict)
2. **"Produced by the run"** selects `gradient_records` by time window + task class
   (`run.py:802-806`). Gradients from OTHER runs of the same class in that window show up as this
   run's. The correct join, verified on the live DB:
   `gradient_records g JOIN execution_traces t ON t.trace_id = g.evidence WHERE t.run_id = ?`.
   Trace ids look like `tr-<node_type>-<node_id>-<hash>`, e.g.
   `tr-researcher-prior_art_lens-7cc5b827`.
3. Item titles lead with the `gradient_id` instead of the finding.

The pack is assembled once, for the planner (`mini_ork/cli/plan.py:499`). Say so in the UI; each
node's own injected learning is on that node's Learning tab (a parallel change).

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/run.py`: ONLY `_learnings_tab` and new private helpers it calls. Two other
  worktrees are editing `_overview_tab` and `build` in this file; do not touch them.
- `tests/unit/test_ide_pages_run.py`: ONLY `test_agents_learnings_and_artifacts`, if an assert
  there must change
- `tests/unit/test_ide_pages_run_learnings.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **Produced by the run.** Replace the window query with the run-scoped join above (bind
   `run.id`), ordered by `g.created_at DESC`, limit 20. Item: title = `signal` (first 200 chars),
   sub = `"fix: " + suggested_change` (first 160) + `f" · {node} · confidence {c:.2f}"`, where
   `node` is the node id parsed from the trace id. Strip the `tr-<type>-` prefix and the trailing
   `-<hash>`; if it doesn't parse, use the trace id. `m="✦"`, `mc="purple"`. Keep the
   `learning_record` rows and the empty-state item as they are. Update the section note to
   "Gradients reflection wrote about this run's own nodes." Tolerate a missing
   `execution_traces` table (no rows, no exception).
2. **Available to the run (summary).** Keep the list and its items (`S.ok` with count / `S.dot`
   "none given"), but the sub is just `"N item(s)"`, with no cite. Set the note to: "Context pack
   assembled for the planner at plan time. Each node's own injected learning is on its Learning
   tab."
3. **One detail list per non-empty pack section**, after the summary, titled
   `f"{label} · {n}"` and `full=True`. At most 8 items each. If there are more, a last item
   `f"+{n-8} more"` with `acts=[S.btn("Open", S.open_path(str(pack_path)), "ghost")]`. Items:
   - failure mode: title = `signal`[:200], sub = `"fix: " + suggested_change`[:160] +
     `" · " + target` + `f" · confidence {c:.2f}"` when present
   - similar lesson: title = `title`[:200], sub = `"fix: " + suggested_fix`[:160] +
     `f" · similarity {score:.2f}"`
   - verified pattern: title = `lesson_text` if non-empty, else `cluster_label`; sub =
     `f"pattern {id} · strength {s:.0f}"`, plus `" · no authored lesson (frequency only)"` when
     `lesson_text` is empty. The pattern id is the part of `cite` after `/`.
   - prior run: title = `f"{status} · {trace_id}"`, sub = `f"${cost:.2f} · {duration_s}s ·
     {created_at[:16]}"`, `mc="green"` for success, `"red"` for failure, else `"sub"`
   - constraint / preference: title = `str(item)`[:200] (for a dict, `f"{k}: {v}"` per entry)
4. Keep the "Graph context", "Pack size" and "Operator steering" items as they are.

## Tests (`tests/unit/test_ide_pages_run_learnings.py`, reuse the seeding helpers of `tests/unit/test_ide_pages_run.py` by importing them)

- Two runs overlapping in time, same task class, each with its own trace + gradient → each run's
  "Produced by the run" shows only its own gradient. The title is the signal text, the sub names
  the node (`prior_art_lens` parsed from `tr-researcher-prior_art_lens-abc123`).
- A pack with 10 failure modes → a "Learned failure modes · 10" list with 8 detail items + a
  "+2 more" item with an Open action. Titles are signal texts, not cites.
- A pattern without `lesson_text` → title = cluster_label, sub contains "no authored lesson".
  With `lesson_text` → title = the lesson.
- The summary sub has no `gradient_records/` substring anywhere.
- `test_agents_learnings_and_artifacts` in `tests/unit/test_ide_pages_run.py` still passes.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_run_learnings.py tests/unit/test_ide_pages_run.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/run.py tests/unit/test_ide_pages_run_learnings.py` → clean.
- Read-only proof: `python3.11 -c 'import json; from pathlib import Path; from mini_ork.ide_pages.run import build; print(json.dumps(build(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learnings", {"run": "retry-hint-20261007103745"}), indent=1)[:4000])'`
  shows failure-mode signal text and lesson titles, no storage keys as titles. Paste it.
- `git diff --stat` touches only the files in scope, and in `run.py` only `_learnings_tab` + helpers.
