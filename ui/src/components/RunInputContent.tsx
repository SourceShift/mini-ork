import type React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function RunInputContent({ kind, content }: { kind: string; content: string }) {
  if (kind === "plan") return <PlanView content={content} />;
  if (kind === "markdown") {
    return (
      <div className="prose prose-invert prose-sm max-w-none prose-headings:text-ink-100 prose-p:text-ink-200 prose-a:text-ork-amber">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </div>
    );
  }
  return (
    <pre className="text-xs text-ink-200 overflow-auto whitespace-pre-wrap">
      {prettyJson(content)}
    </pre>
  );
}

function PlanView({ content }: { content: string }) {
  const plan = parsePlan(content);
  if (!plan) {
    return <pre className="text-xs text-ink-200 whitespace-pre-wrap">{content}</pre>;
  }
  return (
    <div className="space-y-4" data-testid="plan-view">
      <section className="rounded-md border border-ink-700 bg-ink-900/30 p-4">
        <div className="text-xs uppercase tracking-wide text-ink-500 mb-1">objective</div>
        <p className="text-ink-100 text-lg leading-relaxed">{String(plan.objective ?? "—")}</p>
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <PlanList title="Assumptions" items={asStrings(plan.assumptions)} />
        <PlanList title="Risk notes" items={asStrings(plan.risk_notes)} />
      </section>

      <section className="rounded-md border border-ink-700 bg-ink-900/30 p-4">
        <h2 className="text-sm font-semibold text-ink-200 mb-3">Decomposition</h2>
        <ol className="space-y-2">
          {asRecords(plan.decomposition).map((step, index) => (
            <li key={`${step.id ?? index}`} className="border border-ink-700 rounded-md p-3">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-mono text-xs text-ork-amber">#{index + 1}</span>
                <code className="text-ink-100">{String(step.id ?? "step")}</code>
                <span className="pill-muted">{String(step.node_type ?? "node")}</span>
              </div>
              <p className="text-sm text-ink-300 mt-2">{String(step.description ?? "")}</p>
              {Array.isArray(step.depends_on) && step.depends_on.length > 0 && (
                <p className="text-xs text-ink-500 mt-2">
                  depends on: <code>{step.depends_on.join(", ")}</code>
                </p>
              )}
            </li>
          ))}
        </ol>
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <PlanList
          title="Outputs"
          items={asStrings(asRecord(plan.artifact_contract).outputs)}
          empty="No output files declared."
        />
        <PlanList
          title="Verifier checks"
          items={asRecords(asRecord(plan.verifier_contract).checks).map((c) =>
            `${String(c.id ?? "check")}: ${String(c.description ?? c.command ?? "")}`,
          )}
          empty="No verifier checks declared."
        />
      </section>
    </div>
  );
}

function PlanList({ title, items, empty = "None." }: { title: string; items: string[]; empty?: string }) {
  return (
    <section className="rounded-md border border-ink-700 bg-ink-900/30 p-4">
      <h2 className="text-sm font-semibold text-ink-200 mb-3">{title}</h2>
      {items.length ? (
        <ul className="space-y-2 text-sm text-ink-300">
          {items.map((item, i) => (
            <li key={`${item}-${i}`} className="flex gap-2">
              <span className="text-ork-amber">•</span>
              <span>{item}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-ink-500">{empty}</p>
      )}
    </section>
  );
}

function prettyJson(content: string) {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

function parsePlan(content: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(content);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function asRecord(value: unknown): Record<string, any> {
  return typeof value === "object" && value !== null ? (value as Record<string, any>) : {};
}

function asRecords(value: unknown): Array<Record<string, any>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, any> => typeof item === "object" && item !== null)
    : [];
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}
