# Framework Edit: fix learning-loop write half so GRPO can crown a winner

## Goal

Make the learning loop able to produce a **non-zero GRPO advantage** so the
`learning_governed` router can actually flip a node onto the winning lane. Today
every lane gets `relative_advantage = 0.0000`, so the router never moves — the
loop is physically closed but functionally inert.

## Root cause (verified against the live state.db)

Three coupled write-half defects, all proven by inspecting real traces from
`run-1782199619-78956` (task_class=research_synthesis):

1. **`execution_traces.verifier_output` is double-JSON-encoded.**
   - `bin/mini-ork-execute:1077` sets
     `obj['verifier_output'] = json.dumps({'node_type': node_type, 'finish_reason': ...})`
     — already a JSON *string*.
   - `lib/trace_store.sh:93` then does `json.dumps(p.get("verifier_output", {}))`
     — a **second** encode. The column ends up holding a quoted string:
     `"{\"node_type\": \"researcher\", ...}"` instead of the object
     `{"node_type": "researcher", ...}`.

2. **GRPO can't read `node_type`, so it groups every lane into a singleton.**
   - `bin/mini-ork-execute:283` does `payload = json.loads(row["verifier_output"] or "{}")`.
     Because the value is double-encoded, `json.loads` returns a `str`, not a
     `dict`. The guard `payload.get("node_type") if isinstance(payload, dict)`
     is False, so `node_type(row)` falls back to **`row["agent_version_id"]`**
     (the lane name itself).
   - Result: each lane forms its own `(lane, task_class)` group of size 1.
     With one item, `mean = score`, `std = 0`, and the code does
     `0.0 if std == 0` → **`relative_advantage = 0.0` for every lane**.
   - Proof: live `agent_performance_memory` shows
     `kimi_lens adv=+0.0000 runs=1` and `opus_lens adv=+0.0000 runs=1`, even
     though their PRM scores differ sharply (kimi 0.05 vs opus 0.55). If those
     two shared one `(researcher, research_synthesis)` group, opus would score
     `adv ≈ +1.0` (the winner) and kimi `≈ -1.0`.

3. **A non-researcher node reused a researcher lane id, colliding in GRPO.**
   - The `opus_lens` lane trace carries `node_type=reviewer` (the reviewer node
     ran on the opus lane), while the opus *researcher* lens trace is missing
     entirely. So GRPO's grouping key for that lane is polluted by a different
     role.

## Scope Hint

- `bin/mini-ork-execute`  (trace payload builder ~:1077; GRPO `node_type()`/grouping ~:283-300)
- `lib/trace_store.sh`    (verifier_output serialization ~:93)

## Expected Edit

Touch exactly these two files:

1. **Stop the double-encode.** Make the trace payload + `trace_write` agree on a
   single encoding so `execution_traces.verifier_output` holds a real JSON
   object. Prefer fixing it at the source: have `bin/mini-ork-execute:1077`
   pass a dict (`obj['verifier_output'] = {...}`) so `lib/trace_store.sh:93`'s
   single `json.dumps` produces the object. Whatever you choose, the column
   must end up as `{"node_type": "...", ...}` (no outer quotes / escapes).

2. **Harden GRPO grouping** in `mo_learning_write_grpo_advantages`
   (`bin/mini-ork-execute:283`): decode `verifier_output` robustly (tolerate a
   legacy double-encoded string by decoding twice if the first decode yields a
   `str`), and when `node_type` is still unavailable, fall back to a **stable
   role key** (e.g. the recipe role) — **never** to `agent_version_id`, which
   guarantees singleton groups. The grouping key must let the 4 researcher
   lenses (glm/kimi/codex/opus) land in one `(researcher, research_synthesis)`
   group so they compete.

## Requirements

- Do not change the PRM heuristic, the router gate, or any schema/migration.
- Do not touch `.mini-ork/config/**` or any provider wrapper.
- Backward compatible: GRPO must still read **existing** double-encoded rows
  (decode-twice fallback) so historical traces don't break the writer.
- No new dependencies; keep it pure bash + python3 stdlib.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed two-file patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 2, "tests_pass": true, "static_pass": true, "pass": true }`.
- A proof harness in the isolated worktree demonstrates the fix on a seeded DB:
  insert two researcher-lens traces under the same `(researcher,
  research_synthesis)` group with PRM 0.10 and 0.90, run
  `mo_learning_write_grpo_advantages`, and assert the two
  `agent_performance_memory` rows have **opposite-signed, non-zero**
  `relative_advantage` (one `> 0`, one `< 0`). Write the assertion result to
  `${MINI_ORK_RUN_DIR}/grpo-advantage-proof.txt`.
- `bash scripts/learning-loop-closure-gate.sh` still exits 0 (no regression to
  the closed-loop invariants).

## Why this kickoff exists

This is the load-bearing fix for the live learning-loop validation
(`scripts/learning-loop-live-validate.sh`). Until GRPO produces a non-zero
advantage, no number of real runs can make the router flip — the validation
verdict is permanently COLD. Fixing the write half is the prerequisite for
demonstrating learning by running it.
