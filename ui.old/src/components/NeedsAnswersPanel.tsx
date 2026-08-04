import { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, Loader2, Send, Terminal } from "lucide-react";

import { api } from "@/lib/api";

export function NeedsAnswersPanel({ taskRunId }: { taskRunId: string }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["profile", taskRunId],
    queryFn: () => api.profile(taskRunId),
    refetchInterval: 10_000,
  });

  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [success, setSuccess] = useState<{ next_step_cli: string; note: string } | null>(null);

  // Seed from existing answers when data loads
  useEffect(() => {
    if (q.data?.answers && Object.keys(q.data.answers).length > 0) {
      setAnswers((prev) => ({ ...q.data!.answers, ...prev }));
    }
  }, [q.data?.answers]);

  const save = useMutation({
    mutationFn: () => api.saveAnswers(taskRunId, answers),
    onSuccess: (r) => {
      setSuccess({ next_step_cli: r.next_step_cli, note: r.note });
      qc.invalidateQueries({ queryKey: ["profile", taskRunId] });
      qc.invalidateQueries({ queryKey: ["task-run", taskRunId] });
    },
  });

  if (q.isLoading) return null;
  if (!q.data) return null;

  // Only show the panel when there's something for the user to do
  if (!q.data.needs_answers && !success) {
    if (q.data.profile_status && q.data.profile_status !== "ready") {
      // Show benign status (e.g., "ready", "no_profile") in a small card
      return null;
    }
    return null;
  }

  const questions = q.data.questions.map((qq) =>
    typeof qq === "string" ? qq : qq.text ?? qq.question ?? JSON.stringify(qq),
  );

  return (
    <section
      className="card border-l-4 border-ork-amber"
      data-testid="needs-answers-panel"
    >
      <div className="flex items-center gap-2 mb-3">
        <AlertCircle size={16} className="text-ork-amber" />
        <h3 className="text-sm font-semibold text-ink-100">
          Planner needs your input
        </h3>
        <span className="text-xs text-ink-400 ml-auto font-mono">
          confidence: {Math.round((q.data.confidence ?? 0) * 100)}%
        </span>
      </div>

      <p className="text-xs text-ink-400 mb-4">
        The kickoff was ambiguous on these points. Answer below to unblock the
        dispatcher. The CLI also prompts interactively when run on a TTY —
        either path is fine.
      </p>

      {success ? (
        <div data-testid="needs-answers-success">
          <div className="flex items-center gap-2 text-ork-green mb-3">
            <Check size={14} /> Answers saved.
          </div>
          <div className="text-xs text-ink-300 mb-2">{success.note}</div>
          <div className="bg-ink-900/60 border border-ink-700 rounded p-2 flex items-start gap-2 font-mono text-xs">
            <Terminal size={12} className="mt-0.5 flex-shrink-0 text-ink-400" />
            <span className="text-ink-200">{success.next_step_cli}</span>
          </div>
        </div>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
          className="space-y-3"
          data-testid="needs-answers-form"
        >
          {questions.map((question, i) => (
            <label key={i} className="block" data-testid={`needs-answers-q-${i}`}>
              <span className="text-sm text-ink-200 block mb-1">
                <span className="text-ink-500 font-mono">Q{i + 1}.</span> {question}
              </span>
              <textarea
                rows={2}
                value={answers[question] ?? ""}
                onChange={(e) =>
                  setAnswers((prev) => ({ ...prev, [question]: e.target.value }))
                }
                placeholder="your answer…"
                data-testid={`needs-answers-input-${i}`}
                className="w-full px-3 py-2 bg-ink-900 border border-ink-700 rounded text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none focus:border-ork-amber resize-y"
              />
            </label>
          ))}
          <div className="flex items-center justify-between">
            <span className="text-xs text-ink-500">
              {Object.values(answers).filter(Boolean).length} of {questions.length} answered
            </span>
            <button
              type="submit"
              disabled={
                save.isPending ||
                Object.values(answers).filter(Boolean).length === 0
              }
              data-testid="needs-answers-submit"
              className="px-3 py-1.5 rounded text-xs font-medium border border-ork-amber/40 bg-ork-amber/10 text-ork-amber hover:bg-ork-amber/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
            >
              {save.isPending ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Send size={12} />
              )}
              Submit answers
            </button>
          </div>
          {save.error && (
            <p className="text-xs text-red-300" data-testid="needs-answers-error">
              {String(save.error)}
            </p>
          )}
        </form>
      )}
    </section>
  );
}
