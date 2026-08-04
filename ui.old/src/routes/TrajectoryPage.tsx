import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";

import { api, type SelfImproveRun } from "@/lib/api";
import { formatCost, formatDuration, formatRelative } from "@/lib/format";
import { StatusPill } from "@/components/Pill";
import { IdeaTreePanel } from "@/components/IdeaTreePanel";

export function TrajectoryPage() {
  const selfImprove = useQuery({
    queryKey: ["self-improve"],
    queryFn: () => api.selfImprove(50),
    refetchInterval: 10_000,
  });
  const roots = useQuery({
    queryKey: ["idea-tree-roots"],
    queryFn: () => api.ideaTree.roots(),
    refetchInterval: 30_000,
  });
  const [selectedRootId, setSelectedRootId] = useState<string>("");
  useEffect(() => {
    if (!selectedRootId && roots.data?.[0]?.node_id) {
      setSelectedRootId(roots.data[0].node_id);
    }
  }, [roots.data, selectedRootId]);
  const tree = useQuery({
    queryKey: ["idea-tree", selectedRootId],
    queryFn: () => api.ideaTree.read(selectedRootId),
    enabled: Boolean(selectedRootId),
    refetchInterval: 30_000,
  });
  const cost = useQuery({ queryKey: ["cost-by-day"], queryFn: api.costByDay });
  const wall = useQuery({ queryKey: ["wall-time"], queryFn: api.wallTime });

  const costBars = totalsByDay(cost.data ?? [], "cost");
  const wallBars = averagesByDay(wall.data ?? [], "avg_ms");
  const totalCost = costBars.reduce((sum, row) => sum + row.value, 0);
  const avgWall = wallBars.length ? wallBars.reduce((sum, row) => sum + row.value, 0) / wallBars.length : 0;

  return (
    <div className="p-2 space-y-2 max-w-none" data-testid="trajectory-page">
      <header className="flex items-center gap-3 px-1 pb-1" data-testid="trajectory-header">
        <h1 className="m-0 text-[16px] font-black uppercase tracking-[0.08em] text-ink-100">Trajectory</h1>
        <span className="text-[10px] text-ink-500">cost down · wall-time stable · convergence happening</span>
      </header>

      <section className="grid grid-cols-1 xl:grid-cols-2 gap-2" data-testid="trajectory-charts">
        <TrajectoryBarCard
          title="cost / day"
          total={formatCost(totalCost)}
          rows={costBars}
          color="var(--amb)"
          formatValue={(value) => formatCost(value).replace("$", "")}
          empty={!costBars.length}
        />
        <TrajectoryBarCard
          title="avg wall-time / day"
          total={formatDuration(avgWall)}
          rows={wallBars}
          color="var(--cyan)"
          formatValue={formatDuration}
          empty={!wallBars.length}
        />
      </section>

      <section className="card !p-0 overflow-hidden" data-testid="hypothesis-tree-section">
        <div className="panel-title !m-0 flex items-center justify-between gap-3">
          <span>Hypothesis tree</span>
          <select
            className="max-w-[520px] rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2 py-1 font-mono text-[10px] text-ink-300 outline-none hover:border-[var(--hair-2)] focus:border-ork-amber"
            value={selectedRootId}
            onChange={(event) => setSelectedRootId(event.target.value)}
            disabled={!roots.data?.length}
            aria-label="Select hypothesis tree root"
            data-testid="idea-tree-root-select"
          >
            {(roots.data ?? []).map((root) => (
              <option key={root.node_id} value={root.node_id}>
                {rootLabel(root)}
              </option>
            ))}
          </select>
        </div>
        {roots.isLoading || tree.isLoading ? (
          <p className="p-4 text-sm text-ink-400">Loading hypothesis tree.</p>
        ) : roots.isError || tree.isError ? (
          <p className="p-4 text-sm text-red-300">Could not load the hypothesis tree.</p>
        ) : (
          <IdeaTreePanel tree={tree.data} />
        )}
      </section>

      <section className="card" data-testid="self-improve-section">
        <div className="panel-title">self-improve ledger</div>
        {selfImprove.data?.length ? (
          <div className="overflow-x-auto">
            <table className="tbl" data-testid="self-improve-table">
              <thead>
                <tr>
                  <Th>Iter</Th>
                  <Th>Outcome</Th>
                  <Th>Task_run</Th>
                  <Th className="text-right">Cost</Th>
                  <Th className="text-right">Wall</Th>
                  <Th className="text-right">When</Th>
                  <Th className="w-6">{""}</Th>
                </tr>
              </thead>
              <tbody>
                {selfImprove.data.map((s) => (
                  <SelfImproveRow key={s.run_id} row={s} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-400">No self_improve_runs recorded.</p>
        )}
      </section>
    </div>
  );
}

function rootLabel(root: {
  node_id: string;
  hypothesis: string | null;
  node_count: number;
  harvested_count: number;
  pruned_count: number;
}): string {
  const hypothesis = root.hypothesis?.trim() || root.node_id;
  const short = hypothesis.length > 84 ? `${hypothesis.slice(0, 81)}...` : hypothesis;
  return `${short} (${root.node_count} nodes, ${root.harvested_count} harvested, ${root.pruned_count} pruned)`;
}

function SelfImproveRow({ row }: { row: SelfImproveRun }) {
  return (
    <tr
      className="cursor-pointer"
      data-testid={`self-improve-row-${row.iter}`}
      data-iter={row.iter}
      data-outcome={row.outcome}
    >
      <Td>
        <Link
          to="/trajectory/self-improve/$runId"
          params={{ runId: row.run_id }}
          className="font-mono text-ink-100 hover:text-ork-amber block"
        >
          iter {row.iter}
        </Link>
      </Td>
      <Td>
        <Link
          to="/trajectory/self-improve/$runId"
          params={{ runId: row.run_id }}
          className="block"
        >
          <StatusPill status={row.outcome} />
        </Link>
      </Td>
      <Td className="font-mono text-[11px] text-[var(--cyan)]">{row.run_id}</Td>
      <Td className="text-right font-mono text-[11px] text-ink-400">{formatCost((row as any).cost_usd ?? 0)}</Td>
      <Td className="text-right font-mono text-[11px] text-ink-400">{formatDuration(wallMs(row))}</Td>
      <Td className="text-right font-mono text-[11px] text-ink-500">{formatRelative(row.started_at)}</Td>
      <Td>
        <Link
          to="/trajectory/self-improve/$runId"
          params={{ runId: row.run_id }}
          className="text-ink-500 hover:text-ink-100"
          aria-label="open detail"
        >
          <ChevronRight size={14} />
        </Link>
      </Td>
    </tr>
  );
}

function wallMs(row: SelfImproveRun): number | null {
  if (!row.finished_at || !row.started_at) return null;
  return Math.max(0, (row.finished_at - row.started_at) * 1000);
}

function totalsByDay<T extends Record<string, any>>(
  rows: T[],
  valueKey: string,
): Array<{ day: string; value: number }> {
  const byDay = new Map<string, number>();
  for (const r of rows) {
    byDay.set(r.day, (byDay.get(r.day) ?? 0) + Number(r[valueKey] ?? 0));
  }
  return Array.from(byDay.entries()).map(([day, value]) => ({ day, value })).sort((a, b) => a.day.localeCompare(b.day));
}

function averagesByDay<T extends Record<string, any>>(
  rows: T[],
  valueKey: string,
): Array<{ day: string; value: number }> {
  const byDay = new Map<string, { sum: number; count: number }>();
  for (const r of rows) {
    const cur = byDay.get(r.day) ?? { sum: 0, count: 0 };
    cur.sum += Number(r[valueKey] ?? 0);
    cur.count += 1;
    byDay.set(r.day, cur);
  }
  return Array.from(byDay.entries())
    .map(([day, value]) => ({ day, value: value.count ? value.sum / value.count : 0 }))
    .sort((a, b) => a.day.localeCompare(b.day));
}

function TrajectoryBarCard({
  title,
  total,
  rows,
  color,
  formatValue,
  empty,
}: {
  title: string;
  total: string;
  rows: Array<{ day: string; value: number }>;
  color: string;
  formatValue: (value: number) => string;
  empty?: boolean;
}) {
  const max = Math.max(...rows.map((row) => row.value), 1);
  return (
  <div className="card min-h-[150px]">
    <div className="panel-title">{title}</div>
    {empty ? (
      <p className="text-sm text-ink-400 py-8 text-center">no data yet</p>
    ) : (
      <>
        <div className="mb-1 text-right font-mono text-[11px] text-ink-400">{total}</div>
        <div className="flex h-[108px] items-end gap-2 px-8 pb-2">
          {rows.slice(-7).map((row, index, visible) => {
            const height = Math.max(4, (row.value / max) * 82);
            const isLast = index === visible.length - 1;
            return (
              <div key={row.day} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1">
                <span className="font-mono text-[8px] text-ink-500">{formatValue(row.value)}</span>
                <div
                  className="w-full max-w-[28px] rounded-t-[2px]"
                  style={{ height, background: color, opacity: isLast ? 0.95 : 0.5 }}
                />
                <span className="font-mono text-[8px] text-ink-600">{row.day.slice(5)}</span>
              </div>
            );
          })}
        </div>
      </>
    )}
  </div>
  );
}

const Th = ({ children, className = "" }: { children: React.ReactNode; className?: string }) => (
  <th className={className}>{children}</th>
);
const Td = ({
  children,
  className = "",
  ...props
}: React.TdHTMLAttributes<HTMLTableCellElement>) => (
  <td className={className} {...props}>{children}</td>
);
