# Safety and Bounded Autonomy

mini-ork can propose changes to itself. It cannot promote those changes without evidence. This document defines what the system may and may not do autonomously, and what gates enforce the boundaries.

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
| 6 | Propose code changes to mini-ork itself — generate patches to `lib/`, `bin/`, or `schemas/` | Benchmark pass AND `human_gate` review AND no safety constraint violated. |
| 7 | Promote runtime changes to active default — set `workflow_versions.status = 'promoted'` for a rung-6 change | All rung-6 gates + human writes APPROVE to inbox + `version_clear_quarantine` if previously quarantined. |

The system never skips rungs. A rung-6 proposal that passes benchmarks but has no human approval stays as `candidate` in `workflow_candidates` indefinitely — it does not auto-promote.

---

## What Must Not Be Mutated Silently

Regardless of rung or benchmark result, the following are immutable without explicit human action:

| Constraint | Why |
|---|---|
| `config/safety.yaml` | The safety constraints file itself is the root of trust. Mutation requires human gate and is logged to `audit_log`. |
| Zero-fallback policies | The system must not add catch blocks that hide failures. Any proposed patch that adds fallback logic is rejected by the rung-6 reviewer. |
| User-data boundaries | Agent context packs must not cross user-data scope boundaries. The `scope_gate` enforces this; mutations to `scope_gate` logic require rung-7. |
| Deployment / migration permissions | Changes that run `psql`, `kubectl`, `docker`, or equivalent in production require an explicit `deployment_gate`. |
| `audit_log` schema | The audit log is append-only, enforced by a sqlite trigger. No migration may DROP or ALTER the `audit_log` table without an explicit manual step by a human operator. |

---

## PromotionGate Contract

`lib/promotion_gate.sh:promotion_gate_check()` enforces all three conditions before any version is promoted:

```
1. utility_delta > 0
   candidate_score(benchmark_results) > current_score(benchmark_results)

2. All benchmark tasks pass
   SELECT COUNT(*) FROM benchmark_results
   WHERE version_id = :candidate AND passed = 0
   → must be 0

3. No safety constraint violated
   The rung-6 reviewer node must have written approved=true
   AND no constraint_violation flag in its output JSON
```

If any condition fails, the candidate is written to `workflow_candidates` with `status = 'quarantined'`.

**Utility formula (default):**

```
U = 0.45 * task_success_rate
  + 0.20 * verifier_pass_rate
  + 0.15 * artifact_quality_score
  - 0.10 * normalized_cost
  - 0.05 * normalized_latency
  - 0.05 * risk_penalty
```

Override per task class via `${MINI_ORK_HOME}/config/utility_functions/<task_class>.sh` — see [docs/EXTENSION.md](docs/EXTENSION.md).

---

## Quarantine and Rollback

A quarantined version cannot be re-promoted automatically. The path back is:

```bash
# Human clears quarantine after investigating
mini-ork version-clear-quarantine <version_id> --reason "reviewed: false positive"

# Then re-run promotion gate manually
mini-ork promote <version_id>
```

Rollback to a previous version:

```bash
mini-ork rollback workflow <workflow_id>  # restores previous promoted version
mini-ork rollback agent <agent_version_id>
```

Every rollback writes to `audit_log`. The previous version's `workflow_versions.status` is set back to `promoted`; the current version is set to `deprecated` (retained, not deleted).

---

## Audit Log

Every promote, quarantine, rollback, or safety-constraint-check writes an immutable row to `audit_log`.

```sql
-- db/migrations/007_safety.sql (excerpt)
CREATE TABLE IF NOT EXISTS audit_log (
  id           TEXT PRIMARY KEY,
  ts           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  actor        TEXT NOT NULL,   -- 'system' | 'human:<user>' | 'agent:<version_id>'
  action       TEXT NOT NULL,   -- 'promote' | 'quarantine' | 'rollback' | 'constraint_check'
  target_type  TEXT NOT NULL,   -- 'workflow_version' | 'agent_version' | 'config'
  target_id    TEXT NOT NULL,
  result       TEXT NOT NULL,   -- 'approved' | 'rejected' | 'quarantined'
  reason       TEXT,
  evidence_ref TEXT             -- run_id or benchmark_run_id that justified the decision
);

-- Append-only enforcement
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
  BEFORE UPDATE ON audit_log
  BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
  END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
  BEFORE DELETE ON audit_log
  BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
  END;
```

Query recent audit events:

```bash
sqlite3 "${MINI_ORK_DB}" \
  "SELECT ts, actor, action, target_type, target_id, result, reason
   FROM audit_log ORDER BY ts DESC LIMIT 20"
```

---

## Non-Goals

These behaviors are explicitly out of scope. Any proposed change (rung 6) that introduces them is rejected by the safety reviewer:

- **No hidden autonomous production mutation** — the system does not deploy, merge, or push to production without a gate. Rung-7 changes require human approval.
- **No fallback chains that hide failures** — `catch { return defaultValue }` patterns that replace a failed result with fabricated data are rejected. Fail explicitly; preserve sandboxes for inspection.
- **No promotion without measurable utility** — a version that scores the same or worse than the current version is not promoted, even if all benchmarks pass. The `utility_delta > 0` check is strict.
- **No memory writes without provenance** — every write to a memory namespace includes `run_id`, `task_id`, `agent_version_id`, and `ts`. Orphan records (missing provenance) are flagged by the nightly compaction job.
- **No silent model substitution** — if a model call fails (rate limit, network error, provider outage), the node fails loudly. There is no "try the cheaper model" fallback chain. The failure is recorded; the human or the orchestrator decides what to do next.
