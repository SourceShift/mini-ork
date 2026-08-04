import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowLeft,
  Bot,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileInput,
  FileText,
  GitBranch,
} from "lucide-react";

import { api, type ArtifactContent, type DagNode, type LearningAttribution, type RunLearning } from "@/lib/api";
import { formatCost, formatDuration, formatRelative, formatTokens } from "@/lib/format";
import { FamilyPill, StatusPill, VerdictPill } from "@/components/Pill";
import { AgentTranscriptPanel } from "@/components/AgentTranscript";

export function AgentDetailPage() {
  const { taskRunId, nodeId } = useParams({ from: "/runs/$taskRunId/agents/$nodeId" });
  const navigate = useNavigate({ from: "/runs/$taskRunId/agents/$nodeId" });
  const search = useSearch({ from: "/runs/$taskRunId/agents/$nodeId" });
  const activeTab: AgentTab = isAgentTab(search.tab) ? search.tab : "overview";
  const changeTab = (tab: AgentTab) =>
    navigate({ search: (prev) => ({ ...prev, tab }), replace: true });
  const q = useQuery({
    queryKey: ["agent", taskRunId, nodeId],
    queryFn: () => api.agent(taskRunId, nodeId),
    refetchInterval: 5_000,
  });
  const learning = useQuery({
    queryKey: ["learning", taskRunId],
    queryFn: () => api.learning(taskRunId),
    refetchInterval: 10_000,
  });

  if (q.error)
    return (
      <div className="p-6 space-y-2" data-testid="agent-detail-error">
        <Link to="/runs/$taskRunId" params={{ taskRunId }} className="text-ork-amber text-sm">
          ← back to run
        </Link>
        <p className="text-red-300 mt-3">{String(q.error)}</p>
      </div>
    );

  if (!q.data) return <div className="p-6 text-ink-400" data-testid="agent-detail-loading">loading…</div>;

  const d = q.data;
  const n = d.node;
  const llmCost = d.llm_calls.reduce((sum, c) => sum + (c.cost_usd ?? 0), 0);
  const llmTokens = d.llm_calls.reduce((sum, c) => sum + (c.total_tokens ?? 0), 0);
  const turnCount = d.transcript.available ? d.transcript.turns.length : d.llm_calls.length;
  const learningBadge = agentLearningCount(learning.data ?? null, nodeId);

  return (
    <div
      className="p-6 space-y-4 max-w-[1400px] mx-auto"
      data-testid="agent-detail-page"
      data-node-id={nodeId}
      data-status={d.status}
    >
      <div className="flex items-center gap-3 text-sm" data-testid="agent-breadcrumb">
        <Link to="/" className="text-ink-400 hover:text-ink-100" data-testid="agent-back-fleet">
          fleet
        </Link>
        <span className="text-ink-600">/</span>
        <Link
          to="/runs/$taskRunId"
          params={{ taskRunId }}
          className="text-ink-400 hover:text-ink-100 flex items-center gap-1"
          data-testid="agent-back-run"
        >
          <ArrowLeft size={14} /> {taskRunId}
        </Link>
        <span className="text-ink-600">/</span>
        <span className="text-ink-100 font-mono flex items-center gap-1.5">
          <Bot size={14} /> {nodeId}
        </span>
      </div>

      <header className="card flex items-start gap-4 flex-wrap" data-testid="agent-header">
        <div className="flex-1 min-w-0 space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-xl font-semibold">{n.name}</h1>
            <span className="pill-muted">{n.type}</span>
            <FamilyPill family={n.family} />
            <StatusPill status={d.status} />
            {d.verdict && <VerdictPill verdict={d.verdict} />}
            {n.dispatch_mode && (
              <span className="pill-muted" data-testid="agent-dispatch-mode">{n.dispatch_mode}</span>
            )}
          </div>
          <div className="text-xs text-ink-400 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2">
            <Cell label="lane">{n.lane ?? "—"}</Cell>
            <Cell label="duration">{formatDuration(d.duration_ms)}</Cell>
            <Cell label="started">{d.started_at ? formatRelative(d.started_at) : "—"}</Cell>
            <Cell label="ended">{d.ended_at ? formatRelative(d.ended_at) : "—"}</Cell>
            <Cell label="llm calls">{String(d.llm_calls.length)}</Cell>
            <Cell label="cost">{formatCost(llmCost)}</Cell>
            <Cell label="tokens">{formatTokens(llmTokens)}</Cell>
          </div>
        </div>
      </header>

      <AgentTabs
        active={activeTab}
        onChange={changeTab}
        turnCount={turnCount}
        artifactCount={d.artifacts.length}
        learningCount={learningBadge}
        childCount={d.children.length}
      />

      {activeTab === "overview" && <AgentInputPanel prompt={d.prompt} node={n} />}
      {activeTab === "turns" && (
        <AgentTranscriptPanel transcript={d.transcript} calls={d.llm_calls} running={d.status === "running"} />
      )}
      {activeTab === "artifacts" && <ArtifactsPanel artifacts={d.artifacts} />}
      {activeTab === "learnings" && <AgentLearningPanel nodeId={nodeId} learning={learning.data ?? null} />}
      {activeTab === "children" && <ChildSpawnsPanel children={d.children} />}
    </div>
  );
}

const AGENT_TABS = ["overview", "turns", "artifacts", "learnings", "children"] as const;
type AgentTab = (typeof AGENT_TABS)[number];

function isAgentTab(v: unknown): v is AgentTab {
  return typeof v === "string" && (AGENT_TABS as readonly string[]).includes(v);
}

function AgentTabs({
  active,
  onChange,
  turnCount,
  artifactCount,
  learningCount,
  childCount,
}: {
  active: AgentTab;
  onChange: (tab: AgentTab) => void;
  turnCount: number;
  artifactCount: number;
  learningCount: number;
  childCount: number;
}) {
  const tabs: Array<{ id: AgentTab; label: string; badge?: number }> = [
    { id: "overview", label: "overview" },
    { id: "turns", label: "turns", badge: turnCount },
    { id: "artifacts", label: "artifacts", badge: artifactCount },
    { id: "learnings", label: "learnings", badge: learningCount },
    { id: "children", label: "children", badge: childCount },
  ];
  return (
    <div className="card !p-0 overflow-hidden" data-testid="agent-tabs">
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

function agentLearningCount(learning: RunLearning | null, nodeId: string): number {
  if (!learning) return 0;
  const extracted = [
    ...learning.produced.gradients,
    ...learning.produced.patterns,
    ...learning.self_improve.records,
  ].filter((item) => attributionMatches(item.agent_attribution, nodeId)).length;
  const injected = learning.injected_candidates.known_failure_modes.filter(
    (item) => attributionMatches(item.agent_attribution, nodeId) || (item.target ?? "").includes(nodeId),
  ).length;
  return extracted + injected;
}

function AgentInputPanel({
  prompt,
  node,
}: {
  prompt: { path: string | null; content: string | null };
  node: DagNode;
}) {
  return (
    <section className="card" data-testid="agent-input-section">
      <h3 className="text-sm font-semibold text-ink-200 mb-3 flex items-center gap-1.5">
        <FileInput size={14} /> Agent input
      </h3>
      <div className="mb-3 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs text-ink-400" data-testid="agent-own-context">
        <Cell label="prompt ref"><code className="text-xs truncate block">{node.prompt_ref ?? "built-in handler"}</code></Cell>
        <Cell label="verifier"><code className="text-xs truncate block">{node.verifier_ref ?? "—"}</code></Cell>
        <Cell label="gates"><code className="text-xs">{node.gates?.length ? node.gates.join(", ") : "—"}</code></Cell>
        <Cell label="dispatch">{node.dispatch_mode ?? "—"}</Cell>
      </div>
      <PromptPanel prompt={prompt} />
    </section>
  );
}

function AgentLearningPanel({ nodeId, learning }: { nodeId: string; learning: RunLearning | null }) {
  const gradients = (learning?.produced.gradients ?? []).filter((item) => attributionMatches(item.agent_attribution, nodeId));
  const patterns = (learning?.produced.patterns ?? []).filter((item) => attributionMatches(item.agent_attribution, nodeId));
  const records = (learning?.self_improve.records ?? []).filter((item) => attributionMatches(item.agent_attribution, nodeId));
  const injected = (learning?.injected_candidates.known_failure_modes ?? []).filter((item) =>
    attributionMatches(item.agent_attribution, nodeId) || (item.target ?? "").includes(nodeId),
  );
  const createdCount = gradients.length + patterns.length + records.length;

  return (
    <section className="card" data-testid="agent-learning-section">
      <div className="panel-title">Agent learnings</div>
      <div className="space-y-3 min-w-0">
          <div className="grid grid-cols-2 gap-2">
            <LearningMetric label="extracted" value={createdCount} />
            <LearningMetric label="injected" value={injected.length} />
          </div>
          <AgentLearningList
            title="Extracted by this agent"
            empty="No persisted gradients, patterns, or learning_record rows are attributed to this agent yet."
            rows={[
              ...gradients.map((g) => ({
                id: g.gradient_id,
                kind: "gradient",
                primary: g.signal,
                secondary: g.suggested_change,
                meta: `${g.target} · ${Math.round((g.confidence ?? 0) * 100)}%`,
              })),
              ...patterns.map((p) => ({
                id: p.pattern_id,
                kind: "pattern",
                primary: p.description,
                secondary: `${p.output_type} · frequency ${p.frequency}`,
                meta: p.status,
              })),
              ...records.map((r) => ({
                id: String(r.id),
                kind: "record",
                primary: r.title,
                secondary: r.patch_summary || `${r.category} · ${r.outcome}`,
                meta: `${r.severity} · ${Math.round((r.confidence ?? 0) * 100)}%`,
              })),
            ]}
          />
          <AgentLearningList
            title="Injected into this agent (known failure modes)"
            empty="No known failure modes matched this agent. Run-level prior similar runs may still be injected at planner/context level."
            rows={injected.map((g) => ({
              id: g.gradient_id,
              kind: "failure-mode",
              primary: g.signal,
              secondary: g.suggested_change,
              meta: `${g.target} · ${Math.round((g.confidence ?? 0) * 100)}%`,
            }))}
          />
      </div>
    </section>
  );
}

function attributionMatches(attribution: LearningAttribution | undefined, nodeId: string): boolean {
  if (!attribution) return false;
  return attribution.node_id === nodeId || Boolean(attribution.candidate_node_ids?.includes(nodeId));
}

function LearningMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] p-3">
      <div className="text-[9.5px] uppercase tracking-[0.13em] text-ink-500">{label}</div>
      <div className="mt-1 font-mono text-[22px] font-black leading-none text-ink-100">{value}</div>
    </div>
  );
}

function AgentLearningList({
  title,
  empty,
  rows,
}: {
  title: string;
  empty: string;
  rows: Array<{ id: string; kind: string; primary: string; secondary: string; meta: string }>;
}) {
  return (
    <div>
      <h3 className="mb-2 text-[10.5px] font-black uppercase tracking-[0.13em] text-ink-500">{title}</h3>
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={`${row.kind}-${row.id}`} className="rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate text-[12px] text-ink-100">{row.primary}</div>
                <p className="mt-1 line-clamp-2 text-[11px] text-ink-400">{row.secondary}</p>
              </div>
              <span className="pill-muted shrink-0">{row.kind}</span>
            </div>
            <div className="mt-1 truncate font-mono text-[9.5px] text-ink-500">{row.meta}</div>
          </div>
        ))}
        {!rows.length && <p className="text-[12px] text-ink-500">{empty}</p>}
      </div>
    </div>
  );
}

function PromptPanel({ prompt }: { prompt: { path: string | null; content: string | null } }) {
  const [open, setOpen] = useState(true);
  if (!prompt.path) {
    return (
      <div className="rounded-md border border-ink-700 bg-ink-900/30 p-3" data-testid="agent-prompt-section">
        <h3 className="text-sm font-semibold text-ink-200 mb-2 flex items-center gap-1.5">
          <FileText size={14} /> Node prompt
        </h3>
        <p className="text-sm text-ink-500">No prompt_ref configured (this node uses a built-in handler).</p>
      </div>
    );
  }
  return (
    <div className="rounded-md border border-ink-700 bg-ink-900/30 p-3" data-testid="agent-prompt-section">
      <button
        onClick={() => setOpen((x) => !x)}
        className="w-full flex items-center justify-between hover:text-ink-100"
        data-testid="agent-prompt-toggle"
        data-open={open}
      >
        <h3 className="text-sm font-semibold text-ink-200 flex items-center gap-1.5">
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <FileText size={14} /> Node prompt
          <code className="text-xs text-ink-400 font-normal">{prompt.path.split("/").slice(-2).join("/")}</code>
        </h3>
        <span className="text-xs text-ink-500">{prompt.content?.length ?? 0} chars</span>
      </button>
      {open && prompt.content && (
        <div
          className="prose prose-invert prose-sm max-w-none mt-3 prose-headings:text-ink-100 prose-p:text-ink-200 prose-a:text-ork-amber"
          data-testid="agent-prompt-content"
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{prompt.content}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

function ArtifactsPanel({ artifacts }: { artifacts: ArtifactContent[] }) {
  const [selected, setSelected] = useState(0);
  if (artifacts.length === 0) {
    return (
      <section className="card" data-testid="agent-artifacts-section">
        <h3 className="text-sm font-semibold text-ink-200 mb-2">Output artifacts</h3>
        <p className="text-sm text-ink-500">
          No expected output files found on disk for this agent.
        </p>
      </section>
    );
  }
  const cur = artifacts[selected]!;
  return (
    <section className="card" data-testid="agent-artifacts-section">
      <h3 className="text-sm font-semibold text-ink-200 mb-3">
        Output artifacts ({artifacts.length})
      </h3>
      {artifacts.length > 1 && (
        <div className="flex flex-wrap gap-1 mb-3" data-testid="agent-artifacts-picker">
          {artifacts.map((a, i) => (
            <button
              key={a.name}
              onClick={() => setSelected(i)}
              data-testid={`agent-artifact-tab-${a.name}`}
              data-active={i === selected}
              className={`px-2 py-1 rounded text-xs font-mono ${
                i === selected ? "bg-ink-700 text-ink-50" : "text-ink-300 hover:bg-ink-800"
              }`}
            >
              {a.name}
            </button>
          ))}
        </div>
      )}
      <div className="border-t border-ink-700 pt-3" data-testid="agent-artifact-content">
        <div className="flex items-center justify-between mb-2">
          <code className="text-xs text-ink-300">{cur.relpath}</code>
          <span className="text-xs text-ink-500">{cur.size} bytes</span>
        </div>
        {cur.kind === "markdown" ? (
          <div className="prose prose-invert prose-sm max-w-none prose-headings:text-ink-100 prose-p:text-ink-200 prose-a:text-ork-amber max-h-[500px] overflow-auto">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{cur.content}</ReactMarkdown>
          </div>
        ) : cur.kind === "json" ? (
          <pre className="text-xs text-ink-200 overflow-auto max-h-[500px]">{tryPretty(cur.content)}</pre>
        ) : (
          <pre className="text-xs text-ink-200 whitespace-pre-wrap overflow-auto max-h-[500px]">
            {cur.content}
          </pre>
        )}
      </div>
    </section>
  );
}

function ChildSpawnsPanel({
  children: kids,
}: {
  children: Array<{
    spawn_id: string;
    child_run_id: string;
    depth: number;
    recipe: string | null;
    status: string;
    created_at: number;
  }>;
}) {
  return (
    <section className="card" data-testid="agent-children-section">
      <h3 className="text-sm font-semibold text-ink-200 mb-3 flex items-center gap-1.5">
        <GitBranch size={14} /> Child dispatches ({kids.length})
      </h3>
      {kids.length === 0 ? (
        <p className="text-sm text-ink-500">No child task_runs spawned by this agent.</p>
      ) : (
        <ul className="space-y-2">
          {kids.map((c) => (
            <li
              key={c.spawn_id}
              className="flex items-center gap-3 text-sm"
              data-testid={`agent-child-${c.child_run_id}`}
            >
              <span className="font-mono text-xs text-ink-500">d={c.depth}</span>
              <Link
                to="/runs/$taskRunId"
                params={{ taskRunId: c.child_run_id }}
                className="text-ork-amber font-mono text-xs hover:underline flex items-center gap-1"
              >
                {c.child_run_id} <ExternalLink size={10} />
              </Link>
              <span className="text-ink-300">{c.recipe ?? "—"}</span>
              <StatusPill status={c.status} />
              <span className="text-ink-500 text-xs ml-auto">{formatRelative(c.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const Cell = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div>
    <div className="text-[10px] uppercase tracking-wide text-ink-500 mb-0.5">{label}</div>
    <div className="text-xs">{children}</div>
  </div>
);

function tryPretty(s: string): string {
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch {
    return s;
  }
}
