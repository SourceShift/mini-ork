# Context-pack / planner-injection audit — closeout (2026-10-09)

Tracks the context-pack audit items (A = bugs, B = gaps/hardening). Each row
names where the fix/enforcement lives so a future reader can verify it without
re-deriving the audit. Legend: **fixed** = code change landed; **by design** =
intentionally left as-is, with the rationale recorded here and at the source.

## A — defects

| Item | Subject | Status | Where |
|---|---|---|---|
| A1–A8 | Labelling / pack-accuracy defects (Tab rendered a non-injected pack; pack blocks mislabelled) | fixed | `216d17d7` (Learnings tab renders the INJECTED v2 pack + per-node ledger), `82acde38` (pack-accuracy: run-level prior_runs, same-class failure modes, lesson_text, relevance floor, hotspot recency) |
| A9 | `prior_similar_runs` carried a saturated `reward_g` (5k+ traces pinned at 1.0, incl. the verifier@v1 echo) → circular evidence | fixed | `mini_ork/context_assembler.py` — payload now carries a discriminating `outcome` (derived from `artifact_ref` + non-success node count), `reward_g` dropped |

## B — gaps / hardening

| Item | Subject | Status | Where |
|---|---|---|---|
| B1 | `textual_gradients` table superseded | fixed | migration `0068` drops it; `bin/mini-ork update` applied |
| B2 | Prompt directives (`applied:gradient_records:*`) must be *only verified learnings* | fixed | `mini_ork/learning/prompt_directives.py`; guard now scans the project overlay too (see below) |
| B3 | `lesson_themes` (verified lessons) never reached the v2 planner pack | fixed (consumer + **producer**) | consumer: `mini_ork/context_v2.py` `verified_themes()` + `build()`/`render()`. **Producer gap CLOSED** (`fc92bbbc`): `themes.promote` authors + verifies `lesson_text` for candidate themes and reflect runs call it — see `## B3` below |
| B4 | Shared-context planner blocks (role pack, ContextNest sessions, active-state index) | **by design (off)** | `mini_ork/cli/plan.py` `MO_PLANNER_SHARED_CONTEXT` — see below |
| B5 | `code_findings` injection into the v2 pack | already wired | v2 `build()` consumes `scope_findings` / `run_findings` / `prior_attempts`; live DB shows 1,553 `code_findings` rows. Remaining dark families are a producer-mix question, not a wiring gap |

## B2 — only-verified-learnings guard (detail)

Three layers, all present before this closeout, plus one scope fix:

1. **Forward gate** — `mini_ork/cli/apply.py` refuses to promote a directive whose
   scorer is in `FABRICATING_SCORERS` (`mock`, `gepa`); not env-configurable.
2. **Reversal** — `prompt_directives.revert_unverified` + `apply --revert-unverified`
   ([--files-only | --db-only]).
3. **Static guard** — `prompt_directives.unverified_markers(repo_root)`; asserted
   `== []` by `tests/unit/test_prompts_only_verified.py::test_real_repo_has_no_unverified_markers`.

**Gap found and fixed here:** runs resolve recipes **overlay-first**
(`<MINI_ORK_HOME>/recipes/<id>/`, `mini_ork/recipes_catalog.py`) and inject those
prompt files, but the guard scanned `<repo>/recipes` only. A hand-edited overlay
prompt could therefore carry an `applied:` block the guard never saw — the
read-root/scan-root gap. Fix: `scan()` / `unverified_markers()` /
`revert_unverified()` accept `extra_recipe_dirs`; `home_recipe_dirs()` supplies
the overlay; `apply --revert-unverified --include-home` cleans it. The live repo
+ overlay is currently clean (0 blocks, 0 offenders).

## B4 — shared-context blocks, off by default (detail)

`MO_PLANNER_SHARED_CONTEXT` (default off) gates three blocks: the role pack, the
ContextNest recent-sessions feed, and the global active-state index. When off the
producers are not called at all (no ContextNest HTTP, no DB scan) and the planner
injection ledger records each as `skipped: shared_context_off`.

Left off **by design**: the SDD mechanisms plan
(`docs/plans/2026-10-07-sdd-mechanisms-for-mini-ork.md`) measures generic shared
context as a **null effect** on correctness — "fine for efficiency only." It is
retained as an opt-in, not a correctness lever. No change required.

## B3 — producer gap CLOSED (themes author + verify)

The consumer (`verified_themes()`) was correct but rendered nothing because no
row ever became `status='verified'`: every `lesson_themes` row was a candidate
with blank `lesson_text`. The producer was missing, not broken.

**Fix (`fc92bbbc`):** `mini_ork/learning/themes.py::promote` —
- selects `kind='task' AND status='candidate' AND lesson_text='' AND n_runs>=MO_THEME_PROMOTE_MIN_RUNS` (default 3);
- resolves members from `gradient_theme ⋈ gradient_records.evidence` (min `MO_THEME_PROMOTE_MIN_MEMBERS`, default 3);
- authors the lesson via `pattern_induction.induce_cluster` (same path as `pattern_records`, gated by `MO_PATTERN_INDUCE`);
- writes `lesson_text` + `status='verified'`. Blank lessons only → idempotent.

Wiring: reflect calls `themes.promote(db_path)` after assign_new/rollup; the
`[themes]` stdout line reports `N lesson(s) promoted`. CLI: `themes promote
--limit N`. No manual backfill needed — the reflect hook self-populates.

**Live proof (2026-10-09):** `themes promote --limit 1` on the live DB authoring
via the `minimax` lane → `promoted: 1`; row `th-1e42cc51c68c` went
`status='verified'` with a real `lesson_text`; `context_v2.verified_themes()`
then returned `t:th-1e42cc51c68c` for `code_fix`, `bug_audit_cmgk`, and `*`.
Gotcha: a **standalone** `themes promote` needs `MO_ALLOW_FRAMEWORK_CWD=1`
(or `MO_TARGET_CWD`), else the provider cwd-guard returns rc=2 — a real run
supplies this, only the bare invocation does not.

## Reflect trace leak — fixed at source + backfilled

**Bug:** a reflect child SIGKILLed by the parent's `subprocess` timeout
(`MO_REFLECT_TIMEOUT_SECONDS`) never ran its terminal trace write, so its
`execution_traces` row stayed `status='running'` forever. `resolve_finished_runs`
keys on "run over = no trace running", so the leak starved the semantic feedback
loop.

**Fix (`b1b7ac98`):**
- `trace_store.finalize_reflect_traces(run_id="", *, grace_s=720)` — the terminal
  write the parent owes; keyed sweep (exact run) or age-guarded global sweep; cold-safe.
- `cli/main.py` — reflect block gained `finally:` → `finalize_reflect_traces(run_id)`;
  run flow gained a start-of-run age-guarded heal.
- `cli/reflect.py` — an `atexit` guard lands `failure` on the exception/crash path
  (cannot cover the SIGKILL path; that is the parent's `finally`).

**Backfill:** guarded SQL finalized 482 historical `__reflect__` running rows →
0 running (history: 483 failure / 242 success / 21 vacuous). Downstream,
`resolve_finished_runs` then resolved 1,559 pending retrievals (1617→58),
confirming the leak was starving the feedback loop.

**Live proof:** the fix-1 smoke run's reflect child timed out → its row
landed `failure` (0 running).
