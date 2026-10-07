# Only verified learnings in node prompts at dispatch

## The rule (user decision, 2026-10-07)

"Only verified learnings should be added to the prompts." This covers what every researcher /
implementer / reviewer / planner is given at dispatch, through
`mini_ork.context_assembler.failure_modes_md` (called by `_learned_block` in
`mini_ork/cli/execute.py` and by the planner in `mini_ork/cli/plan.py:455`).

What counts as verified:
- **Approved emergent pattern with an authored lesson.** It passed the gate: strength ≥ 3,
  independent runs ≥ floor, and a non-blank `lesson_text` written by grounded pattern induction
  (`reflection_pipeline.reflection_verify_patterns`). This is injected today; keep it.
- **Verified theme lesson:** a `lesson_themes` row with `status='verified'`, `kind='task'`,
  non-blank `lesson_text`. A later phase sets `verified`; today there are none, and that is
  fine.
- **Operator preferences and steering:** authored by the user, so always allowed (untouched).

What is NOT verified and must stop being injected:
- **Raw `gradient_records` rows.** Today `failure_modes_md` injects the top N gradients for the
  task class by the extractor's OWN confidence (≥ 0.6). That is an LLM's self-rating, not a
  verification. Live example of what implementers get today:
  `- [workflow.node.verify] The node finished with status "success" even though
  verifier_output was {"verdict":"partial"}. Fix applied going forward: …`. These are mostly
  mini-ork's own trace complaints: 58% of gradients name trace fields; themes classify 3,201 of
  5,203 as `framework`.

## Files in scope (touch ONLY these)

- `mini_ork/context_assembler.py`: ONLY `failure_modes_md` and a new private helper for verified
  theme lessons
- `mini_ork/web/repositories.py`: ONLY `fetch_failure_mode_gradients` (its docstring says the
  filter MUST mirror `failure_modes_md`)
- `tests/unit/test_context_assembler_py.py`, `tests/unit/test_inject_verified_only.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **`failure_modes_md`:** the raw-gradient section is emitted ONLY when
   `MO_INJECT_UNVERIFIED=1` (default unset = off). With it off, `sources` gets no `gradient`
   entries.
2. **New section, verified theme lessons:** `lesson_themes` rows with `status='verified'`,
   `kind='task'`, non-blank `lesson_text`, and `task_class` equal to the task class or `'*'`.
   Order by `n_runs DESC, last_seen DESC`, limit = the same LIMBO limit the gradient section
   used.
   - Header: `--- Verified lessons from prior runs ({task_class}) ---`.
   - One line per lesson: `- {lesson_text}  (seen in {n_runs} runs)`.
   - Closer: `--- /verified lessons ---`.
   - `sources` entries: `{"kind": "theme", "id": theme_id, "text": lesson_text}`.
   Tolerate a missing table or columns: no section, no error.
3. **The pattern section** (approved + lesson) is unchanged.
4. **Empty result:** when nothing verified exists, return `""` (no headers at all), so
   `_learned_block` records `injected: false, reason: "nothing matched"`.
5. **`fetch_failure_mode_gradients`:** mirror the rule. Return verified theme lessons in the same
   row shape the web route expects (map `lesson_text` → the signal-like field, `theme_id` → id).
   Return raw gradients only under `MO_INJECT_UNVERIFIED=1`. Keep the function name.
6. Module docstring note: "Only verified learnings reach prompts (user rule 2026-10-07):
   approved+lessoned patterns, verified theme lessons, and operator preferences. Raw gradients are
   evidence for verification, not guidance."

## Tests

- `tests/unit/test_inject_verified_only.py` (temp DB):
  - gradients only → `failure_modes_md` returns `""` and sources are empty;
  - with `MO_INJECT_UNVERIFIED=1` → today's gradient section is back;
  - a verified task theme for the class → the verified-lessons section with `(seen in N runs)`
    and a `theme` source;
  - a `candidate` theme, a `framework` theme, a theme for another class, or a blank lesson → not
    injected;
  - an approved pattern with a lesson → still injected.
- `tests/unit/test_context_assembler_py.py`: existing tests that seed gradients and expect them
  injected set `MO_INJECT_UNVERIFIED=1` via monkeypatch (do not delete them). Add one asserting
  the default excludes gradients.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_inject_verified_only.py tests/unit/test_context_assembler_py.py tests/unit/test_learned_record.py tests/unit/test_mini_ork_plan_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/context_assembler.py mini_ork/web/repositories.py tests/unit/test_inject_verified_only.py` → clean.
- Live proof (read-only): `MINI_ORK_DB=/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db MO_SEMANTIC_INJECT=0 python3.11 -c 'from mini_ork import context_assembler as ca; s=[]; print(ca.failure_modes_md("framework_edit", 5, node_type="implementer", sources=s)); print([x["kind"] for x in s])'`.
  It shows only the lessons-from-recurring-patterns block (no raw gradient lines), and sources
  have no `gradient`. Paste it.
- `git diff --stat` touches only the files in scope.
