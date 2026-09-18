# Safety and Bounded Autonomy

mini-ork can propose changes to itself. It cannot promote those changes without evidence. This document defines what the system may and may not do autonomously, and what gates enforce the boundaries.

## Posture: no human in the promotion loop

The promotion gate has **no human branch**. A candidate is promoted only on a real
held-out measurement that strictly beats the baseline; anything else is
`quarantined` or `rejected` and recorded with its reason. Nothing escalates to a
person, and no environment variable can request one.

This is deliberate: the gate exists to answer "did this change measure better than
what is running?", and that question is answered by running the probes, not by
asking someone. Removing the human is safe precisely because it does not remove the
measurement — there is no code path that promotes without one. The fabrication
guards in `mini_ork/cli/apply.py` (`FABRICATING_SCORERS`, `:94`) and the
no-measurement rules below are what hold that property up.

What this costs you, stated plainly: nobody reviews a promotion before it lands.
The backstop is rollback (`mini_ork/registries/version_registry.py`), not review.
If you want a human to see a change first, do not run the apply loop unattended —
see the warning in `README.md`.

---

## The 7-Rung Autonomy Ladder

Self-improvement is sequenced by risk. Lower rungs are always safe; higher rungs require progressively stronger gates.

| Rung | Mutation | Gate required |
|---|---|---|
| 1 | Tune prompt wording — rewrite a node's prompt without changing its role or output schema | None. Always safe to try; benchmark will show if it's worse. |
| 2 | Tune retrieval / context assembly — adjust relevance weights, token budget, namespace selection | None. Context changes don't alter code or workflow topology. |
| 3 | Tune workflow graph edges — add/remove a `depends_on` or `supplies_context_to` edge | Benchmark pass: utility_delta > 0 on the benchmark suite. |
| 4 | Tune agent role definitions — change a node's `model_lane`, `tools`, or `task_classes` binding | Benchmark pass: utility_delta > 0. |
| 5 | Tune verifier selection — add/remove a verifier from a node's `scripts` list or `gates` list | Benchmark pass: utility_delta > 0 AND no regression in gate_pass_rate. |
| 6 | Propose code changes to mini-ork itself — generate patches to `lib/`, `bin/`, or `schemas/` | Benchmark pass AND no safety constraint violated. |
| 7 | Promote runtime changes to active default — write a `stable` row for a rung-6 change | All rung-6 gates, plus a cleared quarantine if one was set. |

The system never skips rungs. A rung-6 proposal that does not pass its benchmark
stays as `candidate` — it does not auto-promote, and it is not escalated to a human
either. (The `human_gate` *node type* still exists for recipes that declare it, but
its evaluator defers unconditionally — `mini_ork/gates/gate_registry.py:_eval_human`
— so it never blocks a run. It is not part of the promotion path.)

**Read the ladder as intent, not as shipped mechanism.** What is actually wired
today is the apply/promote path over `version_registry`: a candidate is scored on a
held-out probe set and promoted only on a strict measured improvement, or
quarantined with a reason. The rung numbering describes how mutation risk *should*
be sequenced. There is no rung dispatch in the runtime that reads a rung number
and picks a gate — so do not read "rung 3 needs a benchmark pass" as a promise that
every edge change is benchmarked. What holds for every promotion is the single
rule in [Posture](#posture-no-human-in-the-promotion-loop): no real measurement, no
promote.

---

## What Must Not Be Mutated Silently

Regardless of rung or benchmark result, the following are immutable without explicit human action:

| Constraint | Why |
|---|---|
| Zero-fallback policies | The system must not add catch blocks that hide failures. Any proposed patch that adds fallback logic is rejected by the reviewer. |
| User-data boundaries | Agent context packs must not cross user-data scope boundaries. |
| Deployment / migration permissions | Changes that run `psql`, `kubectl`, `docker`, or equivalent in production require an explicit human step. |
| `audit_log` schema | Append-only, enforced by a sqlite trigger (`trg_al_no_update` / `trg_al_no_delete`). No migration may DROP or ALTER the table without an explicit manual step by a human operator. (The table has no runtime writer yet — see [Audit Log](#audit-log).) |

**On the "root of trust" that is not there.** Earlier revisions listed
`config/safety.yaml` as the immutable safety-constraints file that the loop could
never promote over. That file does not exist in this repository, and the
`safety_constraints` table (`db/migrations/0012_safety.sql`) has schema but no
writer. There is therefore **no declarative safety-constraints file acting as a
backstop** to the unattended apply loop. What stands between a bad candidate and
your recipe files is the measurement gate plus your VCS — which is why the
[README warning](../README.md#warning-this-system-modifies-itself-unattended) is
not decorative.

---

## PromotionGate Contract

Two gates, both measurement-only. (This section used to cite
`lib/promotion_gate.sh:promotion_gate_check()`. That script was removed in the
2026-07 bash removal; `mini_ork/gates/promotion_gate.py` is now the only
implementation.)

**Benchmark promotion gate** — `promotion_evaluate`
(`mini_ork/gates/promotion_gate.py:147`):

```
1. A measurement exists
   0 benchmark_results rows → rejected. Not quarantined: nothing was measured,
   so there is no verdict on the candidate to record.

2. All benchmark tasks pass
   MIN(pass) over benchmark_results for the candidate → must be 1

3. utility_delta > 0
   candidate utility_after > baseline utility_before (from version_registry)

else → quarantined (recorded in promotion_records with its reason)
```

**Apply-loop gate** — `evaluate_gate` (`mini_ork/cli/apply.py:333`), used by
`apply_run` when a gradient directive is applied to a live target. With per-task
held-out vectors in hand the bar is a *strict* improvement: a delta equal to the
threshold means the candidate is indistinguishable from the baseline, and equality
is not evidence. A previously-passing held-out task that starts failing blocks the
promote regardless of the aggregate. The scalar-only path (no vectors) keeps the
historical `delta >= threshold` rule.

Both gates refuse a promote from a scorer that fabricates its numbers
(`FABRICATING_SCORERS`, `mini_ork/cli/apply.py:94`). A promote on fabricated
utility is indistinguishable in the audit trail from a measured improvement, so it
is quarantined outright, with no opt-in flag that restores it.

If any condition fails, the decision is written to `promotion_records` (and, on the
apply-loop path, `apply_attempts`) with `decision = 'quarantined'` or `'rejected'`
and the reason attached. `pending_human_approval` is a legal stored value only so
that rows written before the human gate was removed keep their meaning; nothing
writes it.

**Utility formula (default):**

```
U = 0.45 * task_success_rate
  + 0.20 * verifier_pass_rate
  + 0.15 * artifact_quality_score
  - 0.10 * normalized_cost
  - 0.05 * normalized_latency
  - 0.05 * risk_penalty
```

Weights are overridable per call via the `weights` argument to
`mini_ork.learning.utility_function.score`, or globally via the `MINI_ORK_W_*`
environment defaults. The `.sh` per-task override path
(`${MINI_ORK_HOME}/config/utility_functions/<task_class>.sh`) that the module
docstring still names was **not** ported and has no reader since the 2026-07 bash
removal — do not expect editing a file there to change anything.

---

## Quarantine and Rollback

A quarantined version cannot be re-promoted automatically. On the apply-loop path
the quarantine is stronger than "not re-promoted": `_previously_failed`
(`mini_ork/cli/apply.py`) keys edit memory on
`(task_class, target_name, source_id)` over `decision IN ('quarantined','rejected')`,
so a quarantined directive is not re-proposed at all. That is the spend
containment — without it, a failing candidate would be retried on every sweep.

There is **no CLI for clearing a quarantine**. `mini-ork version-clear-quarantine`,
cited by older revisions of this document (and still advertised in
`mini_ork/cli/promote.py`'s usage text), is not a registered subcommand. Clearing
one is a Python-level operator action against the registry:

```python
from mini_ork.registries import version_registry

# Manual, out-of-band — not a gate the loop waits on.
# db= is the state.db path; omit it to use $MINI_ORK_DB.
version_registry.clear_quarantine("<version_id>", "human:<you>",
                                  db="/path/to/.mini-ork/state.db")
```

That sets `status='candidate'` and records the approver in
`quarantine_cleared_by`. Re-promotion still has to pass the measurement gate; the
clear only removes the block.

### Rollback

```bash
mini-ork rollback <workflow|agent> <name>
```

`<name>` is the registry row's `name`, and for an applied prompt mutation that name
is the **target file's absolute path** — not a recipe or a version id. Two
consequences worth knowing:

- `mini-ork rollback agent default` is a no-op that reports nothing to revert.
  No row is named `default`; the run's applied targets are resolved from the
  implementer's recorded `files_changed` instead
  (`version_registry.targets_for_paths`, called by
  `_handle_rollback` in `mini_ork/cli/execute_handlers.py`).
- Long-lived DBs accumulate rows pointing into whatever worktree promoted them. A
  rollback whose `payload.target_path` sits outside the active `MINI_ORK_ROOT` is
  **refused, loudly, and the file is not written** — only the DB state moves.
  Restoring a stale checkout's file would silently rewrite a different tree than
  the one being rolled back.

A rollback moves status columns **and rewrites the target file**. The status update
alone would leave the retired directive on disk, so the system that just "rolled
back" would keep executing the change it retired. The pre-mutation bytes come from
the baseline row: the first promotion of a name mints a predecessor row carrying
`baseline_content`, so `previous_stable_version` is non-NULL and there is always
somewhere to go back to. Rows written before that existed carry only a
`rollback_hash`, and a rollback over one moves DB state and logs that the file was
left as is.

A failed restore raises rather than being swallowed — this is the last line of
defense now that no human reviews a promotion.

---

## Audit Log

The `audit_log` table and its append-only triggers exist
(`db/migrations/0012_safety.sql`, plus `safety_events` from `0036_safety_events.sql`):

```sql
CREATE TABLE IF NOT EXISTS audit_log (
  audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type  TEXT NOT NULL,   -- 'promotion' | 'quarantine' | 'rollback' | 'safety_hit' | ...
  actor       TEXT NOT NULL,   -- agent_version_id | 'human:<user_id>' | 'gate:<gate_id>'
  target      TEXT NOT NULL,   -- e.g. 'workflow:code_review_v3'
  payload     TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- trg_al_no_update / trg_al_no_delete raise ABORT on any UPDATE or DELETE.
```

**The Python runtime does not currently write to it.** No module under `mini_ork/`
inserts into `audit_log`; older revisions of this document claimed every
promote/quarantine/rollback wrote a row, and that was never true of the Python
code. Do not treat an empty `audit_log` as evidence that nothing happened.

The machine-readable record of what the loop decided lives in three tables that
*are* written:

| Table | Written by | Carries |
|---|---|---|
| `version_registry` | `mini_ork/registries/version_registry.py` | the promoted row, its baseline predecessor, and both texts so a rollback can restore bytes |
| `apply_attempts` | `mini_ork/cli/apply.py` | apply-loop `decision`, `rationale`, `utility_before` / `utility_after` |
| `promotion_records` | `mini_ork/gates/promotion_gate.py`, `mini_ork/cli/apply.py` | benchmark-gate verdict + reason |

Query the apply-loop trail — this is the one that answers "why did this promote?":

```bash
sqlite3 "${MINI_ORK_DB}" \
  "SELECT decision, utility_before, utility_after, substr(rationale,1,80)
   FROM apply_attempts ORDER BY rowid DESC LIMIT 20"
```

---

## Non-Goals

These behaviors are explicitly out of scope. Any proposed change (rung 6) that introduces them is rejected by the safety reviewer:

- **No hidden autonomous production mutation** — the system does not deploy, merge, or push to production without a gate. Promotion requires a real benchmark measurement; it does not require a person. See [Posture](#posture-no-human-in-the-promotion-loop).
- **No fallback chains that hide failures** — `catch { return defaultValue }` patterns that replace a failed result with fabricated data are rejected. Fail explicitly; preserve sandboxes for inspection.
- **No promotion without measurable utility** — a version that scores the same or worse than the current version is not promoted, even if all benchmarks pass. The `utility_delta > 0` check is strict.
- **No memory writes without provenance** — every write to a memory namespace includes `run_id`, `task_id`, `agent_version_id`, and `ts`. Orphan records (missing provenance) are flagged by the nightly compaction job.
- **No silent model substitution** — if a model call fails (rate limit, network error, provider outage), the node fails loudly. There is no "try the cheaper model" fallback chain. The failure is recorded; the human or the orchestrator decides what to do next.
