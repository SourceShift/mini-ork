import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api, type ArtifactEntry } from "@/lib/api";

export function ArtifactViewer({ taskRunId }: { taskRunId: string }) {
  const list = useQuery({
    queryKey: ["artifacts", taskRunId],
    queryFn: () => api.artifacts(taskRunId),
    refetchInterval: 5_000,
  });
  const [selected, setSelected] = useState<string | null>(null);

  const deliverables = (list.data ?? []).filter(isDeliverableArtifact);
  const initial = deliverables[0]?.relpath ?? null;
  const target = selected ?? initial;
  const file = useQuery({
    queryKey: ["artifact", taskRunId, target],
    queryFn: () => api.artifact(taskRunId, target!),
    enabled: !!target,
  });

  return (
    <div className="grid grid-cols-[240px_1fr] gap-4" data-testid="artifact-viewer">
      <aside className="card !p-2 max-h-[600px] overflow-y-auto" data-testid="artifact-list">
        {deliverables.length ? (
          deliverables.map((a) => (
            <FileRow
              key={a.relpath}
              entry={a}
              active={target === a.relpath}
              onClick={() => setSelected(a.relpath)}
            />
          ))
        ) : (
          <p className="text-xs text-ink-400 px-2 py-3" data-testid="artifact-list-empty">
            no artifacts yet
          </p>
        )}
        {(list.data?.length ?? 0) > deliverables.length && (
          <p className="mt-2 border-t border-ink-800 px-2 pt-2 text-[10px] leading-relaxed text-ink-500">
            Hidden telemetry: {(list.data?.length ?? 0) - deliverables.length} transcript, stream,
            profile, or planner diagnostic file{(list.data?.length ?? 0) - deliverables.length === 1 ? "" : "s"}.
          </p>
        )}
      </aside>
      <div className="card max-h-[600px] overflow-auto" data-testid="artifact-content">
        {file.isLoading && <p className="text-sm text-ink-400">loading…</p>}
        {file.error && <p className="text-sm text-red-300">load failed: {String(file.error)}</p>}
        {file.data && (
          <>
            <div className="flex items-center justify-between mb-2 pb-2 border-b border-ink-700" data-testid="artifact-header">
              <code className="text-xs text-ink-300" data-testid="artifact-relpath">{file.data.relpath}</code>
              <span className="text-xs text-ink-500" data-testid="artifact-size">{file.data.size} bytes</span>
            </div>
            {file.data.binary ? (
              <p className="text-sm text-ink-400">[binary file — preview suppressed]</p>
            ) : file.data.kind === "markdown" ? (
              <div className="prose prose-invert prose-sm max-w-none prose-headings:text-ink-100 prose-p:text-ink-200 prose-a:text-ork-amber">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{file.data.content}</ReactMarkdown>
              </div>
            ) : file.data.kind === "json" ? (
              <pre className="text-xs text-ink-200 overflow-auto">
                {tryPretty(file.data.content)}
              </pre>
            ) : (
              <pre className="text-xs text-ink-200 whitespace-pre-wrap">{file.data.content}</pre>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function FileRow({
  entry,
  active,
  onClick,
}: {
  entry: ArtifactEntry;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      data-testid={`artifact-file-${entry.relpath.replace(/[^a-z0-9]/gi, "-")}`}
      data-active={active}
      data-kind={entry.kind}
      className={`w-full text-left px-2 py-1.5 rounded text-xs font-mono ${
        active ? "bg-ink-700 text-ink-50" : "text-ink-300 hover:bg-ink-800"
      }`}
    >
      <div className="truncate">{entry.relpath}</div>
      <div className="text-[10px] text-ink-500">
        {entry.kind} · {entry.size}b
      </div>
    </button>
  );
}

function tryPretty(s: string): string {
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch {
    return s;
  }
}

function isDeliverableArtifact(entry: ArtifactEntry): boolean {
  const name = entry.name.toLowerCase();
  if (name.startsWith(".")) return false;
  if (name.includes(".transcript.")) return false;
  if (name.endsWith(".stream.jsonl")) return false;
  if (name.startsWith("run_profile") || name.startsWith("profile-answers")) return false;
  if (name.startsWith("plan-failure-")) return false;
  if (name === "plan.json") return false;
  return true;
}
