import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import type React from "react";
import { ArrowLeft, FileJson, FileText } from "lucide-react";

import { api } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { RunInputContent } from "@/components/RunInputContent";

export function RunInputPage() {
  const { taskRunId, inputKey } = useParams({ from: "/runs/$taskRunId/inputs/$inputKey" });
  const q = useQuery({
    queryKey: ["run-input", taskRunId, inputKey],
    queryFn: () => api.input(taskRunId, inputKey),
  });

  if (q.error) {
    return (
      <div className="p-6 space-y-3" data-testid="run-input-error">
        <Link to="/runs/$taskRunId" params={{ taskRunId }} className="text-ork-amber text-sm">
          ← back to run
        </Link>
        <p className="text-red-300">{String(q.error)}</p>
      </div>
    );
  }

  if (!q.data) return <div className="p-6 text-ink-400">loading input…</div>;
  const doc = q.data;

  return (
    <div className="p-6 space-y-4 max-w-[1100px] mx-auto" data-testid="run-input-page">
      <div className="flex items-center gap-3 text-sm">
        <Link
          to="/runs/$taskRunId"
          params={{ taskRunId }}
          className="text-ink-400 hover:text-ink-100 flex items-center gap-1"
        >
          <ArrowLeft size={14} /> {taskRunId}
        </Link>
        <span className="text-ink-600">/</span>
        <span className="text-ink-100 font-mono">{doc.label}</span>
      </div>

      <header className="card flex items-start gap-3">
        <div className="mt-0.5 text-ork-amber">
          {doc.kind === "markdown" ? <FileText size={18} /> : <FileJson size={18} />}
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold text-ink-100">{doc.label}</h1>
          <div className="mt-1 grid grid-cols-1 md:grid-cols-3 gap-2 text-xs text-ink-400">
            <Meta label="file">{doc.name}</Meta>
            <Meta label="updated">{formatRelative(doc.mtime)}</Meta>
            <Meta label="path">
              <code className="break-all">{doc.path}</code>
            </Meta>
          </div>
        </div>
      </header>

      <section className="card">
        <RunInputContent kind={doc.kind} content={doc.content} />
      </section>
    </div>
  );
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-ink-500">{label}</div>
      <div className="truncate">{children}</div>
    </div>
  );
}
