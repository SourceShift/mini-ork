import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, ExternalLink, FileText, XCircle } from "lucide-react";
import clsx from "clsx";

import { api, type Diagnostic, type VerifierResult } from "@/lib/api";

const TESTID = "why-card";

export function WhyCard({ taskRunId }: { taskRunId: string }) {
  const q = useQuery({
    queryKey: ["why", taskRunId],
    queryFn: () => api.why(taskRunId),
    refetchInterval: 10_000,
  });

  if (q.isLoading)
    return (
      <div className="card" data-testid={`${TESTID}-loading`}>
        <p className="text-sm text-ink-400">loading diagnostics…</p>
      </div>
    );
  if (!q.data) return null;
  const d = q.data;

  const passedV = d.verifier_results.filter((v) => v.pass).length;
  const failedV = d.verifier_results.filter((v) => !v.pass).length;
  const hasFailures =
    failedV > 0 ||
    d.evidence_refs.length > 0 ||
    (d.execute_log?.failure_lines?.length ?? 0) > 0;

  return (
    <div
      className={clsx(
        "card border-l-4 space-y-3",
        hasFailures ? "border-ork-red/50" : "border-ork-green/40",
      )}
      data-testid={TESTID}
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-ink-200 flex items-center gap-2">
          {hasFailures ? (
            <AlertTriangle size={16} className="text-ork-red" />
          ) : (
            <CheckCircle2 size={16} className="text-ork-green" />
          )}
          Why? — failure evidence
        </h3>
        <span
          className="text-xs text-ink-400"
          data-testid={`${TESTID}-summary`}
          title={d.summary}
        >
          {d.summary}
        </span>
      </div>

      {!d.run_dir_exists && (
        <div className="text-xs text-ork-amber" data-testid={`${TESTID}-no-rundir`}>
          ⚠ run directory not found at {d.run_dir} — most evidence is on disk
        </div>
      )}

      {d.verifier_results.length > 0 && (
        <div data-testid={`${TESTID}-verifier-results`}>
          <div className="text-xs uppercase tracking-wide text-ink-400 mb-1.5">
            verifiers ({passedV} pass · {failedV} fail)
          </div>
          <ul className="space-y-1">
            {d.verifier_results.map((v) => (
              <VerifierRow key={v.verifier} v={v} taskRunId={taskRunId} />
            ))}
          </ul>
        </div>
      )}

      {d.evidence_refs.length > 0 && (
        <div data-testid={`${TESTID}-evidence-refs`}>
          <div className="text-xs uppercase tracking-wide text-ink-400 mb-1.5">
            evidence references from execute.log
          </div>
          <ul className="space-y-1">
            {d.evidence_refs.map((e, i) => (
              <EvidenceRefRow key={i} ref={e} taskRunId={taskRunId} />
            ))}
          </ul>
        </div>
      )}

      {d.self_improve_notes && (
        <div data-testid={`${TESTID}-self-improve-notes`}>
          <div className="text-xs uppercase tracking-wide text-ink-400 mb-1.5">
            self_improve outcome
          </div>
          <div className="text-xs">
            <span className="text-ink-300">outcome:</span>{" "}
            <code className="text-ink-100">{d.self_improve_notes.outcome}</code>
            {d.self_improve_notes.raw && (
              <>
                {" · "}
                <span className="text-ink-300">notes:</span>{" "}
                <code className="text-ink-400">{d.self_improve_notes.raw}</code>
              </>
            )}
            {d.self_improve_notes.raw === "all-verifiers-pass" && failedV === 0 &&
              (d.execute_log?.failure_lines?.length ?? 0) > 0 && (
                <div className="text-ork-amber mt-1">
                  ⚠ self_improve says "all-verifiers-pass" but execute.log shows a node failure
                  — possible bookkeeping inconsistency between layers
                </div>
              )}
          </div>
        </div>
      )}

      <ExecuteLogPanel diag={d} />
    </div>
  );
}

function VerifierRow({ v, taskRunId }: { v: VerifierResult; taskRunId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <li
      className="text-xs"
      data-testid={`${TESTID}-verifier-${v.verifier}`}
    >
      <button
        onClick={() => setOpen((x) => !x)}
        className="flex items-center gap-2 hover:text-ink-100 w-full text-left"
        data-testid={`${TESTID}-verifier-${v.verifier}-toggle`}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {v.pass ? (
          <CheckCircle2 size={12} className="text-ork-green" />
        ) : (
          <XCircle size={12} className="text-ork-red" />
        )}
        <code className="text-ink-200">{v.verifier}</code>
        <span className={v.pass ? "text-ork-green" : "text-ork-red"}>
          {v.pass ? "PASS" : "FAIL"}
        </span>
        {v.evidence_path && (
          <span className="text-ink-500 ml-auto truncate" title={v.evidence_path}>
            {v.evidence_path.split("/").pop()}
          </span>
        )}
      </button>
      {open && (
        <pre
          className="ml-5 mt-1 text-[10px] text-ink-300 bg-ink-900/60 p-2 rounded overflow-auto"
          data-testid={`${TESTID}-verifier-${v.verifier}-payload`}
        >
          {JSON.stringify(v.payload, null, 2)}
        </pre>
      )}
      {open && v.evidence_path && (
        <div className="ml-5 mt-1">
          <EvidenceLink path={v.evidence_path} taskRunId={taskRunId} />
        </div>
      )}
    </li>
  );
}

function EvidenceRefRow({
  ref: r,
  taskRunId,
}: {
  ref: { verifier: string; evidence_path: string };
  taskRunId: string;
}) {
  return (
    <li
      className="text-xs flex items-center gap-2"
      data-testid={`${TESTID}-evidence-${r.verifier.replace(/[^a-z0-9]/gi, "-")}`}
    >
      <XCircle size={12} className="text-ork-red flex-shrink-0" />
      <code className="text-ink-200 flex-shrink-0">{r.verifier}</code>
      <span className="text-ink-500">→</span>
      <EvidenceLink path={r.evidence_path} taskRunId={taskRunId} />
    </li>
  );
}

function EvidenceLink({ path, taskRunId }: { path: string; taskRunId: string }) {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["evidence", taskRunId, path],
    queryFn: () => api.evidence(taskRunId, path),
    enabled: open,
  });
  return (
    <span className="inline-flex items-center gap-1 min-w-0">
      <button
        onClick={() => setOpen((x) => !x)}
        className="text-ork-amber hover:underline flex items-center gap-1 text-xs truncate"
        data-testid={`${TESTID}-evidence-open`}
        title={path}
      >
        <FileText size={10} />
        {path.split("/").slice(-2).join("/")}
      </button>
      {open && (
        <div className="block ml-5 mt-1 w-full" data-testid={`${TESTID}-evidence-content`}>
          {q.isLoading && <p className="text-ink-400 text-xs">loading…</p>}
          {q.error && (
            <p className="text-red-300 text-xs">
              load failed: {String(q.error)} <ExternalLink size={10} className="inline" />
            </p>
          )}
          {q.data && (
            <pre className="text-[10px] text-ink-200 bg-ink-900/60 p-2 rounded overflow-auto max-h-72 whitespace-pre-wrap">
              {q.data.truncated && <span className="text-ork-amber">[truncated]\n</span>}
              {q.data.content}
            </pre>
          )}
        </div>
      )}
    </span>
  );
}

function ExecuteLogPanel({ diag }: { diag: Diagnostic }) {
  const [open, setOpen] = useState((diag.execute_log?.failure_lines?.length ?? 0) > 0);
  if (!diag.execute_log) return null;

  return (
    <div data-testid={`${TESTID}-execute-log`}>
      <button
        onClick={() => setOpen((x) => !x)}
        className="text-xs uppercase tracking-wide text-ink-400 flex items-center gap-1.5 hover:text-ink-200"
        data-testid={`${TESTID}-execute-log-toggle`}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        execute.log ({diag.execute_log.total_lines} lines, {diag.execute_log.failure_lines.length} flagged)
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {diag.execute_log.failure_lines.length > 0 && (
            <div data-testid={`${TESTID}-failure-lines`}>
              <div className="text-[10px] uppercase text-ork-red mb-1">failure-pattern matches</div>
              <ul className="space-y-0.5">
                {diag.execute_log.failure_lines.map((L, i) => (
                  <li
                    key={i}
                    className="text-[11px] font-mono text-red-300 bg-ork-red/10 px-2 py-1 rounded ring-1 ring-ork-red/20"
                    data-testid={`${TESTID}-failure-line-${L.line_no}`}
                  >
                    <span className="text-ink-500">L{L.line_no}:</span> {L.text}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <details data-testid={`${TESTID}-execute-log-tail`}>
            <summary className="text-[10px] uppercase text-ink-400 cursor-pointer hover:text-ink-200">
              last {diag.execute_log.tail.length} lines
            </summary>
            <pre className="text-[10px] text-ink-300 bg-ink-900/60 p-2 rounded overflow-auto max-h-72 mt-1">
              {diag.execute_log.tail.join("\n")}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}
