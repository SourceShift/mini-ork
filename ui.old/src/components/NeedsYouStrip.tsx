import { Link } from "@tanstack/react-router";
import { AlertOctagon, Clock, Flag, ShieldCheck } from "lucide-react";

import type { ActiveRun, TaskRun } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { computeAttention, type AttentionItem, type AttentionKind } from "@/lib/attention";

const KIND_META: Record<
  AttentionKind,
  { pill: string; icon: typeof AlertOctagon; label: string }
> = {
  failed: { pill: "pill-err", icon: AlertOctagon, label: "failed" },
  escalated: { pill: "pill-warn", icon: Flag, label: "escalated" },
  stale: { pill: "pill-muted", icon: Clock, label: "stale" },
};

/** The "needs you" strip: the deck's triage line. Everything here is derived
 * from the two lists the fleet already polls (task-runs + active-runs) — see
 * lib/attention.ts — so it costs zero extra requests. When nothing needs a
 * human we render a quiet all-clear rather than a scary empty card. */
export function NeedsYouStrip({
  taskRuns,
  activeRuns,
}: {
  taskRuns: TaskRun[];
  activeRuns: ActiveRun[];
}) {
  const items = computeAttention(taskRuns, activeRuns);

  if (!items.length) {
    return (
      <section className="card flex items-center gap-2 py-2" data-testid="needs-you-clear">
        <ShieldCheck size={14} className="text-[var(--grn)]" />
        <span className="text-[11px] text-ink-400">
          Nothing needs you — no failed, escalated, or stalled runs.
        </span>
      </section>
    );
  }

  return (
    <section className="card !p-0 overflow-hidden" data-testid="needs-you-strip" data-count={items.length}>
      <div className="panel-title !m-0 flex items-center gap-2">
        <span>Needs you</span>
        <span className="pill-err" data-testid="needs-you-count">
          {items.length}
        </span>
      </div>
      <div className="flex gap-2 overflow-x-auto p-2 thin">
        {items.map((item) => (
          <AttentionChip key={`${item.kind}-${item.id}`} item={item} />
        ))}
      </div>
    </section>
  );
}

function AttentionChip({ item }: { item: AttentionItem }) {
  const meta = KIND_META[item.kind];
  const Icon = meta.icon;
  return (
    <Link
      to="/runs/$taskRunId"
      params={{ taskRunId: item.id }}
      data-testid={`attention-${item.kind}-${item.id}`}
      className="group flex min-w-[220px] shrink-0 flex-col gap-1 rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-2.5 py-2 hover:border-[var(--amb)]"
      title={item.reason}
    >
      <div className="flex items-center gap-1.5">
        <Icon size={12} className="shrink-0" />
        <span className={meta.pill} data-testid="attention-kind">
          {meta.label}
        </span>
        <span className="ml-auto text-[9.5px] text-ink-500">{formatRelative(item.at)}</span>
      </div>
      <span className="truncate font-mono text-[11px] text-ink-100 group-hover:text-[var(--amb)]">
        {item.recipe ?? item.id}
      </span>
      <span className="truncate text-[10px] text-ink-400">{item.reason}</span>
    </Link>
  );
}
