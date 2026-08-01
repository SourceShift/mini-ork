/**
 * Derive "needs you" attention items from data the deck ALREADY polls — the
 * task-runs list + the active-runs list — so the strip costs zero extra
 * requests and never fans out a per-run profile call.
 *
 * Honesty: we surface only signals we can source cheaply and defensibly:
 *   · failed     — status === "failed"
 *   · escalated  — reviewer verdict demands a human (REQUEST_CHANGES/ESCALATE/CRASH)
 *   · stale      — still in an active status but AGED OUT of /runs/active (the
 *                  server drops runs whose updated_at is past the 6h cutoff), so
 *                  the set-difference IS the staleness signal.
 * We deliberately do NOT fabricate a "needs answers" item — that requires a bulk
 * profile endpoint we don't have, and a guessed flag is worse than an absent one.
 */

import type { ActiveRun, TaskRun } from "./api";

export type AttentionKind = "failed" | "escalated" | "stale";

export type AttentionItem = {
  id: string;
  kind: AttentionKind;
  reason: string;
  recipe: string | null;
  status: string | null;
  at: number | null;
};

const ESCALATION_VERDICTS = new Set(["REQUEST_CHANGES", "ESCALATE", "CRASH"]);
const ACTIVE_STATUSES = new Set(["executing", "verifying", "reviewing"]);

const KIND_RANK: Record<AttentionKind, number> = { failed: 0, escalated: 1, stale: 2 };

export function computeAttention(taskRuns: TaskRun[], activeRuns: ActiveRun[]): AttentionItem[] {
  // ids the server still considers live (recent heartbeat / update)
  const live = new Set<string>();
  for (const r of activeRuns) {
    const id = r.task_run_id ?? (r.source === "task_runs" ? String(r.id) : null);
    if (id) live.add(id);
  }

  const items: AttentionItem[] = [];
  for (const r of taskRuns) {
    if (r.status === "failed") {
      items.push({ id: r.id, kind: "failed", reason: "run failed", recipe: r.recipe, status: r.status, at: r.updated_at });
    } else if (r.verdict && ESCALATION_VERDICTS.has(r.verdict)) {
      items.push({
        id: r.id,
        kind: "escalated",
        reason: `reviewer: ${r.verdict.toLowerCase().replace(/_/g, " ")}`,
        recipe: r.recipe,
        status: r.status,
        at: r.updated_at,
      });
    } else if (ACTIVE_STATUSES.has(r.status) && !live.has(r.id)) {
      items.push({
        id: r.id,
        kind: "stale",
        reason: `${r.status} · no activity in 6h`,
        recipe: r.recipe,
        status: r.status,
        at: r.updated_at,
      });
    }
  }

  items.sort((a, b) => KIND_RANK[a.kind] - KIND_RANK[b.kind] || (b.at ?? 0) - (a.at ?? 0));
  return items;
}
