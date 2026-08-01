import { useQuery } from "@tanstack/react-query";
import { TrendingDown, TrendingUp, Minus, Lightbulb } from "lucide-react";

import { api, type CostByDayRow } from "@/lib/api";
import { formatCost } from "@/lib/format";

type DayPoint = { day: string; costPerRun: number; runs: number };

/** The learning-loop flywheel — honestly. We can derive cost-per-run per day
 * (costByDay) and count the lessons the loop has harvested (gradients), but the
 * backend exposes no per-day verdict column, so we CANNOT plot a single
 * cost-vs-verified-correctness curve. Rather than fake that join, we show the
 * cost-efficiency trend beside the count of harvested lessons, and point the
 * operator at the lane-health leaderboard for the current measured correctness. */
export function FlywheelPanel() {
  const cost = useQuery({ queryKey: ["cost-by-day"], queryFn: api.costByDay });
  const gradients = useQuery({ queryKey: ["global-gradients"], queryFn: () => api.gradients(200) });

  const points = costPerRunByDay(cost.data ?? []);
  const latest = points.at(-1);
  const delta = trendDelta(points);
  const lessons = gradients.data ?? [];
  const avgConfidence = lessons.length
    ? lessons.reduce((s, g) => s + (g.confidence ?? 0), 0) / lessons.length
    : null;

  return (
    <section className="card" data-testid="flywheel-panel">
      <div className="panel-title">Flywheel · cost per run</div>

      {points.length < 2 || !latest ? (
        <p className="py-6 text-center text-[11px] text-ink-400" data-testid="flywheel-empty">
          Need ≥2 days of runs to trend cost efficiency.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-end justify-between gap-3">
            <div>
              <div className="font-mono text-[24px] font-black leading-none text-ink-100" data-testid="flywheel-current">
                {formatCost(latest.costPerRun)}
                <span className="ml-1 text-[10px] font-normal text-ink-500">/ run</span>
              </div>
              <div className="mt-1 text-[10px] text-ink-500">latest day · {latest.day.slice(5)}</div>
            </div>
            <TrendBadge delta={delta} spanDays={points.length} />
          </div>

          <Sparkline points={points} />
        </div>
      )}

      <div className="mt-3 flex items-center gap-2 border-t border-[var(--hair)] pt-2" data-testid="flywheel-lessons">
        <Lightbulb size={13} className="text-[var(--violet)]" />
        <span className="font-mono text-[13px] font-bold text-ink-100">{lessons.length}</span>
        <span className="text-[10px] text-ink-500">
          lessons harvested{avgConfidence != null ? ` · avg conf ${Math.round(avgConfidence * 100)}%` : ""}
        </span>
      </div>

      <p className="mt-2 text-[9.5px] leading-snug text-ink-600" data-testid="flywheel-honesty">
        Cost/run is all recipes combined. Verified correctness is measured per-lane
        in Lane health — no per-day verdict feed exists to join them into one curve,
        so they're shown side by side rather than faked as a single line.
      </p>
    </section>
  );
}

function TrendBadge({ delta, spanDays }: { delta: number | null; spanDays: number }) {
  if (delta == null) {
    return (
      <span className="pill-muted" data-testid="flywheel-trend" data-direction="flat">
        <Minus size={11} /> flat
      </span>
    );
  }
  // Cost going DOWN is the win — negative delta is green.
  const down = delta < -0.02;
  const up = delta > 0.02;
  const cls = down ? "pill-ok" : up ? "pill-err" : "pill-muted";
  const Icon = down ? TrendingDown : up ? TrendingUp : Minus;
  const pct = `${Math.abs(Math.round(delta * 100))}%`;
  return (
    <span className={cls} data-testid="flywheel-trend" data-direction={down ? "down" : up ? "up" : "flat"}>
      <Icon size={11} /> {down ? "↓" : up ? "↑" : ""}
      {pct} over {spanDays}d
    </span>
  );
}

function Sparkline({ points }: { points: DayPoint[] }) {
  const w = 320;
  const h = 56;
  const pad = 4;
  const vals = points.map((p) => p.costPerRun);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const step = points.length > 1 ? (w - pad * 2) / (points.length - 1) : 0;
  const xy = points.map((p, i) => {
    const x = pad + i * step;
    const y = pad + (1 - (p.costPerRun - min) / span) * (h - pad * 2);
    return { x, y, point: p };
  });
  const firstXY = xy[0];
  const lastXY = xy.at(-1);
  if (!firstXY || !lastXY) return null;
  const line = xy.map(({ x, y }, i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const area = `${line} L${lastXY.x.toFixed(1)} ${h - pad} L${firstXY.x.toFixed(1)} ${h - pad} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="block h-[56px] w-full" data-testid="flywheel-sparkline" preserveAspectRatio="none">
      <defs>
        <linearGradient id="flywheel-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--amb)" stopOpacity="0.22" />
          <stop offset="1" stopColor="var(--amb)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#flywheel-fill)" />
      <path d={line} fill="none" stroke="var(--amb)" strokeWidth="1.5" />
      {xy.map(({ x, y, point }, i) => (
        <circle key={point.day} cx={x} cy={y} r={i === xy.length - 1 ? 2.6 : 1.6} fill="var(--amb)">
          <title>{`${point.day}: ${formatCost(point.costPerRun)}/run · ${point.runs} runs`}</title>
        </circle>
      ))}
    </svg>
  );
}

/** Per day: total cost / total run_count across all recipes. */
function costPerRunByDay(rows: CostByDayRow[]): DayPoint[] {
  const byDay = new Map<string, { cost: number; runs: number }>();
  for (const r of rows) {
    const cur = byDay.get(r.day) ?? { cost: 0, runs: 0 };
    cur.cost += Number(r.cost ?? 0);
    cur.runs += Number(r.run_count ?? 0);
    byDay.set(r.day, cur);
  }
  return Array.from(byDay.entries())
    .filter(([, v]) => v.runs > 0)
    .map(([day, v]) => ({ day, costPerRun: v.cost / v.runs, runs: v.runs }))
    .sort((a, b) => a.day.localeCompare(b.day));
}

/** Fractional change first→last day; negative means cost went down (good). */
function trendDelta(points: DayPoint[]): number | null {
  const first = points[0];
  const last = points.at(-1);
  if (!first || !last || first.costPerRun <= 0) return null;
  return (last.costPerRun - first.costPerRun) / first.costPerRun;
}
