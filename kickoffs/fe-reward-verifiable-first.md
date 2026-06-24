# Framework Edit: make reward verifiable-first and decontaminate the judge signal

## Goal

Stop the learning signal from trusting an LLM judge's verdict as ground truth.
Today the GRPO `reward()` function and the PRM both read the *same*
`reviewer_verdict`, so a self-preferring or sycophantic judge can inflate a
lane's reward twice over. Reorder the reward so that **deterministic, verifiable
checks win first**, and the LLM verdict is only a tiebreaker — and counted once.

This is critique wave 3 (#6 judge self-preference corrupts reward).

## Root cause (verified against source)

**Reward falls back to the LLM verdict as truth.** `reward()` in
`bin/mini-ork-execute`:

```
337   def reward(row):
338       if row["process_reward"] is not None:
339           return max(0.0, min(1.0, float(row["process_reward"])))
340       verdict = (row["reviewer_verdict"] or "").lower()
341       if verdict in {"approve", "approved", "pass", "success", "ok"}:
342           return 1.0
343       if verdict in {"reject", "rejected", "fail", ...}:
344           return 0.0
345       return 1.0 if row["status"] == "success" else 0.0
```

When `process_reward` is null, the reward *is* the reviewer's verdict — a single
LLM's opinion becomes the training target for GRPO.

**The same verdict is double-counted through PRM.** Even when `process_reward`
is present, it already baked the verdict in — `lib/process_reward.sh`:

```
56   v = (r["reviewer_verdict"] or "").lower()
57   if v in {"approve", "approved", "pass", "success", "ok"}:
58       score += 0.15
```

So an approving judge contributes `+0.15` to the PRM, and that PRM is then the
reward. There is no independent verifier output (test pass/fail, schema
validation, citation check) consulted before the judge. A judge that prefers its
own family's prose, or rubber-stamps confident output, directly moves the lane's
advantage with nothing to check it against.

## Scope Hint

- `bin/mini-ork-execute`     (`reward()` ~:337-345)
- `lib/process_reward.sh`    (verdict term ~:56-58; whole heuristic ~:49-63)
- verifier artifacts already written per run under `${MINI_ORK_RUN_DIR}` (e.g.
  `verdict.json`, `lens_outputs_complete.sh` output) — read these as the
  verifiable signal.

## Expected Edit

Touch `bin/mini-ork-execute` and `lib/process_reward.sh`:

1. **Verifiable-first reward ladder.** Restructure `reward()` to consult signals
   in priority order:
   1. If a **deterministic verifier result** is available for the trace (a
      recorded pass/fail from a `verifier_ref` script / `verdict.json`
      `pass` field), use it as the dominant term (pass→1.0, fail→0.0).
   2. Else use `process_reward` if present.
   3. Else fall back to `reviewer_verdict` (today's behavior) — but tag it as
      *unverified* so it can be down-weighted.
   The LLM verdict must never override a deterministic verifier that disagrees.

2. **Down-weight / decouple the judge term in PRM.** In `process_reward.sh`,
   either (a) reduce the `reviewer_verdict` term so the judge cannot be the
   majority of the score, or (b) gate it behind verifier agreement (only grant
   the `+0.15` when a deterministic check also passed). Document the chosen
   policy in the header comment. The goal: the judge's opinion is at most a
   minor, non-double-counted contributor.

3. **(Recommended) Judge-identity guard.** When the reviewer model and the
   reviewed lane are the same provider family, neutralize the verdict term to
   avoid self-preference. The lane→family mapping can be read from the trace's
   `agent_version_id` / `model`.

## Requirements

- Do not change the router gate, the GRPO grouping, decay, or any
  schema/migration.
- Do not touch `.mini-ork/config/**` or any provider wrapper.
- If no deterministic verifier signal exists for a trace, behavior must degrade
  gracefully to today's path (no crash, no null reward).
- Pure bash + python3 stdlib; no new dependencies.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the two-file patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 2, "tests_pass": true, "static_pass": true, "pass": true }`.
- **Verifier-overrides-judge proof:** seed a trace with `reviewer_verdict=
  approve` but a deterministic verifier result of **fail**. Assert `reward()`
  returns 0.0 (verifier wins), not 1.0. Seed the mirror case (verdict reject,
  verifier pass) and assert 1.0. Write to
  `${MINI_ORK_RUN_DIR}/verifiable-first-proof.txt`.
- **No-double-count proof:** for a trace with an approving verdict and *no*
  deterministic verifier, assert the judge moves the final reward by at most the
  documented single small weight (not once through PRM and again through the
  fallback). Write the decomposition to
  `${MINI_ORK_RUN_DIR}/judge-weight-proof.txt`.
- **Self-preference guard proof (if implemented):** seed reviewer family ==
  lane family and assert the verdict term is neutralized.
- `bash scripts/learning-loop-closure-gate.sh` still exits 0.

## Why this kickoff exists

Reward hacking starts at the reward. mini-ork already produces deterministic
verifier artifacts every run, yet the learning signal reaches past them to ask
an LLM "was this good?" — and then counts that answer twice. A judge that likes
its own output can lift a lane's advantage with zero correctness behind it.
Putting verifiable checks first makes the reward hard to flatter and easy to
trust. Sources: arXiv:2604.22891, 2410.21819 (LLM-judge self-preference),
2504.03846, 2508.06709 (verifiable-reward / decontamination).
