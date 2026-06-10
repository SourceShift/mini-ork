import type React from "react";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api, type ArtifactEntry } from "@/lib/api";

export function ArtifactViewer({
  taskRunId,
  initialSelection,
  agentByFile = {},
}: {
  taskRunId: string;
  initialSelection?: string | null;
  agentByFile?: Record<string, string>;
}) {
  const list = useQuery({
    queryKey: ["artifacts", taskRunId],
    queryFn: () => api.artifacts(taskRunId),
    refetchInterval: 5_000,
  });
  const [selected, setSelected] = useState<string | null>(null);

  const deliverables = (list.data ?? []).filter(isDeliverableArtifact);
  const runFiles = deliverables.filter((a) => !agentByFile[a.relpath]);
  const agentFiles = deliverables.filter((a) => agentByFile[a.relpath]);
  const initial =
    (initialSelection && deliverables.some((a) => a.relpath === initialSelection)
      ? initialSelection
      : null) ?? deliverables[0]?.relpath ?? null;
  const target = selected ?? initial;
  const file = useQuery({
    queryKey: ["artifact", taskRunId, target],
    queryFn: () => api.artifact(taskRunId, target!),
    enabled: !!target,
  });

  return (
    <div className="grid grid-cols-[240px_1fr] gap-4" data-testid="artifact-viewer">
      <aside className="card !p-2 max-h-[600px] overflow-y-auto" data-testid="artifact-list">
        {runFiles.length > 0 && <GroupLabel>run artifacts</GroupLabel>}
        {runFiles.map((a) => (
          <FileRow
            key={a.relpath}
            entry={a}
            label={artifactLabel(a.relpath)}
            active={target === a.relpath}
            onClick={() => setSelected(a.relpath)}
          />
        ))}
        {agentFiles.length > 0 && <GroupLabel>agent outputs</GroupLabel>}
        {agentFiles.map((a) => (
          <FileRow
            key={a.relpath}
            entry={a}
            badge={agentByFile[a.relpath]}
            active={target === a.relpath}
            onClick={() => setSelected(a.relpath)}
          />
        ))}
        {!deliverables.length && (
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

function GroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2 pt-2 pb-1 text-[9.5px] font-black uppercase tracking-[0.13em] text-ink-500">
      {children}
    </div>
  );
}

function FileRow({
  entry,
  active,
  onClick,
  label,
  badge,
}: {
  entry: ArtifactEntry;
  active: boolean;
  onClick: () => void;
  label?: string;
  badge?: string;
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
      <div className="truncate">{label ?? entry.relpath}</div>
      <div className="text-[10px] text-ink-500 truncate">
        {badge ? <span className="text-ork-amber">{badge} · </span> : null}
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

export function isDeliverableArtifact(entry: ArtifactEntry): boolean {
  const name = entry.name.toLowerCase();
  if (name.startsWith(".")) return false;
  if (name.includes(".transcript.")) return false;
  if (name.endsWith(".stream.jsonl")) return false;
  if (name.startsWith("run_profile") || name.startsWith("profile-answers")) return false;
  if (name.startsWith("plan-failure-")) return false;
  if (name === "plan.json") return false;
  if (name === "trace-write-errors.log") return false;
  if (entry.relpath.startsWith("llm-failures/")) return false;
  return true;
}

const ARTIFACT_LABELS: Record<string, string> = {
  "execute.log": "Execution log",
  "rubric.json": "Rubric",
  "panel-verdict.json": "Panel verdict",
  "context-pack.json": "Context pack",
  "synthesis.md": "Synthesis",
};

export function artifactLabel(relpath: string): string {
  if (ARTIFACT_LABELS[relpath]) return ARTIFACT_LABELS[relpath];
  if (relpath.startsWith("evidence/")) return `Evidence: ${relpath.slice("evidence/".length)}`;
  return relpath;
}
