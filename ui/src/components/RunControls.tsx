import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Skull, StopCircle } from "lucide-react";

import { api } from "@/lib/api";

const CONTROLLABLE_STATUSES = new Set([
  "classified",
  "planned",
  "executing",
  "verifying",
  "reviewing",
]);

export function RunControls({
  taskRunId,
  status,
}: {
  taskRunId: string;
  status: string | null | undefined;
}) {
  const qc = useQueryClient();
  const [confirmKill, setConfirmKill] = useState(false);
  const [lastResult, setLastResult] = useState<string | null>(null);

  const controllable = status != null && CONTROLLABLE_STATUSES.has(status);

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

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["task-run", taskRunId] });
    qc.invalidateQueries({ queryKey: ["why", taskRunId] });
    qc.invalidateQueries({ queryKey: ["agents", taskRunId] });
    qc.invalidateQueries({ queryKey: ["events", taskRunId] });
    qc.invalidateQueries({ queryKey: ["task-runs"] });
    qc.invalidateQueries({ queryKey: ["summary"] });
  }

  if (!controllable) {
    return (
      <div className="flex items-center gap-2 text-xs text-ink-500" data-testid="run-controls-terminal">
        <span>terminal status — no controls available</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2" data-testid="run-controls">
      <button
        onClick={() => stop.mutate()}
        disabled={stop.isPending || kill.isPending}
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
          disabled={stop.isPending || kill.isPending}
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
}
