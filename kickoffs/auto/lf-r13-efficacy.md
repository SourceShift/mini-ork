# LF-R13 make the efficacy measurement meaningful before any retirement

## Goal

`mini-ork metrics context` reports a lift that cannot be trusted yet, for four
reasons the panel measured (F15, X5, X6; R13 in
`docs/plans/2026-10-15-learning-loop-fixes.md`; Q3 and Q5 decided):

1. **Mixed arms.** Every v2 run before R3 received BOTH the v1 and the v2
   blocks, so its recurrence rate is not the v2 effect. R3 stamps
   `pack["themes_once"] = true`; R13 reads that marker and reports unmarked v2
   runs as their own arm `v2_legacy`, excluded from lift (Q5).
2. **Run-level denominators only.** `summarize` counts only harvested runs
   and only repeats per selected group. A run that was assigned to an arm but
   never harvested silently leaves the denominator, which flatters an arm whose
   runs fail early. Report intent-to-treat AND delivered-only side by side.
3. **Pseudo-replication.** Four retries of one kickoff are not four independent
   observations. Recurrence must also be keyed by `context_v2.kickoff_stem`
   (R10's identity), with generic stems bucketing by run id.
4. **The matcher is strict.** `recurrence` uses TF-IDF cosine ≥ 0.35, so a
   reworded restatement (cosine ≈ 0.25) is not counted. Add token-set
   containment, and embedding cosine when `MO_EMBED_PROVIDER` names a real
   embedder.

Plus the report-only half of R11 item 3 and Q3: `memory/retirement.candidates`
extends to **themes** and **learned rules**. It reports; it never retires
anything on its own.

The planner-injection ledger itself already exists (`context_v2.
write_injection_record`, written by `cli/plan.py`); this kickoff adds the
missing provenance (`held_out`) to it, it does not replace it.

Decision record: `docs/plans/2026-10-15-learning-loop-fixes.md` (R13; Q3
report-only; Q5 `v2_legacy`).

All paths are relative to the repository root.

## Mechanism (exact)

1. **Arm split (Q5).** In `metrics_context.run_record`, after computing
   `arm`, apply `if arm == "v2" and not pack.get("themes_once"): arm =
   "v2_legacy"`. `summarize` renders it as its own row in `arms`; `lift` reads
   only `v2` and `holdout`, and its dict gains `n_v2_legacy` (count only).
2. **Ledger provenance.**
   - `context_v2.write_injection_record`: the record gains `"arm"` and
     `"held_out"`, derived from `extra["context_v2"]["arm"]` when that key is a
     string (`None` when absent). Keep the existing never-raises wrapper.
   - `mini_ork/cli/execute_handlers._write_learned_record` gains a keyword-only
     `run_id: str | None = None`; the record gains `"arm"` and `"held_out"`
     computed lazily via `from mini_ork import context_v2;
     context_v2.arm(run_id)` (`None` when `run_id` is falsy). The one caller
     (`_dispatch_llm_node`) passes `run_id=run_id`.
   - Result: a holdout run's every node ledger row carries `held_out: true`;
     a v2 run's carries `false`. No prompt or routing behaviour changes.
3. **Paraphrase-tolerant recurrence.** In `context_v2`, beside
   `recurrence`:
   - `CONTAINMENT_MIN = 0.6` and `EMBED_MIN = 0.6` module constants;
   - `_token_set(text)` = `set(themes._tokenize(clean_issue(text)))`;
   - `_containment(a, b)` = `len(a & b) / min(len(a), len(b))`, `0.0` when
     either set is empty;
   - `_embedding_cosine(a, b)` returns `None` unless
     `os.environ.get("MO_EMBED_PROVIDER", "").strip()` is set, then
     `mini_ork.memory.semantic.get_embedder().embed([a, b])` and the dot
     product (vectors are unit-normalized); any exception → `None`.
   `recurrence` marks a cluster recurred when the TF-IDF cosine is `>= sim`
   **or** containment is `>= CONTAINMENT_MIN` **or** the embedding cosine
   (when available) is `>= EMBED_MIN`. `cluster()`, `DEFAULT_SIM` and every
   other caller of `themes._dot` are untouched.
4. **Denominators and stem keying.** `run_record` adds `"stem":
   context_v2.kickoff_stem(pack.get("kickoff_path") or "") or ""` and
   `"recurs": bool(recurred)` (the existing `recurred` list stays).
   `summarize` adds per arm, keeping every existing key:
   - `itt_runs` = runs with `n_groups > 0`; `itt_recurrence_rate` =
     (runs with `recurs`) / `itt_runs` — an un-harvested run counts as no
     event, which is the intent-to-treat convention;
   - `delivered_recurrence_rate` = today's `recurrence_rate` (repeats /
     groups over harvested runs) and `delivered_runs` = today's
     `harvested_runs`;
   - `stem_buckets` = distinct `stem or run_id` values among runs with
     `n_groups > 0`; `stem_recurrence_rate` = buckets with any `recurs` /
     `stem_buckets`.
   `lift` keeps `value`, `status`, `n_v2`, `n_holdout` (delivered-only, as
   today) and gains `value_itt`, `status_itt`, `n_v2_itt`, `n_holdout_itt`
   computed the same way on `itt_recurrence_rate` / `itt_runs`. Both need
   `MIN_N` per arm to be `"measured"`.
   `_print_human` prints, per arm, `itt_recurrence_rate`,
   `delivered_recurrence_rate` and `stem_recurrence_rate`, and both lifts
   (delivered and ITT). The old run-level `recurrence_rate` stays printed for
   one release (the panel's "print old and new" rule).
5. **Report-only retirement.**
   - `memory/retirement.theme_candidates(*, db_path=None, max_age_days=90,
     now=None)` — rows of `lesson_themes` with `status='verified'` whose
     `last_seen` is older than the window (in seconds; `max_age_days <= 0`
     disables the check and returns `[]`). Each result:
     `{"kind": "theme", "theme_id", "representative", "n_runs", "last_seen",
     "state": "verified", "recommendation": "retire", "evidence": "last seen
     <iso>"}`; sorted by `last_seen` ascending. A missing table or column →
     `[]`, never raises.
   - `memory/retirement.rule_candidates(*, db_path=None)` — rows of
     `user_preference_memory` with `origin='learned'` and `active=1` whose
     `source_ids` is a non-empty JSON list of `code_findings` ids and **none**
     of those findings is fresh under R11a's `code_findings.annotate_staleness`
     (fetch the rows once, annotate against each row's `target_repo` when the
     column exists, else the repo `MINI_ORK_ROOT`/cwd). Each result:
     `{"kind": "rule", "preference_key", "text" (= preference_value),
     "source_ids", "state": "active", "recommendation": "retire",
     "evidence": "<n> of <n> source findings stale"}`. An unparsable
     `source_ids`, an empty list, or a missing table → skip that row (or all
     rows), never raises.
   - Neither function writes: no call to `retire()`/`reactivate()`, no UPDATE.
     `retire()` / `reactivate()` / `retirement_state()` semantics unchanged.
   - `mini_ork/cli/memory_lifecycle.py --candidates` gains
     `--kind semantic|theme|rule|all` (default `semantic`, current behaviour);
     `theme`/`rule` run the new functions and print the same table/JSON shape
     (id column is `theme_id` / `preference_key`). Still always exits 0.

## Files in scope

- `mini_ork/context_v2.py`
- `mini_ork/cli/execute_handlers.py`
- `mini_ork/cli/metrics_context.py`
- `mini_ork/cli/memory_lifecycle.py`
- `mini_ork/memory/retirement.py`
- `tests/unit/test_metrics_context.py` (new)
- `tests/unit/test_memory_retirement_py.py`

## Tests (`tests/unit/test_metrics_context.py`, hermetic temp run dirs + temp DB; extend `tests/unit/test_memory_retirement_py.py`)

- a v2 pack without `themes_once` reports arm `v2_legacy`; with it, `v2`;
  `lift` never uses `v2_legacy` and its dict names `n_v2_legacy`;
- `run_record` returns `stem` from `kickoff_path`, and `""` for
  `kickoff.md`; two runs of the same stem with one recurrence give
  `stem_recurrence_rate` counting ONE bucket, while the run-level rate counts
  two;
- an arm of 3 runs (2 harvested, 1 not) reports `itt_runs=3`,
  `delivered_runs=2`, and an `itt_recurrence_rate` whose denominator is 3;
- holdout ledger rows carry `held_out: true` and `arm: "holdout"` — through
  both `_write_learned_record(run_id=...)` and
  `write_injection_record`'s `extra["context_v2"]` derivation;
- a reworded pair whose TF-IDF cosine is below `DEFAULT_SIM` but whose token
  containment is ≥ 0.6 counts as recurrence; an identical text still counts;
  two unrelated texts do not;
- `MO_EMBED_PROVIDER` unset leaves the embedding path off (no provider
  lookup); a registered fake provider returning identical vectors makes an
  otherwise-unrelated pair recur;
- `theme_candidates` returns a verified theme older than the window and skips
  a recent one and every `candidate` theme; `max_age_days=0` returns `[]`;
- `rule_candidates` reports a learned rule whose two source findings are stale
  and skips one with a fresh source or empty `source_ids`;
- `memory_lifecycle --candidates --kind theme` exits 0 and prints the theme
  row; `--kind semantic` output is unchanged.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_metrics_context.py tests/unit/test_memory_retirement_py.py tests/unit/test_context_v2.py tests/unit/test_context_v2_wiring.py tests/unit/test_ide_pages_learn_code.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_metrics_context.py tests/unit/test_memory_retirement_py.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r13-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r13-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. R11a and R3 have merged: depend on
`code_findings.annotate_staleness` and `pack["themes_once"]`, do not reimplement
either. Do NOT change theme or pattern selection, `scope_findings`,
`cluster()`, `DEFAULT_SIM`, `retire()` / `reactivate()` semantics, any
dispatch decision, or any gate; do not add a migration; do not write to the
live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/` (tests use temp
DBs).
