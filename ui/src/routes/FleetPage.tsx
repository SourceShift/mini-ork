import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Activity, Banknote, Boxes, GaugeCircle } from "lucide-react";

import { api, type TaskRun } from "@/lib/api";
import { formatCost, formatDuration, formatRelative } from "@/lib/format";
import { StatusPill, VerdictPill } from "@/components/Pill";

export function FleetPage() {
  // Fleet polls — browsers cap ~6 concurrent HTTP/1.1 connections per
  // origin, so over-eager polling competes with SSE + nav clicks and
  // surfaces as stalled requests in DevTools. SSE already pushes deltas
  // for live changes, so REST polls only need to catch missed cases.
  const summary = useQuery({
    queryKey: ["summary"],
    queryFn: api.taskRunsSummary,
    refetchInterval: 10_000,
  });
  const active = useQuery({
    queryKey: ["active-runs"],
    queryFn: api.activeRuns,
    refetchInterval: 5_000,
  });
  const runs = useQuery({
    queryKey: ["task-runs"],
    queryFn: () => api.taskRuns({ limit: 100 }),
    refetchInterval: 10_000,
  });

  const totalRuns = summary.data?.by_status.reduce((acc, s) => acc + s.count, 0) ?? 0;
  const activeRuns = active.data?.length ?? summary.data?.by_status.find((s) => s.status === "executing")?.count ?? 0;
  const failedRuns = summary.data?.by_status.find((s) => s.status === "failed")?.count ?? 0;

  return (
    <div className="p-3 space-y-3 max-w-[1500px] mx-auto" data-testid="fleet-page">
      <header className="card" data-testid="fleet-header">
        <div className="panel-title">Command Deck</div>
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="m-0 text-[21px] font-black uppercase tracking-[0.08em] text-ink-100">Fleet</h1>
            <p className="text-[12px] text-ink-400 mt-1">
              Active dispatches and recent <code>task_runs</code> from <code>.mini-ork/state.db</code>.
            </p>
          </div>
          <div className="hidden md:flex items-center gap-2 text-[10px] uppercase tracking-[0.12em] text-ink-500">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--grn)] live-dot" style={{ color: "var(--grn)" }} />
            query_only
          </div>
        </div>
      </header>

      <section className="grid grid-cols-2 lg:grid-cols-4 gap-2" data-testid="fleet-stats">
        <Stat icon={<Boxes size={16} />} label="task_runs" value={totalRuns} testid="stat-task-runs" />
        <Stat
          icon={<Activity size={16} />}
          label="in flight"
          value={activeRuns}
          tone={activeRuns ? "warn" : "muted"}
          testid="stat-executing"
        />
        <Stat
          icon={<GaugeCircle size={16} />}
          label="failed"
          value={failedRuns}
          tone={failedRuns ? "err" : "muted"}
          testid="stat-failed"
        />
        <Stat
          icon={<Banknote size={16} />}
          label="total spend"
          value={formatCost(summary.data?.total_cost_usd)}
          testid="stat-total-spend"
        />
      </section>

      <section className="card !p-0 overflow-hidden" data-testid="fleet-dispatch-map-section">
        <div className="panel-title !m-0">Fleet · dispatch map</div>
        <FleetDispatchMap runs={runs.data ?? []} activeCount={activeRuns} totalCount={totalRuns} />
      </section>

      <section className="card" data-testid="active-runs-section">
        <div className="panel-title">Active Runs</div>
        {active.data?.length ? (
          <div className="overflow-x-auto">
            <table className="tbl" data-testid="active-runs-table">
              <thead>
                <tr>
                  <Th>Epic / Run</Th>
                  <Th>Agent</Th>
                  <Th>Source</Th>
                  <Th>Started</Th>
                  <Th>Updated</Th>
                  <Th>State</Th>
                  <Th>Cost</Th>
                </tr>
              </thead>
              <tbody>
                {active.data.map((r) => {
                  const taskRunId = r.task_run_id ?? (r.source === "task_runs" ? String(r.id) : null);
                  const label = r.epic_title ?? r.epic_id ?? String(r.id);
                  return (
                    <tr
                      key={`${r.source}-${r.id}`}
                      className="cursor-pointer"
                      data-testid={`active-run-${r.id}`}
                    >
                      <Td className="font-mono text-xs">
                        {taskRunId ? (
                          <Link
                            to="/runs/$taskRunId"
                            params={{ taskRunId }}
                            className="text-ink-100 hover:text-[var(--amb)]"
                          >
                            {label}
                          </Link>
                        ) : (
                          label
                        )}
                      </Td>
                      <Td>{r.agent}</Td>
                      <Td className="font-mono text-xs text-ink-300">{r.source === "task_runs" ? "task_runs" : r.branch}</Td>
                      <Td>{formatRelative(r.started_at)}</Td>
                      <Td>{formatRelative(r.last_heartbeat_at)}</Td>
                      <Td><StatusPill status={r.status ?? r.test_status} /></Td>
                      <Td>{formatCost(r.cost_usd)}</Td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-[12px] text-ink-400" data-testid="active-runs-empty">
            No active runs from task_runs or heartbeat-tracked runs.
          </p>
        )}
      </section>

      <section className="card" data-testid="recent-runs-section">
        <div className="panel-title">Recent task_runs</div>
        <div className="overflow-x-auto">
          <table className="tbl" data-testid="recent-runs-table">
            <thead>
              <tr>
                <Th>ID</Th>
                <Th>Recipe</Th>
                <Th>Status</Th>
                <Th>Verdict</Th>
                <Th>Cost</Th>
                <Th>Duration</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody>
              {runs.data?.map((r) => (
                <tr
                  key={r.id}
                  className="cursor-pointer"
                  data-testid={`task-run-row-${r.id}`}
                >
                  <Td>
                    <Link
                      to="/runs/$taskRunId"
                      params={{ taskRunId: r.id }}
                      className="font-mono text-[11px] text-ink-100 hover:text-[var(--amb)]"
                      data-testid={`task-run-link-${r.id}`}
                    >
                      {r.id}
                    </Link>
                  </Td>
                  <Td className="text-ink-300">{r.recipe ?? "—"}</Td>
                  <Td><StatusPill status={r.status} /></Td>
                  <Td><VerdictPill verdict={r.verdict} /></Td>
                  <Td>{formatCost(r.cost_usd)}</Td>
                  <Td>{formatDuration(r.duration_ms)}</Td>
                  <Td className="text-ink-400">{formatRelative(r.created_at)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
          {!runs.data?.length && (
            <p className="text-[12px] text-ink-400 py-2" data-testid="recent-runs-empty">
              No task_runs yet.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

function FleetDispatchMap({
  runs,
  activeCount,
  totalCount,
}: {
  runs: TaskRun[];
  activeCount: number;
  totalCount: number;
}) {
  const shown = runs.slice(0, 14);
  const width = 900;
  const height = 300;
  const cx = width / 2;
  const cy = height / 2;
  const radiusX = width * 0.38;
  const radiusY = height * 0.33;

  if (!shown.length) {
    return (
      <div className="empty m-3" data-testid="fleet-dispatch-map-empty">
        <div className="sm">No runs to map.</div>
      </div>
    );
  }

  return (
    <div className="relative" data-testid="fleet-dispatch-map" data-active={activeCount} data-total={totalCount}>
      <svg viewBox={`0 0 ${width} ${height}`} className="block h-[300px] w-full">
        <defs>
          <radialGradient id="fleet-map-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0" stopColor="rgba(165,40,40,0.24)" />
            <stop offset="1" stopColor="transparent" />
          </radialGradient>
        </defs>
        <rect x="0" y="0" width={width} height={height} fill="var(--panel)" />
        <ellipse cx={cx} cy={cy} rx="150" ry="82" fill="url(#fleet-map-glow)" />
        {shown.map((run, index) => {
          const angle = (index / Math.max(shown.length, 1)) * Math.PI * 2 - Math.PI / 2;
          const x = cx + Math.cos(angle) * radiusX;
          const y = cy + Math.sin(angle) * radiusY;
          const statusColor = run.status === "executing" ? "var(--grn)" : run.status === "failed" ? "var(--red)" : "var(--edge)";
          return (
            <line
              key={`edge-${run.id}`}
              x1={cx}
              y1={cy}
              x2={x}
              y2={y}
              stroke={statusColor}
              strokeWidth={run.status === "executing" ? 1.5 : 1}
              strokeOpacity={run.status === "executing" ? 0.6 : 0.35}
              strokeDasharray={run.status === "executing" ? undefined : "2 4"}
            />
          );
        })}
        <OrchestratorNode x={cx} y={cy} />
        {shown.map((run, index) => {
          const angle = (index / Math.max(shown.length, 1)) * Math.PI * 2 - Math.PI / 2;
          const x = cx + Math.cos(angle) * radiusX;
          const y = cy + Math.sin(angle) * radiusY;
          return <RunMapNode key={run.id} run={run} x={x} y={y} />;
        })}
      </svg>
      <div className="absolute bottom-2 left-3 flex flex-wrap items-center gap-3 text-[9.5px] uppercase tracking-[0.08em] text-ink-500">
        <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-[var(--grn)] live-dot" style={{ color: "var(--grn)" }} />executing</span>
        <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-[var(--red)]" />failed</span>
        <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-[var(--edge)]" />done/other</span>
        <span className="text-ink-600">·</span>
        <span>{activeCount} in flight · {totalCount} total</span>
      </div>
    </div>
  );
}

function RunMapNode({ run, x, y }: { run: TaskRun; x: number; y: number }) {
  const ring = run.status === "executing" ? "var(--grn)" : run.status === "failed" ? "var(--red)" : "var(--edge)";
  const fill = run.status === "failed" ? "rgba(240,88,78,0.16)" : run.status === "executing" ? "rgba(79,209,160,0.16)" : "rgba(255,255,255,0.045)";
  const label = run.id.length > 13 ? run.id.slice(0, 13) : run.id;
  return (
    <Link to="/runs/$taskRunId" params={{ taskRunId: run.id }} data-testid={`fleet-map-node-${run.id}`}>
      <g className="cursor-pointer">
        {run.status === "executing" && (
          <circle cx={x} cy={y} r="22" fill="none" stroke="var(--grn)" strokeWidth="1" opacity="0.35">
            <animate attributeName="r" values="18;28" dur="1.8s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.5;0" dur="1.8s" repeatCount="indefinite" />
          </circle>
        )}
        <polygon points={hexPoints(x, y, 17)} fill={fill} stroke={ring} strokeWidth="1.4" />
        <path d={`M${x - 5} ${y - 2} L${x - 2.4} ${y - 0.8} M${x + 5} ${y - 2} L${x + 2.4} ${y - 0.8}`} stroke={ring} strokeWidth="1.1" strokeLinecap="round" />
        <circle cx={x - 3.4} cy={y + 0.6} r="0.9" fill={ring} />
        <circle cx={x + 3.4} cy={y + 0.6} r="0.9" fill={ring} />
        <path d={`M${x - 2.6} ${y + 4.4} L${x - 3.2} ${y + 2} M${x + 2.6} ${y + 4.4} L${x + 3.2} ${y + 2}`} stroke={ring} strokeWidth="1.1" strokeLinecap="round" />
        {run.status === "failed" && (
          <>
            <circle cx={x + 12} cy={y - 12} r="4.2" fill="var(--bg)" stroke="var(--red)" strokeWidth="1" />
            <path d={`M${x + 12} ${y - 14} v2.2 M${x + 12} ${y - 10.4} v0.1`} stroke="var(--red)" strokeWidth="1.1" strokeLinecap="round" />
          </>
        )}
        <text x={x} y={y + 30} textAnchor="middle" fill="var(--tx-4)" style={{ fontSize: 8, fontFamily: "var(--mono)" }}>
          {label}
        </text>
      </g>
    </Link>
  );
}

function OrchestratorNode({ x, y }: { x: number; y: number }) {
  return (
    <g>
      <polygon points={hexPoints(x, y, 25)} fill="var(--ork-red, #a52828)" fillOpacity="0.42" stroke="#e8a59a" strokeOpacity="0.45" strokeWidth="1.4" />
      <path d={`M${x - 8} ${y - 3} L${x - 3.4} ${y - 1} M${x + 8} ${y - 3} L${x + 3.4} ${y - 1}`} stroke="#f3d9c8" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx={x - 4.6} cy={y + 1.2} r="1.4" fill="#f3d9c8" />
      <circle cx={x + 4.6} cy={y + 1.2} r="1.4" fill="#f3d9c8" />
      <path d={`M${x - 3.4} ${y + 7} L${x - 4.2} ${y + 3} M${x + 3.4} ${y + 7} L${x + 4.2} ${y + 3}`} stroke="#f3d9c8" strokeWidth="1.7" strokeLinecap="round" />
      <text x={x} y={y + 40} textAnchor="middle" fill="var(--tx-3)" style={{ fontSize: 8, fontFamily: "var(--mono)", letterSpacing: "0.12em" }}>
        ORCHESTRATOR
      </text>
    </g>
  );
}

function hexPoints(x: number, y: number, r: number): string {
  const points: string[] = [];
  for (let i = 0; i < 6; i += 1) {
    const angle = (Math.PI / 180) * (60 * i - 90);
    points.push(`${(x + r * Math.cos(angle)).toFixed(1)},${(y + r * Math.sin(angle)).toFixed(1)}`);
  }
  return points.join(" ");
}

function Stat({
  icon,
  label,
  value,
  tone = "muted",
  testid,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  tone?: "muted" | "warn" | "err";
  testid?: string;
}) {
  const toneClass =
    tone === "warn" ? "text-ork-amber" : tone === "err" ? "text-red-300" : "text-ink-100";
  return (
    <div className="card flex flex-col gap-1" data-testid={testid} data-tone={tone}>
      <div className="text-[9.5px] uppercase tracking-[0.13em] text-ink-500 flex items-center gap-1.5">
        {icon}
        {label}
      </div>
      <div className={`text-[24px] leading-none font-black font-mono ${toneClass}`} data-testid={testid ? `${testid}-value` : undefined}>
        {value}
      </div>
    </div>
  );
}

const Th = ({ children }: { children: React.ReactNode }) => (
  <th>{children}</th>
);
const Td = ({ children, className = "" }: { children: React.ReactNode; className?: string }) => (
  <td className={className}>{children}</td>
);
