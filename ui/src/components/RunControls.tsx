import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Banknote,
  Loader2,
  Lock,
  MessageSquareText,
  PlayCircle,
  Send,
  Skull,
  StopCircle,
} from "lucide-react";

import { api } from "@/lib/api";
import { useOperatorToken } from "@/lib/useOperatorToken";

const CONTROLLABLE_STATUSES = new Set([
  "classified",
  "planned",
  "executing",
  "verifying",
  "reviewing",
]);

type RoleTarget = "any" | "planner" | "implementer" | "reviewer" | "verifier";
type Severity = "info" | "warn" | "critical";

export function RunControls({
  taskRunId,
  status,
  compact = false,
}: {
  taskRunId: string;
  status: string | null | undefined;
  compact?: boolean;
}) {
  const qc = useQueryClient();
  const token = useOperatorToken();
  const hasToken = token != null;
  const [confirmKill, setConfirmKill] = useState(false);
  const [lastResult, setLastResult] = useState<string | null>(null);
  const [threshold, setThreshold] = useState(25);
  const [steerOpen, setSteerOpen] = useState(false);
  const [steerMsg, setSteerMsg] = useState("");
  const [steerRole, setSteerRole] = useState<RoleTarget>("any");
  const [steerSeverity, setSteerSeverity] = useState<Severity>("info");

  const controllable = status != null && CONTROLLABLE_STATUSES.has(status);

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["task-run", taskRunId] });
    qc.invalidateQueries({ queryKey: ["why", taskRunId] });
    qc.invalidateQueries({ queryKey: ["agents", taskRunId] });
    qc.invalidateQueries({ queryKey: ["events", taskRunId] });
    qc.invalidateQueries({ queryKey: ["task-runs"] });
    qc.invalidateQueries({ queryKey: ["summary"] });
  }

  const stop = useMutation({
    mutationFn: () => api.stop(taskRunId),
    onSuccess: (r) => {
      setLastResult(`stop requested · ${r.note}`);
      invalidate();
    },
    onError: (e) => setLastResult(`stop failed: ${String(e)}`),
  });

  const kill = useMutation({
    mutationFn: () => api.kill(taskRunId),
    onSuccess: (r) => {
      setLastResult(
        `killed ${r.pids_signaled.length} pid(s)` +
          (r.pids_survived_permission_denied.length
            ? ` · ${r.pids_survived_permission_denied.length} survived (permission denied)`
            : ""),
      );
      setConfirmKill(false);
      invalidate();
    },
    onError: (e) => {
      setLastResult(`kill failed: ${String(e)}`);
      setConfirmKill(false);
    },
  });

  const pauseCost = useMutation({
    mutationFn: () => api.pauseCost(taskRunId, threshold),
    onSuccess: (r) => {
      setLastResult(r.note ?? `cost paused at $${threshold}` + (r.operator ? ` · ${r.operator}` : ""));
      invalidate();
    },
    onError: (e) => setLastResult(privError("pause-cost", e)),
  });

  const resumeCost = useMutation({
    mutationFn: () => api.resumeCost(taskRunId),
    onSuccess: (r) => {
      setLastResult(r.note ?? "cost resumed" + (r.operator ? ` · ${r.operator}` : ""));
      invalidate();
    },
    onError: (e) => setLastResult(privError("resume-cost", e)),
  });

  const steer = useMutation({
    mutationFn: () =>
      api.steer(taskRunId, {
        message: steerMsg.trim(),
        role_target: steerRole,
        severity: steerSeverity,
      }),
    onSuccess: (r) => {
      setLastResult(r.note ?? `steer sent to ${steerRole}` + (r.operator ? ` · ${r.operator}` : ""));
      setSteerMsg("");
      setSteerOpen(false);
      invalidate();
    },
    onError: (e) => setLastResult(privError("steer", e)),
  });

  const busy = stop.isPending || kill.isPending;

  if (!controllable) {
    return (
      <div className="flex items-center gap-2 text-xs text-ink-500" data-testid="run-controls-terminal">
        <span>{compact ? "—" : "terminal status — no controls available"}</span>
      </div>
    );
  }

  const abortRow = (
    <div className="flex items-center gap-2" data-testid="run-controls">
      <button
        onClick={() => stop.mutate()}
        disabled={busy}
        data-testid="run-control-stop"
        className="px-3 py-1.5 rounded text-xs font-medium border border-ork-amber/40 bg-ork-amber/10 text-ork-amber hover:bg-ork-amber/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
        title="Soft stop — current node finishes, no new dispatches"
      >
        {stop.isPending ? <Loader2 size={12} className="animate-spin" /> : <StopCircle size={12} />}
        Stop
      </button>

      {confirmKill ? (
        <div className="flex items-center gap-1.5" data-testid="run-control-kill-confirm">
          <span className="text-xs text-red-300 flex items-center gap-1">
            <AlertTriangle size={12} /> Kill the dispatcher now?
          </span>
          <button
            onClick={() => kill.mutate()}
            disabled={kill.isPending}
            data-testid="run-control-kill-confirm-yes"
            className="px-2 py-1 rounded text-xs font-medium border border-ork-red/40 bg-ork-red/20 text-red-200 hover:bg-ork-red/30 disabled:opacity-50 flex items-center gap-1"
          >
            {kill.isPending ? <Loader2 size={12} className="animate-spin" /> : "yes, kill"}
          </button>
          <button
            onClick={() => setConfirmKill(false)}
            disabled={kill.isPending}
            data-testid="run-control-kill-confirm-no"
            className="px-2 py-1 rounded text-xs text-ink-300 hover:text-ink-100"
          >
            cancel
          </button>
        </div>
      ) : (
        <button
          onClick={() => setConfirmKill(true)}
          disabled={busy}
          data-testid="run-control-kill"
          className="px-3 py-1.5 rounded text-xs font-medium border border-ork-red/40 bg-ork-red/10 text-red-300 hover:bg-ork-red/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
          title="Hard kill — SIGTERM → 2s → SIGKILL the dispatcher pid"
        >
          <Skull size={12} />
          Kill
        </button>
      )}

      {lastResult && (
        <span
          className="text-xs text-ink-400 max-w-[280px] truncate"
          data-testid="run-control-result"
          title={lastResult}
        >
          {lastResult}
        </span>
      )}
    </div>
  );

  // Compact (fleet table): just the loopback-trust abort controls — the full
  // write plane lives on the run detail page where there's room for it.
  if (compact) return abortRow;

  return (
    <div className="flex flex-col gap-2" data-testid="run-controls-full">
      {abortRow}

      <div
        className="flex flex-wrap items-center gap-2 border-t border-[var(--hair)] pt-2"
        data-testid="run-controls-privileged"
        data-authorized={hasToken}
      >
        {!hasToken && (
          <span className="flex items-center gap-1 text-[11px] text-ink-500" data-testid="run-controls-needs-token">
            <Lock size={11} /> set an operator token to unlock cost + steer
          </span>
        )}

        <label className="flex items-center gap-1 text-[11px] text-ink-400">
          <span className="text-ink-500">pause at $</span>
          <input
            type="number"
            min={0}
            step={5}
            value={threshold}
            disabled={!hasToken}
            onChange={(e) => setThreshold(Math.max(0, Number(e.target.value) || 0))}
            className="h-6 w-16 rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-1.5 font-mono text-[11px] text-ink-200 outline-none focus:border-[var(--cyan)] disabled:opacity-40"
            data-testid="run-control-pause-threshold"
          />
        </label>
        <button
          onClick={() => pauseCost.mutate()}
          disabled={!hasToken || pauseCost.isPending}
          data-testid="run-control-pause-cost"
          className="flex items-center gap-1.5 rounded px-2.5 py-1.5 text-xs font-medium border border-ork-amber/40 bg-ork-amber/10 text-ork-amber hover:bg-ork-amber/20 disabled:opacity-40 disabled:cursor-not-allowed"
          title="Freeze new dispatches once run cost crosses the threshold"
        >
          {pauseCost.isPending ? <Loader2 size={12} className="animate-spin" /> : <Banknote size={12} />}
          Pause on cost
        </button>
        <button
          onClick={() => resumeCost.mutate()}
          disabled={!hasToken || resumeCost.isPending}
          data-testid="run-control-resume-cost"
          className="flex items-center gap-1.5 rounded px-2.5 py-1.5 text-xs font-medium border border-[var(--grn)]/40 bg-[var(--w-grn)] text-[var(--grn)] hover:bg-[rgba(79,209,160,0.18)] disabled:opacity-40 disabled:cursor-not-allowed"
          title="Lift a cost pause and let the run continue dispatching"
        >
          {resumeCost.isPending ? <Loader2 size={12} className="animate-spin" /> : <PlayCircle size={12} />}
          Resume
        </button>
        <button
          onClick={() => setSteerOpen((v) => !v)}
          disabled={!hasToken}
          data-testid="run-control-steer-toggle"
          data-open={steerOpen}
          className="flex items-center gap-1.5 rounded px-2.5 py-1.5 text-xs font-medium border border-[var(--cyan)]/40 bg-[var(--cyan)]/10 text-[var(--cyan)] hover:bg-[var(--cyan)]/20 disabled:opacity-40 disabled:cursor-not-allowed"
          title="Inject a mid-run instruction the next agent turn will see"
        >
          <MessageSquareText size={12} />
          Steer
        </button>
      </div>

      {steerOpen && hasToken && (
        <form
          className="flex flex-col gap-2 rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] p-2"
          data-testid="run-control-steer-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (steerMsg.trim()) steer.mutate();
          }}
        >
          <textarea
            value={steerMsg}
            onChange={(e) => setSteerMsg(e.target.value)}
            rows={2}
            placeholder="e.g. prioritize the failing auth test before refactoring"
            className="w-full resize-y rounded-[3px] border border-[var(--hair-2)] bg-[var(--panel)] px-2 py-1.5 text-[11px] text-ink-200 outline-none placeholder:text-ink-600 focus:border-[var(--cyan)]"
            data-testid="run-control-steer-message"
          />
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={steerRole}
              onChange={(e) => setSteerRole(e.target.value as RoleTarget)}
              className="h-6 rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-1.5 text-[11px] text-ink-300 outline-none focus:border-[var(--cyan)]"
              data-testid="run-control-steer-role"
            >
              <option value="any">any role</option>
              <option value="planner">planner</option>
              <option value="implementer">implementer</option>
              <option value="reviewer">reviewer</option>
              <option value="verifier">verifier</option>
            </select>
            <select
              value={steerSeverity}
              onChange={(e) => setSteerSeverity(e.target.value as Severity)}
              className="h-6 rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-1.5 text-[11px] text-ink-300 outline-none focus:border-[var(--cyan)]"
              data-testid="run-control-steer-severity"
            >
              <option value="info">info</option>
              <option value="warn">warn</option>
              <option value="critical">critical</option>
            </select>
            <button
              type="submit"
              disabled={!steerMsg.trim() || steer.isPending}
              className="ml-auto flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium border border-[var(--cyan)]/50 bg-[var(--cyan)]/15 text-[var(--cyan)] hover:bg-[var(--cyan)]/25 disabled:opacity-40 disabled:cursor-not-allowed"
              data-testid="run-control-steer-send"
            >
              {steer.isPending ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />}
              Send steer
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

/** Privileged writes are Bearer-gated and fail-closed — surface the 401 as an
 * actionable "check your token" rather than a raw error string. */
function privError(action: string, e: unknown): string {
  const msg = String(e);
  if (msg.includes("401")) return `${action} denied (401) — check your operator token`;
  return `${action} failed: ${msg}`;
}
