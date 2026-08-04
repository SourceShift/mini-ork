import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";

import { api, type RunInput } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { RunInputContent } from "./RunInputContent";

export function RunInputModal({
  taskRunId,
  input,
  onClose,
}: {
  taskRunId: string;
  input: RunInput | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["run-input", taskRunId, input?.key],
    queryFn: () => api.input(taskRunId, input!.key),
    enabled: !!input,
  });

  useEffect(() => {
    if (!input) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [input, onClose]);

  if (!input) return null;
  const doc = q.data;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`${input.label} input preview`}
      data-testid="run-input-modal"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-ink-700 bg-ink-950 shadow-2xl">
        <header className="flex items-start gap-3 border-b border-ink-700 px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold text-ink-100">{input.label}</h2>
              <span className="pill-muted">{input.kind}</span>
            </div>
            <div className="mt-1 grid grid-cols-1 gap-1 text-xs text-ink-500 md:grid-cols-3">
              <span className="truncate">{input.name}</span>
              <span>{formatRelative(input.mtime)}</span>
              <code className="truncate">{input.path}</code>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-2 text-ink-400 hover:bg-ink-800 hover:text-ink-100"
            aria-label="Close input preview"
            data-testid="run-input-modal-close"
          >
            <X size={18} />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-auto p-4" data-testid="run-input-modal-content">
          {q.isLoading && <p className="text-sm text-ink-400">loading input…</p>}
          {q.error && <p className="text-sm text-red-300">{String(q.error)}</p>}
          {doc && <RunInputContent kind={doc.kind} content={doc.content} />}
        </div>
      </div>
    </div>
  );
}
