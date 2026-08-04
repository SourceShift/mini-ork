import { useQuery } from "@tanstack/react-query";

import { api, type DispatchLane } from "@/lib/api";
import { formatCost } from "@/lib/format";
import { EvidenceBadge, FamilyPill } from "@/components/Pill";
import { laneEvidence } from "@/lib/evidence";

/** Lane-health leaderboard: which model lanes actually earn their dispatches.
 * Every rate here obeys the same honesty contract as the composer — a lane
 * below `min_samples` shows its raw k/n, never a fabricated %. Measured lanes
 * rank first (by rate); thin lanes sink to the bottom where they can't mislead. */
export function LaneHealthPanel() {
  const options = useQuery({ queryKey: ["dispatch-options"], queryFn: () => api.dispatchOptions() });
  const minSamples = options.data?.min_samples ?? 5;
  const lanes = rankLanes(options.data?.lanes ?? []);

  return (
    <section className="card !p-0 overflow-hidden" data-testid="lane-health-panel">
      <div className="panel-title !m-0 flex items-center gap-2">
        <span>Lane health</span>
        <span className="ml-auto text-[9px] font-normal tracking-normal text-ink-600">
          rate hidden below n={minSamples}
        </span>
      </div>

      {options.isLoading ? (
        <p className="p-4 text-[11px] text-ink-400">Loading lanes.</p>
      ) : !lanes.length ? (
        <p className="p-4 text-[11px] text-ink-400" data-testid="lane-health-empty">
          No lane telemetry yet — dispatch a few runs to populate.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="tbl" data-testid="lane-health-table">
            <thead>
              <tr>
                <th>Lane</th>
                <th>Win rate</th>
                <th className="text-right">Runs</th>
                <th className="text-right">Avg cost</th>
                <th className="text-right">Advantage</th>
              </tr>
            </thead>
            <tbody>
              {lanes.map((lane) => {
                const view = laneEvidence(lane, minSamples);
                return (
                  <tr key={`${lane.lane}-${lane.task_class ?? "all"}`} data-testid={`lane-row-${lane.lane}`}>
                    <td>
                      <div className="flex items-center gap-2">
                        <FamilyPill family={laneFamily(lane.lane)} />
                        <span className="font-mono text-[10.5px] text-ink-300">{lane.lane}</span>
                      </div>
                    </td>
                    <td><EvidenceBadge view={view} /></td>
                    <td className="text-right font-mono text-[11px] text-ink-400">
                      {lane.successes}/{lane.runs}
                    </td>
                    <td className="text-right font-mono text-[11px] text-ink-400">
                      {lane.avg_cost_usd != null ? formatCost(lane.avg_cost_usd) : "—"}
                    </td>
                    <td className="text-right">
                      <Advantage value={lane.advantage} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Advantage({ value }: { value: number | null }) {
  if (value == null) return <span className="font-mono text-[11px] text-ink-600">—</span>;
  const tone = value > 0.02 ? "text-[var(--grn)]" : value < -0.02 ? "text-[var(--red)]" : "text-ink-400";
  const sign = value > 0 ? "+" : "";
  return (
    <span className={`font-mono text-[11px] ${tone}`} title="measured advantage vs the lane's task-class baseline">
      {sign}
      {(value * 100).toFixed(0)}%
    </span>
  );
}

/** Measured lanes first (highest rate on top); thin/no-rate lanes sink below,
 * ordered by run volume so the closest-to-measurable sit just under the line. */
function rankLanes(lanes: DispatchLane[]): DispatchLane[] {
  return [...lanes].sort((a, b) => {
    const am = a.success_rate != null;
    const bm = b.success_rate != null;
    if (am !== bm) return am ? -1 : 1;
    if (am && bm) return (b.success_rate ?? 0) - (a.success_rate ?? 0);
    return b.runs - a.runs;
  });
}

/** First token of a lane id is its model family (glm-4.6 → glm, codex/gpt → codex). */
function laneFamily(lane: string): string {
  return lane.split(/[-_/]/)[0] || lane;
}
