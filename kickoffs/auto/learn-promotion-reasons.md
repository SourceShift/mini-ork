# Self-improve decisions in plain words, and whether each change is actually in a prompt

## Why (live evidence, 2026-10-07)

The Overview's "Recent learning events" shows rows like `Promotion quarantined: cand-46e544d167e14b0f ·
2026-10-05 · utility 1.00 → 1.00`. The user asked why these are quarantined, and the page can't
say. The answer is in `promotion_records.rationale`, which nothing shows. The 6 live quarantines:

- `cand-46e5…` (code_fix, `agent.reviewer.prompt`): "no strict-superset gain over the control arm
  (control_n=3) … the control also solved on at least one of 3 baseline retries". So it was no
  better than retrying the old prompt.
- `cand-7a1d…` (code_fix): "per-task no-regression gate: 1 previously-solved held-out task(s) now
  fail … [probe-2.md]". It broke a task the old prompt solved.
- `cand-5158…`: "refusing promote: scorer=mock fabricates utility". The score was simulated.
- `cand-57e6…` (framework_edit, `agent.implementer.prompt`), `cand-7554…`, `cand-e00d…`: "probe
  scorer measured nothing (no frozen probe set under recipes/<recipe>/probes/, unresolvable
  target file, or budget exhausted)". One message hides three causes:
  - framework_edit has NO probe set; only `recipes/code-fix/probes` and `recipes/obs-smoke/probes`
    exist;
  - 7554 and e00d never launched a probe (decided 17:02 and 17:06; the first probe run started
    17:11).
- 4 of the 6 come from the 2026-09-29 apply-loop smoke test: `apply_attempts.source_id LIKE
  'gr-smoke%'`.

The reverse problem also exists. 15 changes show "promoted" for task classes that have no probe
set. All are from 2026-09-12, with rationale "UNVETTED promote (scorer=mock fabricates utility;
operator-enabled via MO_APPLY_UNVETTED)" (commit 13b2f210 appended 18 directives to 10 recipe
prompts). And `cand-57e6` shows "quarantined" while its directive (gradient `gr-1c9d2fad50a1`) has
been live in `recipes/framework-edit/prompts/implementer.md` since then. Applied directives carry
the marker `<!-- applied:gradient_records:<source_id> -->` in the prompt file.

Data:
- `promotion_records(promotion_id, candidate_id, utility_before, utility_after, rationale,
  decision, decided_at, decided_by)`, where `decided_at` is ISO with a malformed `…:fZ` tail
  sometimes; parse defensively
- `apply_attempts(candidate_id, task_class, target_kind, target_name, source_kind, source_id, …)`
- `workflow_candidates(candidate_id, mutations)`: JSON list with `kind`, `node_name`, `new_val`
  (the proposed text)
- `gradient_records(gradient_id, signal, suggested_change)` for `source_id`

## Files in scope (touch ONLY these)

- `mini_ork/learning/promotion_explain.py` (new, pure + read-only helpers)
- `mini_ork/ide_pages/learn/improve.py`
- `mini_ork/ide_pages/learn/overview.py`: ONLY the promotion item inside the recent-learning-events function
- `tests/unit/test_promotion_explain.py` (new)

Do NOT modify any other file.

## `promotion_explain.py` (exact)

1. `explain(decision, rationale, *, task_class, source_id, repo_root) -> dict` → `{"label",
   "reason", "colour", "test_run": bool}`. First match wins:
   | match in rationale | label | reason | colour |
   |---|---|---|---|
   | `no strict-superset gain` | Not better | "Solved nothing the old prompt didn't also solve when simply retried (control: N runs)" | yellow |
   | `per-task no-regression` | Broke a task | "A held-out task the old prompt solved now fails (<probe ids found in [..]>)" | red |
   | `scorer=mock` and decision quarantined | Score was simulated | "Refused: the mock scorer makes up numbers; nothing was measured" | muted |
   | `UNVETTED promote` | Applied without evaluation | "Promoted by operator override (MO_APPLY_UNVETTED) on a simulated score" | orange |
   | `measured nothing` | Never evaluated | `f"No probe set for {task_class} (recipes/<recipe>/probes/ missing)"` if that dir doesn't exist under `repo_root`; else "Probes did not run (launch error or budget)" | muted |
   | `McNemar` or `not significant` | Not significant | "The improvement could be chance (significance test failed)" | yellow |
   | decision promoted (other) | Applied | `f"Solved more held-out tasks: {before:.2f} → {after:.2f}"` | green |
   | else | the decision, title-cased | first 160 chars of the rationale | sub |

   Recipe dir from task_class: `_` → `-`. `test_run` = `source_id` starts with `gr-smoke`.
2. `live_in_prompt(source_id, repo_root) -> str | None`: the repo-relative path of the first
   `recipes/*/prompts/*.md` containing `<!-- applied:gradient_records:{source_id} -->`, else None.
3. `decisions(db_conn, repo_root, *, limit=50) -> list[dict]`: newest first, joined:
   - from the tables: candidate, task_class, target, decided_at (epoch), decision, before/after,
     full rationale, full proposal text (`new_val` of the first mutation), source gradient
     signal / suggested_change;
   - from 1 and 2: `explain(...)` and `live_in_prompt(...)`.
   Tolerates missing tables or columns.

## UI

1. **Overview → Recent learning events, promotion items:** title `f"{label}: {target}"` (e.g.
   "Not better: agent.reviewer.prompt (code_fix)"); sub = `f"{date} · {reason}"`, plus `" · in use
   in <path>"` when live, plus `" · test run"` when it is one. Colour from explain. The row action
   opens Self-improve with that decision selected: `S.page_link("learn", "improve",
   decision=<candidate_id>)`.
2. **Self-improve: a new first section "Changes proposed to your prompts"** (`S.table`, full
   width). Columns `date | change | outcome | in use`:
   - change = `f"{target} ({task_class}): {proposal[:110]}"`
   - outcome = label (coloured)
   - in use = the prompt path or `—`
   - rows from `decisions()`; rows with `test_run` are hidden unless `args.get("tests") == "1"`
     (a chip toggles it);
   - each row `do = S.set_args(decision=<candidate_id>)`.
   When `decision` is set, show BEFORE the table a full-width `S.markdown` titled
   `f"{label} · {target}"` with the full proposal, the full reason, the full rationale (in a
   quote block), and the source gradient's observation and suggestion. Then a kv: decided at,
   before → after, in use (path) with an `Open prompt` action (`S.open_path`) when live, and
   Close. When a change is "Applied without evaluation" and live, add a muted line: "This change
   is in your prompt but was never measured."
3. Keep the existing Self-improve sections after the new one.

## Tests (`tests/unit/test_promotion_explain.py`, temp repo root + DB)

- `explain` on each of the 6 live rationales above (copy them verbatim) plus one UNVETTED promote
  → the exact label, reason and colour. `measured nothing` → "No probe set for framework_edit"
  when `recipes/framework-edit/probes` is absent, "Probes did not run" when present.
- `live_in_prompt` finds the marker in a temp `recipes/x/prompts/implementer.md` and returns
  None otherwise.
- `decisions` joins correctly; `test_run` set for a `gr-smoke…` source.
- Overview events show the plain label/reason; Self-improve hides test runs by default, shows them
  with `tests=1`, and the detail markdown contains the full proposal and rationale.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_promotion_explain.py tests/unit/test_ide_pages_learn.py tests/unit/test_ide_pages_learn_overview.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/promotion_explain.py mini_ork/ide_pages/learn/improve.py mini_ork/ide_pages/learn/overview.py tests/unit/test_promotion_explain.py` → clean.
- Live proof (read-only): render `learn/improve` and `learn/overview` from the live home, then
  `improve` with `decision=cand-57e680e88b0d4eb3`. It must say "Never evaluated: No probe set for
  framework_edit" AND "in use: recipes/framework-edit/prompts/implementer.md". Paste the relevant
  rows and the detail.
- `git diff --stat` touches only the files in scope.
