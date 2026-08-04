import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Copy, LifeBuoy, RotateCcw } from "lucide-react";

import { api, type RecoveryNode } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { statusPillClass } from "@/lib/format";

/** Durable-DAG resume, honestly. The backend assembles a DAG-shaped projection
 * from the durable tables (node_checkpoints / node_attempts / recovery_requests
 * / run_leases) — it is READ-ONLY. Recovery itself is a CLI act
 * (`mini-ork recover <run_id>`), leases + idempotency live server-side, and a
 * button that fired it from a query-only UI would be lying about what it can
 * guarantee. So we render the projection (which nodes are reusable, what failed,
 * whether a lease is live) and hand the operator the exact command to run —
 * same machine-proposes/human-runs contract as dispatch. Renders nothing until
 * there's something to recover, so it stays out of the way on healthy runs. */
export function ResumePanel({ taskRunId }: { taskRunId: string }) {
  const recovery = useQuery({
    queryKey: ["recovery", taskRunId],
    queryFn: () => api.recovery(taskRunId),
    refetchInterval: 10_000,
  });

  const proj = recovery.data;
  // Nothing recorded in the durable tables → this run never used durable-dag,
  // or predates it. Don't show an empty recovery card on every run.
  if (!proj || proj.nodes.length === 0) return null;

  const failed = proj.nodes.filter((n) => !isTerminalOk(n.status));
  const reusable = proj.nodes.filter((n) => n.reusable);
  const rec = proj.active_recovery;
  const recActive = rec != null && (rec.status === "pending" || rec.status === "dispatched");

  return (
    <section className="card" data-testid="resume-panel" data-has-failed={failed.length > 0}>
      <div className="panel-title flex items-center gap-2">
        <LifeBuoy size={13} className="text-[var(--cyan)]" />
        Durable resume
        <span className="ml-auto text-[9.5px] font-normal normal-case tracking-normal text-ink-600">
          {reusable.length}/{proj.nodes.length} nodes reusable
        </span>
      </div>

      <p className="mt-1 text-[11px] text-ink-400" data-testid="resume-next-action">
        {proj.next_action || "no recorded nodes for this run"}
      </p>

      {rec && (
        <div
          className="mt-2 flex flex-wrap items-center gap-2 rounded-[3px] border border-[var(--hair-2)] bg-[var(--panel-2)] px-2 py-1.5"
          data-testid="resume-active-recovery"
        >
          <span className={recActive ? "pill-warn" : "pill-muted"}>
            <RotateCcw size={11} /> recovery {rec.status}
          </span>
          {rec.from_node && (
            <span className="font-mono text-[10.5px] text-ink-300">from {rec.from_node}</span>
          )}
          <span className="text-[10px] text-ink-500">
            dispatch #{rec.dispatch_count}
            {rec.failure_class ? ` · ${rec.failure_class}` : ""}
          </span>
          {proj.lease && (
            <span
              className={proj.lease.live ? "pill-ok ml-auto" : "pill-muted ml-auto"}
              title={`owner ${proj.lease.owner_token} · expires ${formatRelative(proj.lease.expires_at)}`}
            >
              lease {proj.lease.live ? "live" : "stale"}
            </span>
          )}
        </div>
      )}

      <NodeLadder nodes={proj.nodes} />

      {failed.length > 0 && !recActive && <ResumeCommand runId={proj.run_id} />}
    </section>
  );
}

/** Compact node ladder: each recorded node with its status + attempt count.
 * Failed nodes surface their last failure_class so the operator knows what
 * broke without opening the agent forensics. */
function NodeLadder({ nodes }: { nodes: RecoveryNode[] }) {
  return (
    <div className="mt-2 space-y-1" data-testid="resume-node-ladder">
      {nodes.map((n) => {
        const last = n.attempts.at(-1);
        return (
          <div
            key={n.node_id}
            className="flex items-center gap-2 rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2 py-1"
            data-testid={`resume-node-${n.node_id}`}
            data-reusable={n.reusable}
          >
            <span className={statusPillClass(n.status)}>{n.status}</span>
            <span className="font-mono text-[11px] text-ink-100">{n.node_id}</span>
            {n.reusable && (
              <span className="text-[9.5px] uppercase tracking-[0.08em] text-[var(--grn)]">reuse</span>
            )}
            <span className="ml-auto text-[9.5px] text-ink-500">
              {n.attempts.length} attempt{n.attempts.length === 1 ? "" : "s"}
              {last?.failure_class ? ` · ${last.failure_class}` : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** The honest recovery action: a copy-to-run command, not a fake button. */
function ResumeCommand({ runId }: { runId: string }) {
  const [copied, setCopied] = useState(false);
  const cmd = `mini-ork recover ${runId}`;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(cmd);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // clipboard unavailable (insecure context) — the command is visible to
      // hand-copy, so this is a soft failure not worth surfacing.
    }
  };

  return (
    <div
      className="mt-2 flex items-center gap-2 rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-2 py-1.5"
      data-testid="resume-command"
    >
      <span className="text-[9.5px] uppercase tracking-[0.13em] text-ink-500">resume</span>
      <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-[var(--amb)]" title={cmd}>
        {cmd}
      </code>
      <button
        type="button"
        onClick={copy}
        className="flex shrink-0 items-center gap-1 rounded-[3px] border border-[var(--hair-2)] px-1.5 py-0.5 text-[9.5px] uppercase tracking-[0.08em] text-ink-400 hover:border-[var(--cyan)] hover:text-[var(--cyan)]"
        data-testid="resume-command-copy"
      >
        {copied ? <Check size={11} className="text-[var(--grn)]" /> : <Copy size={11} />}
        {copied ? "copied" : "copy"}
      </button>
    </div>
  );
}

function isTerminalOk(status: string): boolean {
  return status === "success" || status === "skipped";
}
