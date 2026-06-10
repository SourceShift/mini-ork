import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import type React from "react";
import { useState } from "react";
import {
  ArrowLeft,
  Bot,
  BrainCircuit,
  Clock,
  Database,
  FileInput,
  GitBranch,
  Lightbulb,
  Route,
  ShieldCheck,
  ScrollText,
} from "lucide-react";

import { api, type AgentSummary, type ArtifactEntry, type DagNode, type Diagnostic, type LearningAttribution, type LlmCall, type RunEvent, type RunInput, type RunLearning, type TaskRun } from "@/lib/api";
import { formatCost, formatDuration, formatRelative, formatTokens } from "@/lib/format";
import { FamilyPill, StatusPill, VerdictPill } from "@/components/Pill";
import { RunDag } from "@/components/RunDag";
import { ArtifactViewer, artifactLabel, isDeliverableArtifact } from "@/components/ArtifactViewer";
import { WhyCard } from "@/components/WhyCard";
import { RunControls } from "@/components/RunControls";
import { NeedsAnswersPanel } from "@/components/NeedsAnswersPanel";
import { RunInputModal } from "@/components/RunInputModal";
import { useEventStream } from "@/lib/sse";

export function RunDetailPage() {
  const { taskRunId } = useParams({ from: "/runs/$taskRunId" });
  const navigate = useNavigate({ from: "/runs/$taskRunId" });
  const search = useSearch({ from: "/runs/$taskRunId" });
  const qc = useQueryClient();
  const [selectedInput, setSelectedInput] = useState<RunInput | null>(null);
  // URL is the source of truth for the active tab — deep links and the
  // back button both work without a state-sync effect.
  const activeTab: RunTab = isRunTab(search.tab) ? search.tab : "agents";
  const changeTab = (tab: RunTab) =>
    navigate({ search: (prev) => ({ ...prev, tab }), replace: true });

  const tr = useQuery({ queryKey: ["task-run", taskRunId], queryFn: () => api.taskRun(taskRunId) });
  const events = useQuery({
    queryKey: ["events", taskRunId],
    queryFn: () => api.events(taskRunId),
    refetchInterval: 5_000,
  });
  const llm = useQuery({
    queryKey: ["llm", taskRunId],
    queryFn: () => api.llmCalls(taskRunId),
    refetchInterval: 5_000,
  });
  const dag = useQuery({
    queryKey: ["dag", taskRunId],
    queryFn: () => api.dag(taskRunId),
    refetchInterval: 5_000,
  });
  const agents = useQuery({
    queryKey: ["agents", taskRunId],
    queryFn: () => api.agents(taskRunId),
    refetchInterval: 5_000,
  });
  const inputs = useQuery({
    queryKey: ["inputs", taskRunId],
    queryFn: () => api.inputs(taskRunId),
    refetchInterval: 5_000,
  });
  // Same queryKey as ArtifactViewer — react-query dedupes the request.
  const artifacts = useQuery({
    queryKey: ["artifacts", taskRunId],
    queryFn: () => api.artifacts(taskRunId),
    refetchInterval: 5_000,
  });
  const learning = useQuery({
    queryKey: ["learning", taskRunId],
    queryFn: () => api.learning(taskRunId),
    refetchInterval: 10_000,
  });
  const correlation = useQuery({
    queryKey: ["correlation", taskRunId],
    queryFn: () => api.correlation(taskRunId),
  });
  // Shares queryKey with WhyCard — react-query dedupes, no extra request.
  const why = useQuery({
    queryKey: ["why", taskRunId],
    queryFn: () => api.why(taskRunId),
    refetchInterval: 10_000,
  });

  useEventStream(`/api/v1/stream?task_run=${encodeURIComponent(taskRunId)}`, (name) => {
    if (name === "event") {
      for (const key of ["events", "task-run", "llm", "artifacts", "agents", "dag", "correlation", "why", "inputs", "learning"]) {
        qc.invalidateQueries({ queryKey: [key, taskRunId] });
      }
    }
  });

  if (tr.error) {
    return (
      <div className="p-6">
        <p className="text-red-300">task_run not found: {taskRunId}</p>
        <Link to="/" className="text-ork-amber text-sm">← back to fleet</Link>
      </div>
    );
  }

  const run = tr.data;
  const dagNodes = dag.data?.nodes ?? [];
  const agentRows = agents.data?.agents ?? [];
  const agentByFile: Record<string, string> = Object.fromEntries(
    agentRows.flatMap((a) => a.artifact_files.map((f) => [f, a.node_id])),
  );
  const selectedArtifact = typeof search.artifact === "string" ? search.artifact : null;
  const openArtifact = (relpath: string) =>
    navigate({ search: (prev) => ({ ...prev, tab: "artifacts", artifact: relpath }) });

  return (
    <div className="p-3 space-y-3 max-w-[1500px] mx-auto" data-testid="run-detail-page" data-run-id={taskRunId}>
      <div className="flex items-center gap-3 text-[11px]" data-testid="run-detail-breadcrumb">
        <Link to="/" className="text-ink-500 hover:text-ink-100 uppercase tracking-[0.08em] flex items-center gap-1">
          <ArrowLeft size={14} /> fleet
        </Link>
        <span className="text-ink-600">/</span>
        <code className="text-ink-100">{taskRunId}</code>
        <div className="ml-auto">
          <RunControls taskRunId={taskRunId} status={run?.status} />
        </div>
      </div>

      {run && (
        <header className="card" data-testid="run-overview">
          <div className="panel-title">Run Forensics</div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="h-8 w-1 rounded-sm" style={{ background: run.status === "failed" ? "var(--red)" : "var(--grn)" }} />
            <h1 className="text-[21px] font-black uppercase tracking-[0.06em] text-ink-100">{run.recipe ?? run.task_class}</h1>
            <StatusPill status={run.status} />
            {run.stale && (
              <span
                className="px-1.5 py-0.5 rounded-sm text-[10px] font-bold uppercase tracking-[0.08em]"
                style={{ background: "rgba(255,170,0,0.12)", color: "var(--amber, #d6a000)" }}
                title="No status updates or run events past the staleness cutoff — the engine likely exited without finalizing this run."
                data-testid="run-stale-pill"
              >
                stale
              </span>
            )}
            <VerdictPill verdict={run.verdict} />
            <code className="text-[10.5px] text-ink-500">{run.task_class}</code>
          </div>
          <div className="mt-3 grid grid-cols-2 md:grid-cols-5 gap-3">
            <Metric icon={<Clock size={14} />} label="created" value={formatRelative(run.created_at)} />
            <Metric icon={<Clock size={14} />} label="ended" value={run.ended_at ? formatRelative(run.ended_at) : run.stale ? "stale" : "running"} />
            <Metric icon={<Clock size={14} />} label="duration" value={runDuration(run)} />
            <Metric icon={<ScrollText size={14} />} label="cost" value={formatCost(run.cost_usd)} />
            <Metric icon={<Bot size={14} />} label="llm calls" value={String(llm.data?.length ?? 0)} />
          </div>
        </header>
      )}

      <RunTabs active={activeTab} onChange={changeTab} agentCount={agentRows.length} learningCount={learningCount(learning.data ?? null)} />

      {activeTab === "overview" && (
        <>
          <NeedsAnswersPanel taskRunId={taskRunId} />
          <PipelineCard
            nodes={dagNodes}
            taskRunId={taskRunId}
            run={run ?? null}
            events={events.data ?? []}
            why={why.data}
          />
          <RunInputChips inputs={inputs.data ?? []} onSelect={setSelectedInput} />
          <RunArtifactChips artifacts={artifacts.data ?? []} agentByFile={agentByFile} onOpen={openArtifact} />
          {hasFailureSignal(why.data, run?.status) && <WhyCard taskRunId={taskRunId} />}
        </>
      )}

      {activeTab === "learnings" && <RunLearningPanel taskRunId={taskRunId} learning={learning.data ?? null} />}

      {activeTab === "agents" && (
        <AgentsTabView
          dag={dag.data ?? null}
          taskRunId={taskRunId}
          agents={agentRows}
          onOpenArtifact={openArtifact}
        />
      )}

      {activeTab === "artifacts" && (
        <section className="space-y-2" data-testid="run-output-section">
          <SectionTitle icon={<ScrollText size={16} />} title="Output artifacts" subtitle="Telemetry sidecars are hidden from this deliverable view." />
          <ArtifactViewer taskRunId={taskRunId} initialSelection={selectedArtifact} agentByFile={agentByFile} />
        </section>
      )}

      {activeTab === "diagnostics" && (
        <>
          <CorrelationStrip
            traceId={run?.trace_id ?? null}
            events={events.data ?? []}
            llmCalls={llm.data ?? []}
            methods={correlation.data?.bridge_methods ?? []}
          />
          <section className="grid grid-cols-1 xl:grid-cols-[1fr_380px] gap-3">
            <WhyCard taskRunId={taskRunId} />
            <DiagnosticsCard events={events.data ?? []} llmCalls={llm.data ?? []} />
          </section>
        </>
      )}
      <RunInputModal taskRunId={taskRunId} input={selectedInput} onClose={() => setSelectedInput(null)} />
    </div>
  );
}

function AgentsTabView({
  dag,
  taskRunId,
  agents,
  onOpenArtifact,
}: {
  dag: ({ nodes: DagNode[]; edges: any[]; families_used?: Record<string, number> } & Record<string, any>) | null;
  taskRunId: string;
  agents: AgentSummary[];
  onOpenArtifact: (relpath: string) => void;
}) {
  const totalCost = agents.reduce((sum, agent) => sum + (agent.llm_cost_usd || 0), 0);
  const totalTokens = agents.reduce((sum, agent) => sum + (agent.llm_total_tokens || 0), 0);
  return (
    <section className="space-y-2" data-testid="run-agents-section">
      {dag && (
        <CoalitionPanel
          dag={dag}
          stats={`Σ ${formatCost(totalCost)} · ${formatTokens(totalTokens)} tok · ${agents.length} agents`}
        />
      )}

      {dag && (
        <div className="card !p-0 overflow-hidden">
          <div className="panel-title !m-0">Recipe DAG</div>
          <RunDag nodes={dag.nodes} edges={dag.edges} taskRunId={taskRunId} />
        </div>
      )}

      <div>
        <SectionTitle icon={<GitBranch size={16} />} title="Agent roster" subtitle="Sorted by recipe node with run/cost/timing attribution." />
        <AgentRunGrid taskRunId={taskRunId} agents={agents} onOpenArtifact={onOpenArtifact} />
      </div>
    </section>
  );
}

function CoalitionPanel({
  dag,
  stats,
}: {
  dag: { nodes: DagNode[]; recipe?: string; coalition?: string; families_used?: Record<string, number>; dominant_family?: string | null; dominant_share?: number };
  stats?: string;
}) {
  const families = dag.families_used ?? {};
  const familyCount = Object.keys(families).length;
  const coalition = dag.coalition ?? (familyCount >= 4 ? "heterogeneous" : familyCount > 1 ? "low" : "high");
  const ok = coalition === "heterogeneous" || coalition === "low";
  return (
    <div className="card !p-0 overflow-hidden" data-testid="run-coalition-panel" data-coalition={coalition}>
      <div className="panel-title !m-0">Coalition</div>
      <div className="flex flex-wrap items-center justify-between gap-3 px-3 py-3">
        <div className="flex items-center gap-2">
          <ShieldCheck size={16} style={{ color: ok ? "var(--grn)" : "var(--amb)" }} />
          <span className="font-black uppercase tracking-[0.08em]" style={{ color: ok ? "var(--grn)" : "var(--amb)" }}>
            {coalition}
          </span>
          <span className="font-mono text-[10.5px] text-ink-500">
            {familyCount} families · {dag.nodes.length} nodes{stats ? ` · ${stats}` : ""}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {Object.entries(families).map(([family, count]) => (
            <span key={family} className="flex items-center gap-1">
              <FamilyPill family={family} />
              <span className="font-mono text-[10px] text-ink-500">×{count}</span>
            </span>
          ))}
          {!Object.keys(families).length && <span className="text-[11px] text-ink-500">No family attribution.</span>}
        </div>
      </div>
    </div>
  );
}

const RUN_TABS = ["overview", "agents", "learnings", "artifacts", "diagnostics"] as const;
type RunTab = (typeof RUN_TABS)[number];

function isRunTab(v: unknown): v is RunTab {
  return typeof v === "string" && (RUN_TABS as readonly string[]).includes(v);
}

function RunTabs({
  active,
  onChange,
  agentCount,
  learningCount,
}: {
  active: RunTab;
  onChange: (tab: RunTab) => void;
  agentCount: number;
  learningCount: number;
}) {
  const tabs: Array<{ id: RunTab; label: string; badge?: number }> = [
    { id: "overview", label: "overview" },
    { id: "agents", label: "agents", badge: agentCount },
    { id: "learnings", label: "learnings", badge: learningCount },
    { id: "artifacts", label: "artifacts" },
    { id: "diagnostics", label: "diagnostics" },
  ];
  return (
    <div className="card !p-0 overflow-hidden" data-testid="run-tabs">
      <div className="flex flex-wrap items-center gap-0 border-b border-[var(--hair)] bg-[var(--panel)]">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            data-active={active === tab.id}
            className="border-b-2 px-3 py-2 text-[10.5px] font-black uppercase tracking-[0.1em] text-ink-500 hover:text-ink-100 data-[active=true]:border-[var(--red)] data-[active=true]:text-ink-100 data-[active=false]:border-transparent"
          >
            {tab.label}
            {tab.badge != null && <span className="ml-1 text-[var(--amb)]">{tab.badge}</span>}
          </button>
        ))}
      </div>
    </div>
  );
}

function learningCount(learning: RunLearning | null): number {
  if (!learning) return 0;
  return (
    learning.summary.gradients_produced +
    learning.summary.patterns_evidenced +
    learning.summary.learning_records
  );
}

function RunLearningPanel({ taskRunId, learning }: { taskRunId: string; learning: RunLearning | null }) {
  const gradients = learning?.produced.gradients ?? [];
  const patterns = learning?.produced.patterns ?? [];
  const records = learning?.self_improve.records ?? [];
  const priorRuns = learning?.injected_candidates.prior_similar_runs ?? [];
  const failureModes = learning?.injected_candidates.known_failure_modes ?? [];
  const injectionPoints = learning?.injected_candidates.injection_points ?? [];
  const hasLearning = gradients.length || patterns.length || records.length || priorRuns.length || failureModes.length;

  return (
    <section className="space-y-3" data-testid="run-learning-section">
      <SectionTitle
        icon={<BrainCircuit size={16} />}
        title="Persistent learning"
        subtitle="What this run learned, which agent it came from, and where memory is injected later."
      />
      <div className="grid grid-cols-1 xl:grid-cols-[1.15fr_0.85fr] gap-5">
        <div className="card min-w-0" data-testid="run-learning-produced">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
            <LearningCount label="gradients" value={learning?.summary.gradients_produced ?? 0} />
            <LearningCount label="patterns" value={learning?.summary.patterns_evidenced ?? 0} />
            <LearningCount label="records" value={learning?.summary.learning_records ?? 0} />
            <LearningCount label="prior runs" value={learning?.summary.prior_similar_runs_available ?? 0} />
            <LearningCount label="failure modes" value={learning?.summary.known_failure_modes_available ?? 0} />
          </div>

          <div className="mt-4 space-y-4">
            <LearningList
              taskRunId={taskRunId}
              title="Gradients produced by this run"
              empty="No gradient_records point directly at this run's trace yet."
              items={gradients.map((g) => ({
                id: g.gradient_id,
                primary: g.signal,
                secondary: g.suggested_change,
                meta: `${g.target} · confidence ${Math.round((g.confidence ?? 0) * 100)}%`,
                attribution: g.agent_attribution,
              }))}
            />
            <LearningList
              taskRunId={taskRunId}
              title="Patterns this run contributed evidence to"
              empty="No pattern_records cite this run's trace yet."
              items={patterns.map((p) => ({
                id: p.pattern_id,
                primary: p.description,
                secondary: `${p.output_type} · ${p.status}`,
                meta: `frequency ${p.frequency} · evidence ${p.evidence_trace_ids.length}`,
                attribution: p.agent_attribution,
              }))}
            />
            <LearningList
              taskRunId={taskRunId}
              title="Self-improve learning records"
              empty="No learning_record rows are attached to this run."
              items={records.map((r) => ({
                id: String(r.id),
                primary: r.title,
                secondary: r.patch_summary || `${r.category} · ${r.outcome}`,
                meta: `${r.severity} severity · confidence ${Math.round((r.confidence ?? 0) * 100)}%`,
                attribution: r.agent_attribution,
              }))}
            />
            {!hasLearning && (
              <div className="rounded-md border border-ink-700 bg-ink-900/30 p-3 text-sm text-ink-400">
                No persisted learning is visible for this run yet. That usually means the run has not reached reflection/self-improve,
                or its trace was not bridged into `gradient_records`, `pattern_records`, or `learning_record`.
              </div>
            )}
          </div>
        </div>

        {/* min-w-0: without it the truncate (nowrap) rows below set a huge
            min-content width on this grid column, pushing the grid past the
            viewport and crushing the left card to a sliver. */}
        <div className="space-y-3 min-w-0">
          <div className="card" data-testid="run-learning-injection">
            <h3 className="text-sm font-semibold text-ink-100 flex items-center gap-2">
              <span className="text-ork-amber"><Database size={15} /></span>
              Injection points
            </h3>
            <div className="mt-3 space-y-2">
              {injectionPoints.map((point) => (
                <div key={point.name} className="rounded-md border border-ink-700 bg-ink-900/30 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-mono text-xs text-ink-100">{point.name}</div>
                    {point.wired === false ? (
                      <span className="shrink-0 rounded border border-ink-600 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-500">not wired</span>
                    ) : (
                      <span className="shrink-0 rounded border border-ork-green/40 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ork-green">live</span>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-ork-amber">{point.where}</div>
                  <p className="mt-1 text-xs text-ink-400 leading-relaxed">{point.how}</p>
                </div>
              ))}
              {!injectionPoints.length && <p className="text-sm text-ink-500">Injection metadata is not available.</p>}
            </div>
          </div>

          <div className="card" data-testid="run-learning-candidates">
            <h3 className="text-sm font-semibold text-ink-100 flex items-center gap-2">
              <span className="text-ork-amber"><Lightbulb size={15} /></span>
              Available memory for this class
            </h3>
            <div className="mt-3 space-y-3">
              <div>
                <div className="text-xs uppercase tracking-wide text-ink-500 mb-1">Prior similar runs</div>
                <ul className="space-y-1 text-xs">
                  {priorRuns.slice(0, 5).map((r) => (
                    <li key={r.trace_id} className="rounded border border-ink-700 bg-ink-900/25 px-2 py-1.5">
                      <code className="text-ink-200">{r.trace_id}</code>
                      <span className="ml-2 text-ink-500">{r.status} · {formatDuration(r.duration_ms)}</span>
                    </li>
                  ))}
                  {!priorRuns.length && <li className="text-ink-500">None available.</li>}
                </ul>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide text-ink-500 mb-1">Known failure modes</div>
                <ul className="space-y-1 text-xs">
                  {failureModes.slice(0, 5).map((g) => (
                    <li key={g.gradient_id} className="rounded border border-ink-700 bg-ink-900/25 px-2 py-1.5">
                      <div className="text-ink-200 truncate">{g.signal}</div>
                      <div className="text-ink-500 truncate">{g.suggested_change}</div>
                    </li>
                  ))}
                  {!failureModes.length && <li className="text-ink-500">None matched this task class.</li>}
                </ul>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function LearningCount({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-ink-700 bg-ink-900/30 p-3">
      <div className="text-[10px] uppercase tracking-wide text-ink-500">{label}</div>
      <div className="mt-1 text-xl font-semibold text-ink-100">{value}</div>
    </div>
  );
}

function LearningList({
  taskRunId,
  title,
  empty,
  items,
}: {
  taskRunId: string;
  title: string;
  empty: string;
  items: Array<{ id: string; primary: string; secondary: string; meta: string; attribution: LearningAttribution }>;
}) {
  return (
    <div>
      <h3 className="text-xs uppercase tracking-wide text-ink-500 mb-2">{title}</h3>
      <div className="space-y-2">
        {items.slice(0, 6).map((item) => (
          <div key={item.id} className="rounded-md border border-ink-700 bg-ink-900/30 p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm text-ink-100 truncate">{item.primary}</div>
                <p className="mt-1 text-xs text-ink-400 line-clamp-2">{item.secondary}</p>
                <div className="mt-1 text-[11px] text-ink-500 truncate">{item.meta}</div>
              </div>
              <AgentAttributionBadge taskRunId={taskRunId} attribution={item.attribution} />
            </div>
          </div>
        ))}
        {!items.length && <p className="text-sm text-ink-500">{empty}</p>}
      </div>
    </div>
  );
}

function AgentAttributionBadge({ taskRunId, attribution }: { taskRunId: string; attribution: LearningAttribution }) {
  const nodeId = attribution.node_id;
  const confidence = attribution.confidence || "none";
  const cls = confidence === "high" ? "pill-ok" : confidence === "medium" ? "pill-warn" : "pill-muted";
  if (nodeId) {
    return (
      <Link
        to="/runs/$taskRunId/agents/$nodeId"
        params={{ taskRunId, nodeId }}
        className={`${cls} shrink-0 max-w-[180px]`}
        title={`${attribution.source} · ${confidence}`}
      >
        <Bot size={12} />
        <span className="truncate">{nodeId}</span>
      </Link>
    );
  }
  const candidates = attribution.candidate_node_ids?.length ? attribution.candidate_node_ids.join(", ") : null;
  return (
    <span className={`${cls} shrink-0 max-w-[180px]`} title={`${attribution.source} · ${confidence}`}>
      <Bot size={12} />
      <span className="truncate">{candidates || attribution.agent_version_id || "unattributed"}</span>
    </span>
  );
}

function RunInputChips({
  inputs,
  onSelect,
}: {
  inputs: RunInput[];
  onSelect: (input: RunInput) => void;
}) {
  const ordered = [...inputs].sort((a, b) => inputRank(a.key) - inputRank(b.key));
  return (
    <section className="card flex flex-wrap items-center gap-2" data-testid="run-inputs-card">
      <span className="flex items-center gap-1.5 text-[9.5px] font-black uppercase tracking-[0.13em] text-ink-500">
        <FileInput size={13} className="text-[var(--grn)]" /> inputs
      </span>
      {ordered.length ? ordered.map((input) => (
        <button
          key={input.key}
          type="button"
          onClick={() => onSelect(input)}
          title={input.path}
          className="flex items-center gap-1.5 rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2 py-1 hover:border-ork-amber/60"
          data-testid={`run-input-link-${input.key}`}
        >
          <span className="text-[11px] text-ink-100">{input.label}</span>
          <span className="text-[9px] uppercase text-ink-500">{input.kind}</span>
        </button>
      )) : (
        <span className="text-sm text-ink-500">No input documents found yet.</span>
      )}
    </section>
  );
}

function RunArtifactChips({
  artifacts,
  agentByFile,
  onOpen,
}: {
  artifacts: ArtifactEntry[];
  agentByFile: Record<string, string>;
  onOpen: (relpath: string) => void;
}) {
  // Run-level deliverables only — agent-claimed files live in the roster table.
  const runLevel = artifacts.filter((a) => isDeliverableArtifact(a) && !agentByFile[a.relpath]);
  if (!runLevel.length) return null;
  return (
    <section className="card flex flex-wrap items-center gap-2" data-testid="run-artifact-chips">
      <span className="flex items-center gap-1.5 text-[9.5px] font-black uppercase tracking-[0.13em] text-ink-500">
        <ScrollText size={13} className="text-[var(--grn)]" /> artifacts
      </span>
      {runLevel.map((a) => (
        <button
          key={a.relpath}
          type="button"
          onClick={() => onOpen(a.relpath)}
          title={a.relpath}
          className="flex items-center gap-1.5 rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2 py-1 hover:border-ork-amber/60"
          data-testid={`run-artifact-chip-${a.relpath.replace(/[^a-z0-9]/gi, "-")}`}
        >
          <span className="text-[11px] text-ink-100">{artifactLabel(a.relpath)}</span>
          <span className="text-[9px] uppercase text-ink-500">{a.kind}</span>
        </button>
      ))}
    </section>
  );
}

function PipelineCard({
  nodes,
  taskRunId,
  run,
  events,
  why,
}: {
  nodes: DagNode[];
  taskRunId: string;
  run: TaskRun | null;
  events: RunEvent[];
  why: Diagnostic | undefined;
}) {
  const running = nodes.filter((n) => nodeTone(n) === "running" || nodeTone(n) === "startup");
  const lastEvent = events.length ? events[events.length - 1] : null;
  const tail = why?.execute_log?.tail ?? [];
  const isLive = run != null && run.ended_at == null && run.status !== "failed" && run.status !== "rolled_back";
  return (
    <section className="card" data-testid="run-pipeline-card">
      <SectionTitle
        icon={<Route size={16} />}
        title="Pipeline"
        subtitle="Recipe nodes in dispatch order — click a node for its forensics."
        compact
      />
      <div className="mt-3 flex flex-wrap items-center gap-y-2">
        {nodes.map((node, i) => (
          <span key={node.name} className="flex items-center">
            {i > 0 && <span className="mx-1.5 text-ink-600">→</span>}
            <PipelineNode node={node} taskRunId={taskRunId} />
          </span>
        ))}
        {!nodes.length && <p className="text-sm text-ink-500">No recipe DAG available for this run.</p>}
      </div>

      <div className="mt-4 grid grid-cols-1 xl:grid-cols-2 gap-3 border-t border-[var(--hair)] pt-3">
        <div className="min-w-0" data-testid="run-now-panel">
          <div className="text-[9.5px] font-black uppercase tracking-[0.13em] text-ink-500">now</div>
          <div className="mt-1.5 space-y-1">
            {running.length ? (
              running.map((n) => (
                <div key={n.name} className="flex items-center gap-2 text-[12px] text-ink-100">
                  <span className="h-2 w-2 rounded-full bg-ork-green animate-pulse shrink-0" />
                  <Link
                    to="/runs/$taskRunId/agents/$nodeId"
                    params={{ taskRunId, nodeId: n.name }}
                    className="font-mono hover:text-[var(--amb)]"
                  >
                    {n.name}
                  </Link>
                  <span className="text-ink-500">
                    {n.type}{n.lane ? ` · ${n.lane}` : ""}
                    {n.started_at != null ? ` · started ${formatRelative(n.started_at)}` : ""}
                  </span>
                </div>
              ))
            ) : (
              <div className="text-[12px] text-ink-300">
                {run?.ended_at
                  ? `run ${run.status}${run.verdict ? ` — verdict ${run.verdict}` : ""}`
                  : "no node is dispatching right now"}
              </div>
            )}
            {lastEvent && (
              <div className="text-[11px] text-ink-500">
                last event: <code className="text-ink-300">{lastEvent.event_type}</code> · {formatRelative(lastEvent.ts)}
              </div>
            )}
          </div>
        </div>
        {isLive && tail.length > 0 && (
          <div className="min-w-0" data-testid="run-log-tail">
            <div className="text-[9.5px] font-black uppercase tracking-[0.13em] text-ink-500">execute.log tail</div>
            <pre className="mt-1.5 text-[10px] text-ink-300 bg-ink-900/60 p-2 rounded overflow-hidden whitespace-pre-wrap">
              {tail.slice(-3).join("\n")}
            </pre>
          </div>
        )}
      </div>
    </section>
  );
}

function PipelineNode({ node, taskRunId }: { node: DagNode; taskRunId: string }) {
  const tone = nodeTone(node);
  const glyph = tone === "succeeded" ? "✓" : tone === "failed" ? "✗" : tone === "queued" ? "○" : "●";
  const detail =
    tone === "running" || tone === "startup"
      ? node.started_at != null
        ? formatRelative(node.started_at)
        : null
      : node.duration_ms
        ? formatDuration(node.duration_ms)
        : null;
  return (
    <Link
      to="/runs/$taskRunId/agents/$nodeId"
      params={{ taskRunId, nodeId: node.name }}
      className="flex items-center gap-1.5 rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2 py-1 hover:border-ork-amber/60"
      data-testid={`pipeline-node-${node.name}`}
      data-status={tone}
      title={`${node.type}${node.lane ? ` · lane ${node.lane}` : ""}${node.verdict ? ` · ${node.verdict}` : ""}`}
    >
      <span
        className={tone === "running" ? "animate-pulse" : undefined}
        style={{ color: toneColor(tone, node.family) }}
      >
        {glyph}
      </span>
      <span className="font-mono text-[11px] text-ink-100">{node.name}</span>
      {detail && <span className="text-[9.5px] text-ink-500">{detail}</span>}
    </Link>
  );
}

function hasFailureSignal(d: Diagnostic | undefined, status?: string): boolean {
  if (status === "failed" || status === "rolled_back") return true;
  if (!d) return false;
  return (
    d.verifier_results.some((v) => !v.pass) ||
    d.evidence_refs.length > 0 ||
    (d.execute_log?.failure_lines?.length ?? 0) > 0
  );
}

function runDuration(run: TaskRun): string {
  if (run.ended_at) return formatDuration(run.duration_ms);
  if (run.stale) {
    const last = run.last_activity_at ?? run.updated_at ?? run.created_at;
    return formatDuration(Math.max(0, (last - run.created_at) * 1000));
  }
  return formatDuration(Math.max(0, Date.now() - run.created_at * 1000));
}

function AgentRunGrid({
  taskRunId,
  agents,
  onOpenArtifact,
}: {
  taskRunId: string;
  agents: AgentSummary[];
  onOpenArtifact: (relpath: string) => void;
}) {
  if (!agents.length) {
    return <p className="card text-sm text-ink-500">No recipe agents found for this run.</p>;
  }
  return (
    <div className="card !p-0 overflow-hidden">
      <table className="tbl">
        <thead>
          <tr>
            <th className="w-1"></th>
            <th>node</th>
            <th>family</th>
            <th>lane</th>
            <th>status</th>
            <th>verdict</th>
            <th className="text-right">cost</th>
            <th className="text-right">tok</th>
            <th className="text-right">dur</th>
            <th className="text-right">calls</th>
            <th>gates</th>
            <th>artifacts</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((agent) => {
            const tone = nodeTone({ name: agent.node_id, type: agent.node_type, status: agent.status } as DagNode);
            return (
              <tr key={agent.node_id} data-testid={`agent-card-${agent.node_id}`} data-status={agent.status}>
                <td className="!p-0">
                  <span className="block h-7 w-[3px]" style={{ background: toneColor(tone, agent.family) }} />
                </td>
                <td>
                  <Link
                    to="/runs/$taskRunId/agents/$nodeId"
                    params={{ taskRunId, nodeId: agent.node_id }}
                    className="font-mono text-[11.5px] text-ink-100 hover:text-[var(--amb)]"
                  >
                    {agent.node_id}
                  </Link>
                </td>
                <td><FamilyPill family={agent.family} /></td>
                <td className="font-mono text-[10.5px] text-ink-500">{agent.model_lane ?? "—"}</td>
                <td><StatusPill status={agent.status} /></td>
                <td><VerdictPill verdict={agent.verdict} /></td>
                <td className="text-right font-mono text-[10.5px]">{formatCost(agent.llm_cost_usd)}</td>
                <td className="text-right font-mono text-[10.5px]">{formatTokens(agent.llm_total_tokens)}</td>
                <td className="text-right font-mono text-[10.5px]">{formatDuration(agent.duration_ms)}</td>
                <td className="text-right font-mono text-[10.5px]">{agent.llm_call_count}</td>
                <td className="font-mono text-[10px] text-ink-500">{agent.gates.length ? agent.gates.join(", ") : "—"}</td>
                <td className="font-mono text-[10px]">
                  {agent.artifact_files.length ? (
                    agent.artifact_files.map((f) => (
                      <button
                        key={f}
                        type="button"
                        onClick={() => onOpenArtifact(f)}
                        title={f}
                        className="block max-w-[180px] truncate text-left text-[var(--amb)] hover:underline"
                        data-testid={`agent-artifact-link-${agent.node_id}-${f.replace(/[^a-z0-9]/gi, "-")}`}
                      >
                        {f}
                      </button>
                    ))
                  ) : (
                    <span className="text-ink-500">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CorrelationStrip({
  traceId,
  events,
  llmCalls,
  methods,
}: {
  traceId: string | null;
  events: RunEvent[];
  llmCalls: LlmCall[];
  methods: string[];
}) {
  return (
    <section className="card grid grid-cols-2 lg:grid-cols-4 gap-3 content-start" data-testid="run-correlation-strip">
      <Metric icon={<Route size={14} />} label="trace" value={traceId ?? "—"} code />
      <Metric icon={<GitBranch size={14} />} label="events" value={String(events.length)} />
      <Metric icon={<Bot size={14} />} label="llm calls" value={String(llmCalls.length)} />
      <Metric icon={<ScrollText size={14} />} label="bridges" value={methods.length ? methods.join(", ") : "—"} code />
    </section>
  );
}

function DiagnosticsCard({ events, llmCalls }: { events: RunEvent[]; llmCalls: LlmCall[] }) {
  return (
    <section className="card" data-testid="run-diagnostics-card">
      <h3 className="text-sm font-semibold text-ink-200 mb-3">Diagnostics</h3>
      <div className="space-y-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-ink-500 mb-1">Recent events</div>
          <ul className="space-y-1 text-xs">
            {events.slice(-6).reverse().map((event) => (
              <li key={`${event.source}-${event.id}`} className="flex gap-2 text-ink-300">
                <code className="text-ink-500">{formatRelative(event.ts)}</code>
                <span>{event.event_type}</span>
              </li>
            ))}
            {!events.length && <li className="text-ink-500">No events recorded.</li>}
          </ul>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-ink-500 mb-1">LLM ledger</div>
          <p className="text-xs text-ink-400">
            {llmCalls.length} call{llmCalls.length === 1 ? "" : "s"}, total{" "}
            {formatCost(llmCalls.reduce((sum, call) => sum + (call.cost_usd ?? 0), 0))}
          </p>
        </div>
      </div>
    </section>
  );
}

function DagLegend() {
  return (
    <div className="flex flex-wrap gap-2 text-xs" data-testid="dag-legend">
      <LegendItem tone="startup" label="startup" />
      <LegendItem tone="running" label="running" />
      <LegendItem tone="succeeded" label="succeeded" />
      <LegendItem tone="failed" label="failed" />
      <LegendItem tone="queued" label="queued" />
    </div>
  );
}

function LegendItem({ tone, label }: { tone: NodeTone; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-ink-700 bg-ink-900/40 px-2 py-1">
      <span className={`h-2 w-2 rounded-full ${toneClass(tone)}`} />
      <span className="text-ink-300">{label}</span>
    </span>
  );
}

function SectionTitle({
  icon,
  title,
  subtitle,
  compact = false,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  compact?: boolean;
}) {
  return (
    <div className={compact ? "" : "flex items-end justify-between gap-3 border-b border-[var(--hair)] pb-1"}>
      <div>
        <h2 className="text-[10.5px] font-black uppercase tracking-[0.13em] text-ink-200 flex items-center gap-2">
          <span className="text-[var(--grn)]">{icon}</span>
          {title}
        </h2>
        {subtitle && <p className="text-[10.5px] text-ink-500 mt-1">{subtitle}</p>}
      </div>
    </div>
  );
}

function Metric({ icon, label, value, code = false }: { icon: React.ReactNode; label: string; value: string; code?: boolean }) {
  return (
    <div className="rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] p-3 min-w-0">
      <div className="flex items-center gap-1.5 text-[9.5px] uppercase tracking-[0.13em] text-ink-500">
        {icon} {label}
      </div>
      <div className={`${code ? "font-mono text-xs" : "text-sm"} text-ink-100 mt-1 truncate`} title={value}>
        {value}
      </div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[9.5px] uppercase tracking-[0.13em] text-ink-500">{label}</div>
      <div className="text-ink-200 truncate">{value}</div>
    </div>
  );
}

function NodeStateBadge({ node }: { node: DagNode }) {
  const tone = nodeTone(node);
  return <span className={`h-3 w-3 rounded-full ${toneClass(tone)}`} title={tone} />;
}

type NodeTone = "startup" | "running" | "succeeded" | "failed" | "queued";

function nodeTone(node: DagNode): NodeTone {
  if (node.status === "failed") return "failed";
  if (node.status === "done") return "succeeded";
  if (node.status === "running") return node.started_at ? "running" : "startup";
  return "queued";
}

function toneClass(tone: NodeTone): string {
  switch (tone) {
    case "startup":
      return "bg-ork-amber";
    case "running":
      return "bg-ork-green";
    case "succeeded":
      return "bg-purple-400";
    case "failed":
      return "bg-ork-red";
    case "queued":
      return "bg-ink-500";
  }
}

function toneColor(tone: NodeTone, family?: string | null): string {
  if (tone === "failed") return "var(--red)";
  if (tone === "running") return "var(--grn)";
  if (tone === "startup") return "var(--amb)";
  if (tone === "succeeded") return family ? familyColorForCss(family) : "var(--violet)";
  return "var(--tx-4)";
}

function familyColorForCss(family: string): string {
  return {
    opus: "#a394e8",
    sonnet: "#e0975f",
    glm: "#46c0b6",
    kimi: "#5f97e6",
    codex: "#d4b24f",
    gemini: "#74c463",
  }[family.toLowerCase()] ?? "var(--tx-4)";
}

function inputRank(key: string): number {
  return { kickoff: 0, plan: 1, workflow: 2, run_profile: 3, profile_answers: 4 }[key] ?? 10;
}
