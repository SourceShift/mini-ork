# Framework Edit: harden PRM against Goodhart and rescue σ=0 groups

## Goal

Two bounded refinements from the critique's wave 4 that operate on **columns
that already exist** on `execution_traces` / `agent_performance_memory`:

- **#5** — make the PRM harder to game: the heuristic currently grants ~45% of
  its score for *activity* (called a tool, touched a file, spent money) rather
  than *correctness*. Re-weight toward outcome-grounded terms.
- **#2** — rescue homogeneous groups: when every lane in a group scores
  identically (`std == 0`) the advantage is forced to exactly 0, so identical-
  looking lanes never separate even when one is cheaper/faster. Add a
  tie-breaking fallback.

**Deferred — #7 (per-job routing) is NOT in this epic.** Per-job routing needs a
per-job difficulty signal, and `execution_traces` has no difficulty / word-count
/ complexity column (verified: only `prompt_version_hash` / `context_bundle_hash`
exist, which are identity hashes, not difficulty). Adding that signal is a schema
change, out of scope for a bounded reward-shaping framework-edit. Track #7
separately as a schema-first follow-up. **Do not attempt #7 here; do not go
searching for a difficulty column — there isn't one.**

## Root cause (verified against source)

**#5 — PRM rewards motion, not outcome.** `lib/process_reward.sh:49-63`:

```
50   if (r["status"] or "") == "success":            score += 0.40
52   if _len_json(r["tool_calls"]) > 0:              score += 0.20   # activity
54   if files_written or files_read:                 score += 0.10   # activity
57   if verdict in {approve,...}:                    score += 0.15   # judge
60   if 1000 <= duration_ms <= 600000:               score += 0.10   # activity
62   if cost_usd > 0:                                score += 0.05   # activity
```

`0.20 + 0.10 + 0.10 + 0.05 = 0.45` of the maximum reward is earned by *doing
something* — calling a tool, writing a file, taking a plausible amount of time,
spending a cent. An agent that loops, touches files, and burns tokens scores
well even if the output is wrong. Only `status==success` (0.40) and the judge
term (0.15, see fe-reward-verifiable-first) are outcome-ish.

**#2 — σ=0 forces zero advantage.** `bin/mini-ork-execute:355-366`:

```
355   variance = sum((score - mean) ** 2 for score in scores) / len(scores)
356   std = math.sqrt(variance)
366   bucket["adv"].append(0.0 if std == 0 else (score - mean) / std)
```

When all lanes in a group score identically, `std == 0` → every lane gets
advantage exactly 0 → the router can never prefer the cheaper or faster of two
equally-correct lanes.

**#7 — routing is category-coarse.** The GRPO grouping key
`bin/mini-ork-execute:349` is `(node_type(row), row["task_class"])`, and the
router selects on the same `(task_class, node_type)` (`:197-219`). Every job in
a `task_class` shares one verdict about which lane is best — there is no
per-prompt / per-difficulty feature, so an easy chapter and a hard chapter route
identically.

## Scope Hint

- `lib/process_reward.sh`   (term weights — header table + the duplicated
  scoring blocks in BOTH `prm_score_trace` and `prm_backfill`. Line numbers
  shifted after wave 3 added the same-family guard; find them by the
  `score += 0.40 / 0.20 / 0.10 / 0.05` literals.)
- `bin/mini-ork-execute`    (σ=0 branch — the line
  `bucket["adv"].append(0.0 if std == 0 else (score - mean) / std)`, plus the
  per-agent loop that has `avg_cost_usd` / `avg_duration_ms` available.)

Both files were already touched by waves 1-3; build on the current code, do not
revert any of it.

## Expected Edit

Implement the two refinements (each independently env-gated so partial landing
is safe):

1. **Re-weight PRM toward outcome (#5).** Cap the activity terms (tool_calls,
   files, duration, cost) at a small combined total (recommend ≤ 0.20) and put
   the freed weight on the deterministic `status` term, so a wrong-but-busy
   trace cannot out-score a correct-but-quiet one. The full score must still max
   at ≤ 1.0 and floor at 0.0. Apply the SAME new weights to both the
   `prm_score_trace` and `prm_backfill` copies (they are duplicated) and add a
   guard comment noting the two must stay identical. Note wave 3 already gated
   the `+0.15` verdict term behind `status==success AND not same-family`; keep
   that gating intact.

2. **σ=0 tie-breaker (#2).** When `std == 0`, instead of writing exactly 0,
   break the tie on a cheap secondary signal already aggregated per agent —
   recommend a small advantage proportional to `-avg_cost_usd` (or
   `-avg_duration_ms`) normalized within the group, bounded to a small range
   (e.g. `±0.1`) so it never dominates a real correctness signal. Equal
   correctness ⇒ prefer the cheaper/faster lane. This must compose with the
   existing shrinkage (wave 1) and decay (wave 2) — apply the tie-break to the
   raw per-trace advantage before shrinkage/decay run.

## Requirements

- Do not change the router's AND-gate eligibility (`sample_size`/`runs_count`
  thresholds), the shrinkage (wave 1), the decay/recency (wave 2), the
  verifiable-first reward ladder (wave 3), or any schema/migration.
- Do not touch `.mini-ork/config/**` or any provider wrapper.
- Both changes must be individually disable-able via env, defaulting to the new
  behavior, with a value that reproduces today's behavior
  (`MO_PRM_ACTIVITY_CAP`, `MO_LEARNING_TIEBREAK=0` ⇒ legacy σ=0→0).
- Keep `prm_score_trace` and `prm_backfill` weight tables byte-identical.
- Pure bash + python3 stdlib; no new dependencies. **Bound your work:** ≤ ~50
  lines across two files. Do not add a difficulty/job feature (that is the
  deferred #7) and do not search for one.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 2, "tests_pass": true, "static_pass": true, "pass": true }`.
- **Goodhart proof (#5):** score two traces — A = `status=success`, no files, no
  tools, no cost (correct-but-quiet); B = `status=failed` but tool_calls=5,
  files written, duration in-window, cost>0 (wrong-but-busy). Assert
  `score(A) > score(B)`, and assert `prm_score_trace` and `prm_backfill` produce
  identical scores for the same row. Write to
  `${MINI_ORK_RUN_DIR}/goodhart-proof.txt`.
- **Tie-break proof (#2):** seed a group of two lanes with identical PRM scores
  (so `std == 0`) but different `avg_cost_usd`. Run
  `mo_learning_write_grpo_advantages` and assert the cheaper lane has a strictly
  higher (still small, within the tie-break bound) `relative_advantage` than the
  dearer one — instead of both being exactly 0. With `MO_LEARNING_TIEBREAK=0`
  assert both are exactly 0 (legacy). Write to
  `${MINI_ORK_RUN_DIR}/tiebreak-proof.txt`.
- `bash scripts/learning-loop-closure-gate.sh` still exits 0.

## Why this kickoff exists

These are the "refinement" tier — none blocks the loop, but each removes a way
the signal lies. The PRM re-weight closes the easiest reward hack (be busy, not
right). The σ=0 tie-breaker lets the loop pick the cheaper of two equals instead
of shrugging. (#7 per-job routing is deferred to a schema-first follow-up — it
needs a difficulty column that doesn't exist yet.) Sources: arXiv:2604.13602,
2506.19248, 2510.10931 (Goodhart / reward-model hacking), 2509.18851 (NGRPO /
homogeneous groups).
