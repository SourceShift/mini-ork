import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import type React from "react";
import { useState } from "react";
import { Activity, BrainCircuit, ChevronDown, GitBranch, Network, Radio, Search, Telescope } from "lucide-react";

import { api, type TaskRun } from "@/lib/api";
import { formatCost } from "@/lib/format";

const NAV = [
  { to: "/", label: "Fleet", icon: Activity, key: "1" },
  { to: "/trajectory", label: "Trajectory", icon: Telescope, key: "2" },
  { to: "/fingerprint", label: "Fingerprint", icon: Network, key: "3" },
] as const;

export function Shell() {
  const { location } = useRouterState();
  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 30_000,
  });
  const { data: summary } = useQuery({
    queryKey: ["summary"],
    queryFn: api.taskRunsSummary,
    refetchInterval: 10_000,
  });
  const { data: runs } = useQuery({
    queryKey: ["shell-task-runs"],
    queryFn: () => api.taskRuns({ limit: 24 }),
    refetchInterval: 10_000,
  });
  const { data: gradients } = useQuery({
    queryKey: ["shell-gradients"],
    queryFn: () => api.gradients(10),
    refetchInterval: 30_000,
  });

  const executing = summary?.by_status.find((s) => s.status === "executing")?.count ?? 0;
  const failed = summary?.by_status.find((s) => s.status === "failed")?.count ?? 0;
  const total = summary?.by_status.reduce((sum, row) => sum + row.count, 0) ?? 0;
  const pathLabel = location.pathname === "/" ? "/fleet" : location.pathname;

  return (
    <div className="ork-shell" data-testid="app-shell">
      <header className="ork-topbar" data-testid="app-topbar">
        <div className="ork-brand" data-testid="app-brand">
          <div className="ork-mark">↯</div>
          <span className="font-black tracking-[0.16em] text-[13px] text-ink-100">
            ORK<span style={{ color: "var(--red)" }}>·</span>COMMAND
          </span>
          <span className="text-[9px] text-ink-500 -mt-3">v0.1</span>
        </div>
        <div className="ork-telemetry">
          <Tele label="in flight" value={executing} tone={executing ? "ok" : "default"} live={executing > 0} />
          <Tele label="spend total" value={formatCost(summary?.total_cost_usd)} tone="warn" />
          <Tele label="failures" value={failed} tone={failed ? "err" : "default"} />
          <Tele label="runs" value={total} />
          <Tele label="db" value={health?.ok ? "state.db" : "offline"} tone={health?.ok ? "ok" : "err"} />
        </div>
      </header>

      <div className="ork-body">
        <aside className="ork-rail" data-testid="app-sidebar">
          <div className="px-3 pt-3 pb-2 text-[9.5px] uppercase tracking-[0.18em] text-ink-500 ork-rail-meta">
            Navigation
          </div>
          <nav className="flex-1" data-testid="app-nav">
            {NAV.map(({ to, label, icon: Icon, key }) => {
              const active = to === "/" ? location.pathname === "/" : location.pathname.startsWith(to);
              return (
                <Link
                  key={to}
                  to={to}
                  data-testid={`nav-${label.toLowerCase()}`}
                  data-active={active}
                  className="ork-nav-item"
                >
                  <span className="kbd">{key}</span>
                  <Icon size={15} />
                  <span className="ork-nav-label">{label}</span>
                  {label === "Fingerprint" && (
                    <span className="ml-auto pill-muted" data-testid="nav-fingerprint-learning-count">
                      {gradients?.length ?? 0}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>
          <RailFleet runs={runs ?? []} activePath={location.pathname} />
          <div className="border-t border-[var(--hair)] p-3 space-y-2 ork-rail-meta">
            <div className="text-[9.5px] uppercase tracking-[0.18em] text-ink-500">Substrate</div>
            <div className="flex items-center gap-2 text-[10px] text-ink-400">
              <GitBranch size={12} />
              <span className="font-mono truncate" title={health?.db_path}>
                {health?.db_path?.split("/").slice(-2).join("/") ?? "—"}
              </span>
            </div>
            <div
              data-testid="app-db-status"
              data-status={health?.ok ? "connected" : "unreachable"}
              className={clsx("flex items-center gap-2 text-[10px]", health?.ok ? "text-[var(--grn)]" : "text-[var(--red)]")}
            >
              <span
                className={clsx(
                  "h-1.5 w-1.5 rounded-full",
                  health?.ok ? "bg-[var(--grn)] live-dot" : "bg-[var(--red)]",
                )}
                style={{ color: health?.ok ? "var(--grn)" : "var(--red)" }}
              />
              {health?.ok ? "query_only connected" : "state.db unreachable"}
            </div>
            <div className="flex items-center gap-2 text-[10px] text-ink-500">
              <BrainCircuit size={12} />
              <span>learning records visible per run</span>
            </div>
          </div>
        </aside>

        <main className="ork-main" data-testid="app-main">
          <Outlet />
        </main>
      </div>

      <footer className="ork-statusline" data-testid="app-status">
        <span className="pill-ok rounded-none">operator</span>
        <span className="font-mono uppercase tracking-[0.08em]">{sectionLabel(location.pathname)}</span>
        <span className="font-mono truncate flex-1">{pathLabel}</span>
        <span className="hidden md:inline-flex items-center gap-1">
          <Radio size={11} />
          loopback · readonly
        </span>
        <span className="kbd">⌘K</span>
      </footer>
    </div>
  );
}

function RailFleet({ runs, activePath }: { runs: TaskRun[]; activePath: string }) {
  const [open, setOpen] = useState(true);
  const [q, setQ] = useState("");
  const filtered = runs.filter((run) => {
    if (!q) return true;
    const needle = q.toLowerCase();
    return run.id.toLowerCase().includes(needle) || (run.recipe ?? "").toLowerCase().includes(needle);
  });

  return (
    <section className="border-y border-[var(--hair)] ork-rail-meta">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[11px] font-bold uppercase tracking-[0.08em] text-ink-300 hover:bg-white/[0.035]"
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronDown size={12} className={clsx("transition-transform", !open && "-rotate-90")} />
        Fleet
        <span className="ml-auto pill-ok">{runs.filter((r) => r.status === "executing").length}</span>
      </button>
      {open && (
        <div className="pb-2">
          <label className="relative mx-3 mb-2 block">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-ink-500" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="grep runs..."
              className="h-6 w-full rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] pl-7 pr-2 text-[11px] text-ink-200 outline-none placeholder:text-ink-600 focus:border-[var(--cyan)]"
            />
          </label>
          <div className="max-h-[260px] overflow-auto thin">
            {filtered.slice(0, 16).map((run) => {
              const active = activePath === `/runs/${run.id}` || activePath.startsWith(`/runs/${run.id}/`);
              const stripe = run.status === "failed" ? "var(--red)" : run.status === "executing" ? "var(--grn)" : "var(--tx-4)";
              return (
                <Link
                  key={run.id}
                  to="/runs/$taskRunId"
                  params={{ taskRunId: run.id }}
                  className="flex items-center gap-2 border-l-2 px-3 py-1.5 hover:bg-white/[0.035]"
                  style={{ borderLeftColor: active ? "var(--amb)" : "transparent" }}
                >
                  <span
                    className={clsx("h-1.5 w-1.5 rounded-full", run.status === "executing" && "live-dot")}
                    style={{ background: stripe, color: stripe }}
                  />
                  <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-ink-300">{shortRunId(run.id)}</span>
                  <span className="max-w-[62px] truncate text-[9.5px] text-ink-500">{run.recipe ?? run.task_class}</span>
                </Link>
              );
            })}
            {!filtered.length && <div className="px-4 py-2 text-[10.5px] text-ink-500">no match</div>}
          </div>
        </div>
      )}
    </section>
  );
}

function shortRunId(id: string): string {
  return id.length > 14 ? id.slice(0, 14) : id;
}

function Tele({
  label,
  value,
  tone = "default",
  live = false,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "default" | "ok" | "warn" | "err";
  live?: boolean;
}) {
  const color =
    tone === "ok" ? "var(--grn)" : tone === "warn" ? "var(--amb)" : tone === "err" ? "var(--red)" : "var(--tx)";
  return (
    <div className="ork-tele">
      <span className="ork-tele-label">{label}</span>
      <span className="flex items-center gap-1">
        {live && <span className="h-1.5 w-1.5 rounded-full bg-[var(--grn)] live-dot" style={{ color: "var(--grn)" }} />}
        <span className="ork-tele-value" style={{ color }}>
          {value}
        </span>
      </span>
    </div>
  );
}

function sectionLabel(pathname: string): string {
  if (pathname.startsWith("/runs/") && pathname.includes("/agents/")) return "agent forensics";
  if (pathname.startsWith("/runs/")) return "run forensics";
  if (pathname.startsWith("/trajectory")) return "trajectory";
  if (pathname.startsWith("/fingerprint")) return "fingerprint";
  return "fleet";
}
