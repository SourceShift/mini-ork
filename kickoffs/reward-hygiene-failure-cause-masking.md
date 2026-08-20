# Reward hygiene: mask non-learnable exits from group-relative advantage (Slice 1)

## Objective

mini-ork's compounding-autonomy curve measures **FLAT** (memory:
`project_compounding_curve_phase1`). One documented, untested cause is
**reward-bin pollution**: when a run exits for an *infra* reason the agent
never controlled — cost-circuit trip, watchdog timeout, a fused-open lane
(429 "Fair Usage", shadow-yaml unknown-lane) — the writeback path still files
a low reward (`0.15`) into the same `(node_type, task_class)` group as genuine
capability failures. Group-relative advantage (`writeback.py`) subtracts the
**group mean**, so those non-learnable zeros drag the mean down and hand
undeserved positive advantage to whichever lane happened to have healthy
groupmates. The learning signal is measuring lane *luck*, not lane *skill*.

The `execution_traces.validity` column already exists to express exactly this
distinction — but it is **dead end-to-end**: the producer (`_tf` in
`cli/execute.py`) hard-defaults it to `"valid"`, and the consumer
(`learning/writeback.py`) never SELECTs it. This slice wires that one column
through, no schema migration required.

**This is a behavior change with a KPI hypothesis, not a refactor.** The win
condition is: recomputing advantages with infra exits masked changes the
compounding metric. Masking only changes *which rows enter the group
aggregate* — so it is falsifiable on the existing trace corpus before any new
dispatch runs.

## What to change

### 1. Producer — stamp `validity` from `finish_reason` (`cli/execute.py`)

In the trace-fn factory `_make_trace_fn` (the `_tf` closure, ~line 1687), the
`finish_reason` is already a parameter and is already stamped into
`verifier_output` (~line 1696) and `extra["finish_reason"]` (~line 1738).
Derive `validity` from it and add it to `extra`:

- If `status` is a failure AND `finish_reason` is in the **non-learnable set**
  `{"timeout", "cost_limit"}` OR the finish-reason text contains
  `"lane_fuse_open"`, set `extra["validity"] = "infra_failed"`.
- Otherwise do **not** set `validity` (let `trace_write` keep its `"valid"`
  default — genuine capability failures like test-red / apply-fail / reviewer
  reject stay learnable).

The non-learnable set is the ONE tunable in this slice. Seed it with the three
exits above (they map 1:1 to `finish_reason_for_failure`'s `timeout` /
`cost_limit` / `lane_fuse_open` branches, `cli/execute.py:82`). Keep the set
as a small module-level frozenset/constant so it is greppable and editable in
one place. Do NOT try to also catch empty-text-but-rc=0 lanes here — that is a
detection problem, explicitly deferred to Slice 2 below.

### 2. Consumer — read `validity`, exclude non-valid rows (`learning/writeback.py`)

- Add `validity` to the `rows` SELECT (~line 199-203). It must tolerate a NULL
  / missing value as `"valid"` (older rows predate the stamp), e.g. select
  `COALESCE(validity, 'valid')` or normalise in Python.
- When building `weight_rows` (line 270) / `groups` (line 271-273), **exclude**
  any row whose normalised validity is not `"valid"`. Excluded rows must not
  enter the mean/variance (line 287-292), the cost tie-break (line 299), or the
  per-agent buckets (line 307-309). They are dropped from learning entirely —
  not zero-weighted, dropped (a zero-weight row still perturbs the cost
  tie-break set).
- Guard the empty-group case: if masking empties a `(node_type, task_class)`
  group, skip it (no advantage row written) rather than dividing by zero.

## Scope — files codex may edit

- `mini_ork/cli/execute.py` — add `validity` derivation in the `_tf` closure;
  add the non-learnable-finish-reason constant. No other logic touched.
- `mini_ork/learning/writeback.py` — add `validity` to the SELECT; exclude
  non-`valid` rows from `weight_rows`/`groups`; guard empty groups.
- `tests/unit/test_writeback_validity_masking_py.py` — NEW; the test below.

Do NOT edit any other file. Do NOT touch `trace_store.py` (the column and its
write path already exist). Do NOT touch `eval_judge.py` (that is Slice 2).

## Hard invariants (verifier will check these)

1. **Backward-compatible read.** A row with `validity` NULL, absent, or empty
   string is treated as `"valid"` and still learned from. The historical corpus
   has no explicit `validity` values — masking must not silently drop all of it.

2. **Only infra exits are masked.** A `status="failed"` row with a
   capability finish_reason (anything NOT in the non-learnable set — test-red,
   apply-fail, `error` from a real reviewer reject) keeps `validity="valid"`
   and stays in the group. Masking must never swallow a genuine wrong-fix; that
   would hide regressions, the opposite of the goal.

3. **Masking is drop, not down-weight.** Excluded rows are absent from mean,
   variance, cost-span tie-break, and per-agent buckets. Verify a masked row
   does not appear in ANY aggregate.

4. **No new column, no migration.** `validity` is read/written on the existing
   schema. `python3.11 -c 'import mini_ork.cli.execute, mini_ork.learning.writeback'`
   imports clean.

5. **Empty-group safety.** If every row in a group is masked, that group
   produces no advantage row and raises no exception (no div-by-zero at
   writeback.py:287-292).

## Verification (must all pass)

- NEW `tests/unit/test_writeback_validity_masking_py.py` proves, against an
  in-memory / temp `execution_traces`:
  - a group of two `valid` rows + one `validity="infra_failed"` row computes the
    same advantages as the two `valid` rows alone (the infra row is invisible);
  - a `validity=NULL` legacy row is still counted (backward-compat);
  - a group whose only row is `infra_failed` writes no advantage row and does
    not raise.
- NEW producer assertion: a failed `_tf` call with `finish_reason="cost_limit"`
  (or `"timeout"`) persists a row with `validity="infra_failed"`; a failed
  `_tf` call with a capability finish_reason persists `validity="valid"`.
- `python3.11 -m pytest -q tests/unit/test_writeback_validity_masking_py.py` — green.
- Full suite `python3.11 -m pytest -q` — no new failures vs baseline
  (`main` clean baseline, re-run yourself — do not trust an implementer's
  "pre-existing" attribution).
- `ruff check mini_ork/cli/execute.py mini_ork/learning/writeback.py` (F+E9) — clean.

## KPI falsification (run after green, report numbers — not gated by CI)

The point of the slice is the hypothesis, so measure it. On the existing
trace DB, compute the group-relative advantages twice — once with the new
masking, once forcing every row `valid` (masking disabled) — and diff the
`agent_performance_memory.relative_advantage` values per `(agent_version_id,
task_class)`. Report: how many lanes flip sign, and the magnitude of the
largest advantage swing. A non-trivial diff is the evidence that infra-zero
pollution was distorting the FLAT compounding curve; a null diff means this
corpus had no infra exits and the wound is elsewhere.

## Next slice — NOT in this scope (surfaces a design decision for the user)

**Slice 2 — layered execution-reward tier in `learning/eval_judge.py`.**
`execution_reward()` (`eval_judge.py:231`) currently returns a pass-fraction in
`[0,1]` collapsing to a binary at the extremes. The proposal is a **three-tier
reward** `{0.0, 0.5, 1.0}`:

- `1.0` — ran AND all verifiers (including any held-out / metamorphic check)
  passed;
- `0.5` — ran AND the primary gate passed BUT a held-out / metamorphic /
  amplified-input check failed (a *shallow* pass — matches
  `feedback_execution_anchored_is_not_ungameable`: a single test is a
  hackable extensional verifier);
- `0.0` — did not run, or the primary gate failed.

**The open design call is the exact predicate for the `0.5` tier** — what
counts as "the primary gate passed but a deeper check failed" in *this*
codebase's verifier vocabulary (which verifier types are "primary" vs
"held-out"?). That threshold is where domain knowledge shapes the reward, so
Slice 2 will scaffold `execution_reward` and leave the predicate for the user
to specify. Slice 2 also owns the harder detection problem this slice defers:
lanes that return **empty text at rc=0** (429 / shadow-yaml) and currently
mis-score as `status="success"` — a validity stamp on the *success* path, not
the failure path.

## Trajectory

Slice 1 of a two-slice "reward-signal hygiene" arc grounded in the 2026 arXiv
survey (2605.07276 failure-cause governance; EGCA/2510.00915 execution-grounded
reward). Slice 1 is the no-decision, immediately-falsifiable denominator fix
(mask non-learnable exits). Slice 2 is the numerator fix (layered tier) and
carries the one genuine design decision.
