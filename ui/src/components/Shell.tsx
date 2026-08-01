import { Link, Outlet, useRouter, useRouterState } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import type React from "react";
import { useEffect, useRef, useState } from "react";
import { Activity, BookMarked, BrainCircuit, ChevronDown, FolderOpen, GitBranch, Network, Radio, Rocket, Search, Telescope, X } from "lucide-react";

import { api, getWorkspaceHome, setWorkspaceHome, type TaskRun } from "@/lib/api";
import { formatCost } from "@/lib/format";
import { OperatorTokenControl } from "@/components/OperatorTokenControl";

const NAV = [
  { to: "/", label: "Fleet", icon: Activity, key: "1" },
  { to: "/new", label: "New Run", icon: Rocket, key: "2" },
  { to: "/recipes", label: "Recipes", icon: BookMarked, key: "3" },
  { to: "/trajectory", label: "Trajectory", icon: Telescope, key: "4" },
  { to: "/fingerprint", label: "Fingerprint", icon: Network, key: "5" },
] as const;

export function Shell() {
  const { location } = useRouterState();
  const router = useRouter();

  // Keep ?ws= pinned in the URL so reloads/deep links resolve to the right
  // workspace. Router navigations strip unknown search params; re-append via
  // replaceState (doesn't re-trigger the router). Subscribe to onResolved
  // because the router commits the URL asynchronously — an effect keyed on
  // location.href can run before window.location reflects the navigation.
  useEffect(() => {
    const sync = () => {
      const ws = getWorkspaceHome();
      const url = new URL(window.location.href);
      const current = url.searchParams.get("ws");
      if (ws === current || (!ws && current === null)) return;
      if (ws) url.searchParams.set("ws", ws);
      else url.searchParams.delete("ws");
      window.history.replaceState(window.history.state, "", url);
    };
    sync();
    return router.subscribe("onResolved", sync);
  }, [router]);

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
  const { data: activeRuns } = useQuery({
    queryKey: ["shell-active-runs"],
    queryFn: api.activeRuns,
    refetchInterval: 5_000,
  });
  const { data: gradients } = useQuery({
    queryKey: ["shell-gradients"],
    queryFn: () => api.gradients(10),
    refetchInterval: 30_000,
  });

  const executing = activeRuns?.length ?? 0;
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
        <ProjectSwitcher />
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
          <RailFleet runs={runs ?? []} activePath={location.pathname} activeCount={executing} />
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
        <OperatorTokenControl />
        <span className="font-mono uppercase tracking-[0.08em]">{sectionLabel(location.pathname)}</span>
        <span className="font-mono truncate flex-1">{pathLabel}</span>
        <span className="hidden md:inline-flex items-center gap-1">
          <Radio size={11} />
          loopback · control
        </span>
        <span className="kbd">⌘K</span>
      </footer>
    </div>
  );
}

function ProjectSwitcher() {
  const qc = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  const { data } = useQuery({
    queryKey: ["projects"],
    queryFn: api.projects,
    refetchInterval: 30_000,
  });

  const sw = useMutation({
    mutationFn: (home: string) => api.switchProject(home),
    onSuccess: (res) => {
      setOpen(false);
      // Pin the workspace client-side (header + ?ws= + localStorage), drop
      // every cached query from the old project, and land on the fleet —
      // run/agent pages from the previous workspace would 404 here.
      setWorkspaceHome(res.active);
      qc.clear();
      // Stamp ?ws= here too: navigating "/"→"/" is a location no-op, so the
      // Shell URL-keeper effect wouldn't fire.
      const url = new URL(window.location.href);
      url.searchParams.set("ws", res.active);
      window.history.replaceState(window.history.state, "", url);
      void router.navigate({ to: "/" });
    },
  });

  const rm = useMutation({
    mutationFn: (home: string) => api.removeProject(home),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      // A polling re-render can swap the clicked row's DOM node before this
      // document-level handler runs; a detached target is NOT an outside
      // click — without this guard the dropdown closes on inner clicks.
      if (!document.contains(t)) return;
      if (boxRef.current && !boxRef.current.contains(t)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const active = data?.projects.find((p) => p.active);

  return (
    <div ref={boxRef} className="relative" data-testid="project-switcher">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-2 py-1 text-[10.5px] font-mono text-ink-300 hover:border-[var(--cyan)]"
        title={active?.home}
        data-testid="project-switcher-toggle"
      >
        <FolderOpen size={12} />
        <span className="max-w-[140px] truncate">{active?.name ?? "project"}</span>
        <ChevronDown size={11} className={clsx("transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-[340px] rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] shadow-lg">
          <div className="px-3 pt-2 pb-1 text-[9.5px] uppercase tracking-[0.18em] text-ink-500">Projects</div>
          {(data?.projects ?? []).map((p) => (
            <div
              key={p.home}
              className={clsx(
                "group flex w-full items-center gap-2 px-3 py-1.5 text-[11px] hover:bg-white/[0.035]",
                p.active ? "text-[var(--grn)]" : p.exists ? "text-ink-300" : "text-ink-600",
              )}
            >
              <button
                type="button"
                disabled={p.active || !p.exists || sw.isPending}
                onClick={() => sw.mutate(p.home)}
                className="flex min-w-0 flex-1 items-center gap-2 text-left disabled:cursor-default"
                title={p.active ? p.home : `open ${p.home}`}
                data-testid={`project-row-${p.name}`}
              >
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ background: p.active ? "var(--grn)" : p.exists ? "var(--tx-4)" : "var(--red)" }}
                />
                <span className="font-mono truncate">{p.name}</span>
                <span className="ml-auto max-w-[150px] truncate text-[9.5px] text-ink-500">
                  {p.exists ? p.home.replace(/\/\.mini-ork$/, "") : "missing state.db"}
                </span>
              </button>
              {!p.active && (
                <button
                  type="button"
                  onClick={() => rm.mutate(p.home)}
                  className="invisible shrink-0 text-ink-600 hover:text-[var(--red)] group-hover:visible"
                  title="remove from list (folder is untouched)"
                >
                  <X size={11} />
                </button>
              )}
            </div>
          ))}
          <AddProjectForm
            onAdd={() => qc.invalidateQueries({ queryKey: ["projects"] })}
            onOpen={(home) => sw.mutate(home)}
            switching={sw.isPending}
            switchError={sw.isError ? (sw.error as Error).message : null}
          />
        </div>
      )}
    </div>
  );
}

function AddProjectForm({
  onAdd,
  onOpen,
  switching,
  switchError,
}: {
  onAdd: () => void;
  onOpen: (home: string) => void;
  switching: boolean;
  switchError: string | null;
}) {
  const qc = useQueryClient();
  const [path, setPath] = useState("");
  const [debounced, setDebounced] = useState("");
  const [added, setAdded] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const [browsePath, setBrowsePath] = useState<string | undefined>(undefined);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(path.trim()), 350);
    return () => clearTimeout(t);
  }, [path]);

  const { data: check, isFetching: checking } = useQuery({
    queryKey: ["project-validate", debounced],
    queryFn: () => api.validateProject(debounced),
    enabled: debounced.length > 0,
    staleTime: 5_000,
  });

  const {
    data: tree,
    error: browseError,
    isError: browseFailed,
    isFetching: browseLoading,
    refetch: retryBrowse,
  } = useQuery({
    queryKey: ["project-browse", browsePath ?? "~"],
    queryFn: () => api.browseDirs(browsePath),
    enabled: browsing,
    staleTime: 10_000,
  });

  const add = useMutation({
    mutationFn: (home: string) => api.addProject(home),
    onSuccess: (res) => {
      setAdded(res.project.name);
      qc.invalidateQueries({ queryKey: ["project-validate"] });
      onAdd();
    },
  });

  const pick = (p: string) => {
    setPath(p);
    setDebounced(p);
    setAdded(null);
    setBrowsing(false);
  };

  const valid = !!check?.ok && debounced.length > 0;
  const showCheck = debounced.length > 0 && !checking;

  return (
    <form
      className="border-t border-[var(--hair)] p-2 space-y-1.5"
      data-testid="add-project-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && check?.home) onOpen(check.home);
      }}
    >
      <div className="text-[9.5px] uppercase tracking-[0.18em] text-ink-500">Add project</div>
      <div className="flex gap-1.5">
        <input
          value={path}
          onChange={(e) => {
            setPath(e.target.value);
            setAdded(null);
          }}
          placeholder="~/ps/researcher  — or browse →"
          className={clsx(
            "h-7 min-w-0 flex-1 rounded-[3px] border bg-[var(--bg)] px-2 text-[10.5px] font-mono text-ink-200 outline-none placeholder:text-ink-600",
            showCheck && valid && "border-[var(--grn)]",
            showCheck && !valid && "border-[var(--red)]",
            (!showCheck || checking) && "border-[var(--hair-2)] focus:border-[var(--cyan)]",
          )}
          data-testid="add-project-input"
        />
        <button
          type="button"
          onClick={() => setBrowsing((v) => !v)}
          aria-controls="folder-browser"
          aria-expanded={browsing}
          aria-label="Browse folders"
          className={clsx(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-[3px] border text-ink-400 hover:border-[var(--cyan)]",
            browsing ? "border-[var(--cyan)] text-[var(--cyan)]" : "border-[var(--hair-2)]",
          )}
          title="browse folders"
          data-testid="browse-folders"
        >
          <FolderOpen size={13} />
        </button>
      </div>
      {browsing && (
        <div
          id="folder-browser"
          className="rounded-[3px] border border-[var(--hair-2)]"
          data-testid="folder-browser"
          aria-busy={browseLoading}
        >
          {tree && (
            <>
              <div className="flex items-center gap-1 border-b border-[var(--hair)] px-2 py-1">
                <button
                  type="button"
                  disabled={!tree.parent || browseLoading}
                  onClick={() => setBrowsePath(tree.parent ?? undefined)}
                  className="kbd disabled:opacity-30"
                  title="up one level"
                >
                  ..
                </button>
                <span className="min-w-0 flex-1 truncate font-mono text-[9.5px] text-ink-500" title={tree.path}>
                  {tree.path}
                </span>
                {tree.is_project && (
                  <button
                    type="button"
                    onClick={() => pick(tree.path)}
                    className="shrink-0 rounded-[3px] border border-[var(--grn)] px-1.5 text-[9.5px] uppercase text-[var(--grn)]"
                  >
                    use this
                  </button>
                )}
              </div>
              <div className="max-h-[180px] overflow-auto thin">
                {tree.dirs.map((d) => (
                  <div key={d.path} className="group flex items-center">
                    <button
                      type="button"
                      disabled={browseLoading}
                      onClick={() => setBrowsePath(d.path)}
                      className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1 text-left text-[10.5px] hover:bg-white/[0.035] disabled:opacity-50"
                    >
                      <FolderOpen size={11} className={d.is_project ? "text-[var(--grn)]" : "text-ink-600"} />
                      <span className={clsx("font-mono truncate", d.is_project ? "text-[var(--grn)]" : "text-ink-300")}>
                        {d.name}
                      </span>
                    </button>
                    {d.is_project && (
                      <button
                        type="button"
                        onClick={() => pick(d.path)}
                        className="invisible mr-2 shrink-0 rounded-[3px] border border-[var(--grn)] px-1.5 text-[9.5px] uppercase text-[var(--grn)] group-hover:visible"
                        data-testid={`pick-${d.name}`}
                      >
                        select
                      </button>
                    )}
                  </div>
                ))}
                {!tree.dirs.length && <div className="px-3 py-2 text-[10px] text-ink-600">no subfolders</div>}
              </div>
            </>
          )}
          {!tree && browseLoading && (
            <div className="px-3 py-2 text-[10px] text-ink-500" data-testid="folder-browser-loading">
              loading folders…
            </div>
          )}
          {browseFailed && !browseLoading && (
            <div className="space-y-1.5 px-3 py-2 text-[10px]" role="alert" data-testid="folder-browser-error">
              <div className="text-[var(--red)]">Could not load folders.</div>
              <div className="break-words font-mono text-[9.5px] text-ink-500">{(browseError as Error).message}</div>
              <div className="flex items-center gap-2 text-ink-500">
                <span>In development, make sure the API is running on :7090.</span>
                <button
                  type="button"
                  onClick={() => void retryBrowse()}
                  className="shrink-0 rounded-[3px] border border-[var(--hair-2)] px-1.5 py-0.5 uppercase text-ink-300 hover:border-[var(--cyan)]"
                  data-testid="folder-browser-retry"
                >
                  retry
                </button>
              </div>
            </div>
          )}
        </div>
      )}
      <div className="min-h-[14px] text-[10px]" data-testid="add-project-feedback">
        {checking && <span className="text-ink-500">checking…</span>}
        {showCheck && valid && (
          <span className="text-[var(--grn)]">
            ✓ {check?.name} — {check?.home}
            {check?.registered ? " (already in list)" : ""}
          </span>
        )}
        {showCheck && !valid && <span className="text-[var(--red)]">✗ {check?.error ?? "not a mini-ork folder"}</span>}
        {!debounced && added && <span className="text-[var(--grn)]">✓ added {added}</span>}
        {switchError && <span className="text-[var(--red)]"> {switchError}</span>}
      </div>
      <div className="flex gap-1.5">
        <button
          type="submit"
          disabled={!valid || switching}
          className="h-6 flex-1 rounded-[3px] border border-[var(--hair-2)] bg-white/[0.04] text-[10.5px] font-bold uppercase tracking-[0.08em] text-ink-200 hover:border-[var(--cyan)] disabled:opacity-40 disabled:hover:border-[var(--hair-2)]"
          data-testid="add-open-project"
        >
          {switching ? "opening…" : "Add + open"}
        </button>
        <button
          type="button"
          disabled={!valid || !!check?.registered || add.isPending}
          onClick={() => check?.home && add.mutate(check.home)}
          className="h-6 flex-1 rounded-[3px] border border-[var(--hair-2)] text-[10.5px] uppercase tracking-[0.08em] text-ink-400 hover:border-[var(--cyan)] disabled:opacity-40 disabled:hover:border-[var(--hair-2)]"
          data-testid="add-project"
        >
          Add to list
        </button>
      </div>
    </form>
  );
}

function RailFleet({ runs, activePath, activeCount }: { runs: TaskRun[]; activePath: string; activeCount: number }) {
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
        <span className="ml-auto pill-ok">{activeCount}</span>
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
  if (pathname.startsWith("/new")) return "dispatch";
  if (pathname.startsWith("/recipes")) return "catalog";
  if (pathname.startsWith("/trajectory")) return "trajectory";
  if (pathname.startsWith("/fingerprint")) return "fingerprint";
  return "fleet";
}
