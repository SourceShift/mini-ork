import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import {
  ArrowLeft,
  ArrowRight,
  Clock,
  ExternalLink,
  Flag,
  GitBranch,
  Hash,
  KeyRound,
  Timer,
} from "lucide-react";

import { api, type ParsedNote } from "@/lib/api";
import { formatCost, formatDuration, formatRelative } from "@/lib/format";
import { StatusPill, VerdictPill } from "@/components/Pill";

export function SelfImproveDetailPage() {
  const { runId } = useParams({ from: "/trajectory/self-improve/$runId" });
  const q = useQuery({
    queryKey: ["self-improve-detail", runId],
    queryFn: () => api.selfImproveDetail(runId),
    refetchInterval: 5_000,
  });

  if (q.error)
    return (
      <div className="p-6 space-y-2">
        <Link to="/trajectory" className="text-ork-amber text-sm">← back to trajectory</Link>
        <p className="text-red-300 mt-3">{String(q.error)}</p>
      </div>
    );

  const d = q.data;
  if (!d) return <div className="p-6 text-ink-400">loading…</div>;

  const now = Date.now() / 1000;
  const inProgress = !d.finished_at;
  const softLate = inProgress && now > d.soft_deadline_at;
  const hardLate = inProgress && now > d.hard_deadline_at;

  return (
    <div
      className="p-6 space-y-4 max-w-[1200px] mx-auto"
      data-testid="self-improve-detail-page"
      data-iter={d.iter}
      data-outcome={d.outcome}
    >
      <div className="flex items-center gap-3 text-sm">
        <Link to="/trajectory" className="text-ink-400 hover:text-ink-100 flex items-center gap-1">
          <ArrowLeft size={14} /> trajectory
        </Link>
        <span className="text-ink-600">/</span>
        <span className="text-ink-300">self-improve</span>
        <span className="text-ink-600">/</span>
        <span className="text-ink-100 font-mono">iter {d.iter}</span>
      </div>

      <header className="card flex items-start justify-between flex-wrap gap-4" data-testid="si-header">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">Iter {d.iter}</h1>
            <StatusPill status={d.outcome} />
            {hardLate && <span className="pill-err">hard deadline passed</span>}
            {softLate && !hardLate && <span className="pill-warn">soft deadline passed</span>}
          </div>
          <code className="text-xs text-ink-400">{d.run_id}</code>
        </div>
        <SiblingNav siblings={d.siblings} currentIter={d.iter} />
      </header>

      <section className="grid grid-cols-2 gap-4">
        <div className="card space-y-3">
          <h2 className="text-sm font-semibold text-ink-200">Timeline</h2>
          <Row icon={<Clock size={14} />} label="started">
            {formatRelative(d.started_at)}
          </Row>
          <Row icon={<Clock size={14} />} label="finished">
            {d.finished_at ? formatRelative(d.finished_at) : <span className="text-ork-amber">in progress</span>}
          </Row>
          <Row icon={<Timer size={14} />} label="wall time">
            {d.finished_at
              ? formatDuration((d.finished_at - d.started_at) * 1000)
              : formatDuration((now - d.started_at) * 1000)}
          </Row>
          <Row icon={<Timer size={14} />} label="soft deadline" tone={softLate ? "warn" : undefined}>
            <DeadlineCell ts={d.soft_deadline_at} reached={!inProgress} now={now} />
          </Row>
          <Row icon={<Timer size={14} />} label="hard deadline" tone={hardLate ? "err" : undefined}>
            <DeadlineCell ts={d.hard_deadline_at} reached={!inProgress} now={now} />
          </Row>
        </div>

        <div className="card space-y-3">
          <h2 className="text-sm font-semibold text-ink-200">Worktree</h2>
          <Row icon={<GitBranch size={14} />} label="branch">
            <code className="text-xs text-ink-200">{d.branch_name ?? "—"}</code>
          </Row>
          <Row icon={<GitBranch size={14} />} label="worktree">
            <code className="text-xs text-ink-200 break-all">{d.worktree_path ?? "—"}</code>
          </Row>
          <Row icon={<Hash size={14} />} label="parent">
            {d.parent_run_id ? (
              <Link
                to="/trajectory/self-improve/$runId"
                params={{ runId: d.parent_run_id }}
                className="text-ork-amber font-mono text-xs hover:underline"
              >
                {d.parent_run_id}
              </Link>
            ) : (
              <span className="text-ink-500">none</span>
            )}
          </Row>
          <Row icon={<Hash size={14} />} label="children">
            {d.children.length ? (
              <span className="font-mono text-xs">{d.children.length} spawned</span>
            ) : (
              <span className="text-ink-500">none</span>
            )}
          </Row>
        </div>
      </section>

      {d.linked_task_run && (
        <section className="card" data-testid="si-linked-task-run">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-ink-200">Linked task_run</h2>
            <Link
              to="/runs/$taskRunId"
              params={{ taskRunId: d.linked_task_run.id }}
              className="text-ork-amber text-xs flex items-center gap-1 hover:underline"
            >
              open run forensics <ExternalLink size={12} />
            </Link>
          </div>
          <div className="grid grid-cols-4 gap-3 text-sm">
            <Cell label="recipe">{d.linked_task_run.recipe ?? "—"}</Cell>
            <Cell label="status"><StatusPill status={d.linked_task_run.status} /></Cell>
            <Cell label="verdict"><VerdictPill verdict={d.linked_task_run.verdict} /></Cell>
            <Cell label="cost">{formatCost(d.linked_task_run.cost_usd)}</Cell>
            <Cell label="duration">{formatDuration(d.linked_task_run.duration_ms)}</Cell>
            <Cell label="trace">
              <code className="text-xs text-ink-400">{d.linked_task_run.trace_id ?? "—"}</code>
            </Cell>
            <Cell label="plan">
              <code className="text-xs text-ink-400 truncate block">{d.linked_task_run.plan_path ?? "—"}</code>
            </Cell>
            <Cell label="artifact">
              <code className="text-xs text-ink-400 truncate block">{d.linked_task_run.artifact_path ?? "—"}</code>
            </Cell>
          </div>
        </section>
      )}

      <section className="card" data-testid="si-notes">
        <h2 className="text-sm font-semibold text-ink-200 mb-3">Notes</h2>
        {d.parsed_notes.length ? (
          <ul className="grid grid-cols-2 gap-y-2 gap-x-6">
            {d.parsed_notes.map((n, i) => (
              <NoteItem key={`${n.key}-${i}`} note={n} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-400">no notes recorded</p>
        )}
        {d.notes && (
          <details className="mt-3">
            <summary className="text-xs text-ink-400 cursor-pointer hover:text-ink-200">raw notes</summary>
            <pre className="mt-2 text-xs text-ink-300 whitespace-pre-wrap font-mono bg-ink-900/60 p-2 rounded">
              {d.notes}
            </pre>
          </details>
        )}
      </section>

      {d.children.length > 0 && (
        <section className="card">
          <h2 className="text-sm font-semibold text-ink-200 mb-3">
            Descendant iterations ({d.children.length})
          </h2>
          <ul className="divide-y divide-ink-800">
            {d.children.map((c) => (
              <li key={c.run_id} className="py-2 flex items-center gap-3 text-sm">
                <Link
                  to="/trajectory/self-improve/$runId"
                  params={{ runId: c.run_id }}
                  className="text-ork-amber font-mono text-xs hover:underline"
                >
                  iter {c.iter}
                </Link>
                <StatusPill status={c.outcome} />
                <span className="text-ink-400 text-xs">{formatRelative(c.started_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function NoteItem({ note }: { note: ParsedNote }) {
  const Icon = note.kind === "flag" ? Flag : note.kind === "sha" ? Hash : KeyRound;
  if (note.kind === "flag") {
    return (
      <li className="flex items-center gap-2 text-sm">
        <Icon size={12} className="text-ork-amber" />
        <span className="font-mono text-ink-200">{note.key}</span>
      </li>
    );
  }
  if (note.kind === "sha") {
    const [ref, sha] = note.value.split("@");
    return (
      <li className="flex items-center gap-2 text-sm min-w-0">
        <Icon size={12} className="text-ork-green flex-shrink-0" />
        <span className="font-mono text-ink-400 flex-shrink-0">{note.key}</span>
        <span className="text-ink-500 flex-shrink-0">=</span>
        <code className="text-xs text-ink-200">{ref}</code>
        {sha && (
          <code
            className="text-xs text-ork-amber bg-ink-900/60 px-1.5 py-0.5 rounded"
            title={sha}
          >
            {sha.slice(0, 7)}
          </code>
        )}
      </li>
    );
  }
  return (
    <li className="flex items-center gap-2 text-sm min-w-0">
      <Icon size={12} className="text-ink-400 flex-shrink-0" />
      <span className="font-mono text-ink-400 flex-shrink-0">{note.key}</span>
      <span className="text-ink-500 flex-shrink-0">=</span>
      <span className="text-ink-200 truncate">{note.value || <span className="text-ink-500">(empty)</span>}</span>
    </li>
  );
}

function SiblingNav({ siblings, currentIter }: { siblings: { iter: number; run_id: string; outcome: string }[]; currentIter: number }) {
  const prev = siblings.find((s) => s.iter === currentIter - 1);
  const next = siblings.find((s) => s.iter === currentIter + 1);
  return (
    <div className="flex items-center gap-1 text-xs">
      {prev ? (
        <Link
          to="/trajectory/self-improve/$runId"
          params={{ runId: prev.run_id }}
          className="px-2 py-1 rounded border border-ink-700 text-ink-300 hover:border-ink-500 hover:text-ink-100 flex items-center gap-1"
          title={`iter ${prev.iter} · ${prev.outcome}`}
        >
          <ArrowLeft size={12} /> iter {prev.iter}
        </Link>
      ) : (
        <span className="px-2 py-1 text-ink-600">first</span>
      )}
      {next ? (
        <Link
          to="/trajectory/self-improve/$runId"
          params={{ runId: next.run_id }}
          className="px-2 py-1 rounded border border-ink-700 text-ink-300 hover:border-ink-500 hover:text-ink-100 flex items-center gap-1"
          title={`iter ${next.iter} · ${next.outcome}`}
        >
          iter {next.iter} <ArrowRight size={12} />
        </Link>
      ) : (
        <span className="px-2 py-1 text-ink-600">latest</span>
      )}
    </div>
  );
}

function DeadlineCell({ ts, reached, now }: { ts: number; reached: boolean; now: number }) {
  const diff = ts - now;
  if (reached) {
    return <span className="text-ink-400">{formatRelative(ts)}</span>;
  }
  if (diff > 0) {
    const mins = Math.floor(diff / 60);
    const hrs = Math.floor(mins / 60);
    const label = hrs >= 1 ? `${hrs}h ${mins % 60}m` : `${mins}m`;
    return <span className="text-ork-green">{label} remaining</span>;
  }
  const over = Math.floor(-diff / 60);
  return <span className="text-red-300">{over}m over</span>;
}

function Row({
  icon,
  label,
  children,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
  tone?: "warn" | "err";
}) {
  const toneClass = tone === "warn" ? "text-ork-amber" : tone === "err" ? "text-red-300" : "";
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-ink-400">
        {icon}
        {label}
      </span>
      <span className={`text-sm ${toneClass} text-right min-w-0`}>{children}</span>
    </div>
  );
}

const Cell = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="flex flex-col gap-0.5">
    <span className="text-xs uppercase tracking-wide text-ink-400">{label}</span>
    <span>{children}</span>
  </div>
);
