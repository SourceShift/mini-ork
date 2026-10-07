# Memory tab — what mini-ork knows about you, which lane fits which work, what to forget

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, phase P7b. Read its
"Usefulness contract" section. Every section below must fill all of its columns, and the reviewer
must reject any section that doesn't.

## Why

The Memory tab (`mini_ork/ide_pages/learn/memory.py`) shows two things today:

- **Namespace row counts** as bars (`task_memory 91`, `failure_memory 81`, …). These answer no
  question an operator has.
- **A lifecycle kv** (active / decaying / retired). It says "decaying" without saying which
  memories decay, and it ranks by raw win rate. Win rate is meaningless here: across
  `semantic_memory_uses` the outcomes are win 3,176 / loss 1,054 / pending 1,363, so the
  baseline is ~75%, and the most-used memories are content-free cluster labels.

Meanwhile `mini-ork prefs` (merged) lets the operator set preferences that now go first in every
LLM node's prompt, and the injection ledger (`mini_ork/learning/ledger.py`, table
`lesson_injections`) records each preference given to a node as `source_kind='preference'`,
`source_id = f"pref:{scope}:{target}:{key}"`. `agent_performance_memory` (128 rows, refreshed
today) has runs / successes / cost per (role, model, task_class).

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn/memory.py`
- `tests/unit/test_ide_pages_learn_memory.py` (new)

Do NOT modify any other file. Read-only helpers to use:
- `mini_ork.memory.preferences.list_prefs`
- `mini_ork.learning.ledger.injection_counts`
- `mini_ork.memory` (`candidates`, `retirement_state`, `RETIRE_*`)
- `mini_ork.ide_pages.learn._common`
- `mini_ork.ide_pages.spec`

## Sections (exact, in this order)

| Section | Question | Denominator / baseline | Evidence link | Action | Empty state |
|---|---|---|---|---|---|
| Preferences & constraints | What did I tell it, and does every run get it? | given to N nodes in the last 7 days | — | Remove / Open file; section: Add | "No preferences yet." + the command |
| Lane fit by task class | Which lane works for which kind of work? | runs, pass rate, cost per run per lane×role×class | → Lanes & cost | open lanes page | "No lane history yet — it fills as runs finish." |
| Memories to review | What should it forget? | each memory's win rate vs its scope's baseline, with n | — | Retire / Reactivate | "Nothing to review — no memory is doing worse than its baseline." |

1. **Preferences & constraints** (`S.lst`, full width). One item per `list_prefs()` entry:
   - title = value[:200]
   - sub = `"global"` or `f"{scope}: {target}"`, then `" · given to {n} node(s) in 7 days"` from
     `injection_counts("preference", [ids], since=now-7d)`, or `" · not given to any node in 7
     days"`
   - for `source` starting `file:`, add `" · from <filename>"`
   - DB entries: `acts=[S.btn("Remove", S.cli("prefs", "rm", key, "--scope", scope, "--target",
     target, confirm=f"Remove preference '{key}'?"), "ghost")]`
   - file entries: `acts=[S.btn("Open", S.open_path(path), "ghost")]`

   Section note: "Every researcher, implementer and reviewer gets these first in its prompt. Add
   from a terminal: mini-ork prefs set <key> <text> [--scope task_class --target <class>]".
   Section actions: `[S.btn("Add preference", {"thread": "I want to add a mini-ork preference. Ask
   me what it is and its scope, then run `mini-ork prefs set` for it."}, "primary")]`.
   Empty: `S.dot("No preferences yet", "Add one: mini-ork prefs set tone \"Keep summaries
   short\"")`.
2. **Lane fit by task class** (`S.table`, full width). From `agent_performance_memory`, rows with
   `runs_count >= 3`, grouped by `task_class` (classes ordered by total runs desc, top 8 classes,
   top 4 lanes per class by pass rate). Columns: `class | role | lane | runs | pass rate | cost /
   run`. Pass rate = success_count / runs_count, coloured green ≥ 70%, red < 40%. Within a class,
   mark the best lane (highest pass rate with runs ≥ 5) with `" ★"` after its name. Section
   actions: `[S.btn("Lanes & cost", S.page_link("lanes"), "ghost")]`. Note: "From
   agent_performance_memory (refreshed by reflect). ★ = best pass rate with at least 5 runs."
3. **Memories to review** (`S.table`, full width). Replaces today's lifecycle kv and the
   namespace bars, which go away.
   - Per scope, baseline = wins / (wins + losses) over `semantic_memory_uses` in that scope
     (pending excluded).
   - Per memory with ≥ `RETIRE_MIN_USES` resolved uses: its own win rate, n, and delta vs the
     scope baseline in points.
   - List memories whose delta ≤ −5 pts (doing worse than baseline), plus every candidate from
     `mini_ork.memory.candidates()`. Columns: `memory | scope | win rate | baseline | Δ | n |
     state`. Memory text [:140].
   - Row action: active → `S.cli("memory-lifecycle", "--retire", str(id),
     confirm="Retire this memory? It stops being injected; reactivate any time.")`; retired →
     `S.cli("memory-lifecycle", "--reactivate", str(id))`. Rows are `{"cells": …, "do": …}`.
   - Above the table, one `S.kv` line: active / retired counts and the overall baseline, with the
     note "Win = the run it was used in succeeded. Compared with the scope baseline, because most
     runs succeed either way. True per-lesson lift comes with the 10% holdout."
   - Empty: the empty-state text above.

Drop `_namespaces` and the old `_lifecycle` entirely.

## Tests (`tests/unit/test_ide_pages_learn_memory.py`, temp home + DB, frozen time)

- Prefs: a DB pref with 2 ledger injections in 7 days and one older → "given to 2 node(s)";
  Remove action args exact; a file pref has Open and no Remove; empty state text.
- Lane fit: rows below 3 runs hidden; ★ only on the best lane with ≥ 5 runs; pass rate colours.
- Memories to review: a scope with a 75% baseline; a memory at 60% with n=10 is listed with Δ
  −15 pts and a Retire action; a memory at 80% is not listed; a retired memory shows Reactivate.
- No section titled "Namespaces · state.db" remains.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_memory.py tests/unit/test_ide_pages_learn.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn/memory.py tests/unit/test_ide_pages_learn_memory.py` → clean.
- Read-only proof on the live DB:
  `python3.11 -c 'import json; from pathlib import Path; from mini_ork.ide_pages import build_page; print(json.dumps(build_page(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn", "memory", {}), indent=1)[:5000])'`.
  Paste it. Lane fit must show real classes; Memories to review must show baselines.
- `git diff --stat` touches only the files in scope.
