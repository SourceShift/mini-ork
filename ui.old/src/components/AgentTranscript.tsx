import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronDown, ChevronRight, MessageSquare, Wrench } from "lucide-react";

import type { AgentDetail, AgentTranscript, AgentTurn } from "@/lib/api";
import { formatCost, formatDuration, formatRelative, formatTokens } from "@/lib/format";
import { FamilyPill, StatusPill } from "@/components/Pill";

type LlmCallRow = AgentDetail["llm_calls"][number];

export function AgentTranscriptPanel({
  transcript,
  calls,
  running = false,
}: {
  transcript: AgentTranscript;
  calls: LlmCallRow[];
  running?: boolean;
}) {
  const callsByTurn = new Map<number, LlmCallRow>();
  for (const c of calls) {
    const turn = parseMeta(c.metadata_json).turn_index;
    if (turn != null && !callsByTurn.has(Number(turn))) callsByTurn.set(Number(turn), c);
  }
  const matchedIds = new Set<number>();
  if (transcript.available) {
    for (const t of transcript.turns) {
      const c = callsByTurn.get(t.turn_index);
      if (c) matchedIds.add(c.id);
    }
  }
  const orphanCalls = calls.filter((c) => !matchedIds.has(c.id));
  const totalCost = calls.reduce((acc, c) => acc + c.cost_usd, 0);

  return (
    <section className="card" data-testid="agent-transcript-section">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-ink-200 flex items-center gap-1.5">
          <MessageSquare size={14} /> Agent transcript
          {transcript.available && (
            <span className="font-normal text-ink-400">
              ({transcript.turns.length} turn{transcript.turns.length === 1 ? "" : "s"})
            </span>
          )}
          {calls.length > 0 && (
            <span className="font-normal text-xs text-ink-400 ml-2" data-testid="agent-llm-summary">
              {calls.length} LLM call{calls.length === 1 ? "" : "s"} · total cost {formatCost(totalCost)}
            </span>
          )}
        </h3>
        <div className="flex items-center gap-3">
          {transcript.truncated && (
            <span className="text-xs text-ork-amber">truncated for size</span>
          )}
          {transcript.fallback === "text-output" && (
            <span className="text-xs text-ink-400">final output fallback</span>
          )}
        </div>
      </div>

      {running && (
        <div
          className="mb-3 flex items-center gap-2 rounded-md border border-ork-amber/30 bg-ork-amber/5 px-3 py-2"
          data-testid="agent-inflight-banner"
        >
          <span className="h-2 w-2 rounded-full bg-ork-amber animate-pulse" />
          <p className="text-xs text-ink-300">
            Agent in flight — LLM calls are ledgered when each call completes, so turns
            appear here as they finish (auto-refreshes every 5s).
          </p>
        </div>
      )}

      {!transcript.available && !running && (
        <p className="text-sm text-ink-500 mb-3">
          {transcript.reason ?? "no transcript captured"}.
          Rich turn transcripts require <code className="text-ink-300">MO_TRACE_RICH=1</code>{" "}
          and a stream-json-capable provider. Codex / Gemini / gateway agents should still
          show a final-output transcript fallback on new runs.
        </p>
      )}

      {transcript.available && transcript.turns.length === 0 && calls.length === 0 && (
        <p className="text-sm text-ink-500">
          No assistant turns recorded — the agent dispatched but didn't emit any
          messages (likely a connection failure or empty response).
        </p>
      )}

      {!transcript.available && calls.length === 0 && !running && (
        <p className="text-sm text-ink-500">
          No LLM calls attributed to this agent. Either (a) this is a non-LLM node (verifier /
          publisher / rollback), (b) the run predates <code>lib/llm-dispatch.sh</code>'s llm_calls
          emission, or (c) the dispatch was through a path that bypasses the shim (e.g. a custom
          Python SDK call inside the agent process — those aren't visible to the outer envelope).
        </p>
      )}

      {transcript.available && transcript.turns.length > 0 && (
        <ol className="space-y-2">
          {transcript.turns.map((t) => (
            <TurnCard key={t.turn_index} turn={t} call={callsByTurn.get(t.turn_index)} />
          ))}
        </ol>
      )}

      {orphanCalls.length > 0 && (
        <div className="mt-3" data-testid="agent-llm-orphan-calls">
          {transcript.available && transcript.turns.length > 0 && (
            <div className="text-[10px] uppercase tracking-wide text-ink-400 mb-1.5">
              telemetry-only calls (no transcript turn)
            </div>
          )}
          <ul className="space-y-1.5">
            {orphanCalls.map((c) => (
              <CallRow key={c.id} call={c} />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function CallTelemetry({ call }: { call: LlmCallRow }) {
  const meta = parseMeta(call.metadata_json);
  const cache =
    Number(meta.cache_read_input_tokens ?? 0) + Number(meta.cache_creation_input_tokens ?? 0);
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs font-mono text-ink-400"
      data-testid={`agent-llm-row-${call.id}`}
      data-status={call.status}
    >
      <span className="flex items-center gap-1.5">
        <FamilyPill family={call.provider} />
        <code className="text-ink-300">{call.model_id}</code>
      </span>
      <span>{formatTokens(call.input_tokens)} in / {formatTokens(call.output_tokens)} out</span>
      {cache > 0 && <span className="text-ork-amber">cache {formatTokens(cache)}</span>}
      <span>{formatCost(call.cost_usd)}</span>
      <span>{formatDuration(call.duration_ms)}</span>
      <span>{formatRelative(call.ts)}</span>
      {call.status !== "success" && <StatusPill status={call.status} />}
      {call.error_message && (
        <span className="text-red-300 max-w-xs truncate" title={call.error_message}>
          {call.error_message}
        </span>
      )}
    </div>
  );
}

function CallRow({ call }: { call: LlmCallRow }) {
  const meta = parseMeta(call.metadata_json);
  return (
    <li className="border border-ink-700 rounded-md px-3 py-2 bg-ink-900/40">
      <div className="flex items-center gap-2 mb-1">
        <span className="font-mono text-xs text-ink-200">
          {meta.turn_index != null ? `turn #${meta.turn_index}` : "envelope"}
        </span>
        {meta.stop_reason && (
          <span className="text-xs text-ink-500">stop: {meta.stop_reason}</span>
        )}
      </div>
      <CallTelemetry call={call} />
    </li>
  );
}

function TurnCard({ turn, call }: { turn: AgentTurn; call?: LlmCallRow }) {
  const [open, setOpen] = useState(turn.turn_index === 0);
  const cache =
    (turn.cache_read_input_tokens ?? 0) + (turn.cache_creation_input_tokens ?? 0);
  return (
    <li
      className="border border-ink-700 rounded-md overflow-hidden"
      data-testid={`agent-turn-${turn.turn_index}`}
    >
      <button
        onClick={() => setOpen((x) => !x)}
        className="w-full text-left px-3 py-2 bg-ink-900/40 hover:bg-ink-800 flex items-center gap-2 flex-wrap"
        data-testid={`agent-turn-${turn.turn_index}-toggle`}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="font-mono text-xs text-ink-200">turn #{turn.turn_index}</span>
        {turn.model && <span className="text-xs text-ink-400">{turn.model}</span>}
        <span className="text-xs text-ink-500 font-mono">
          {formatTokens(turn.input_tokens)} in / {formatTokens(turn.output_tokens)} out
          {cache > 0 && <span className="text-ork-amber"> · cache {formatTokens(cache)}</span>}
        </span>
        {call && (
          <span className="text-xs text-ink-400 font-mono">
            {formatCost(call.cost_usd)} · {formatDuration(call.duration_ms)}
          </span>
        )}
        {call && call.status !== "success" && <StatusPill status={call.status} />}
        {turn.stop_reason && (
          <span className="text-xs text-ink-500 ml-auto">stop: {turn.stop_reason}</span>
        )}
        {turn.tool_uses.length > 0 && (
          <span className="text-xs text-ork-amber flex items-center gap-1">
            <Wrench size={10} /> {turn.tool_uses.length} tool call{turn.tool_uses.length === 1 ? "" : "s"}
          </span>
        )}
      </button>
      {open && (
        <div className="p-3 space-y-3" data-testid={`agent-turn-${turn.turn_index}-body`}>
          {call && (call.status !== "success" || call.error_message) && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-ink-400 mb-1">
                llm call telemetry
              </div>
              <CallTelemetry call={call} />
            </div>
          )}
          {turn.text && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-ink-400 mb-1">
                assistant text
              </div>
              <div className="prose prose-invert prose-sm max-w-none prose-headings:text-ink-100 prose-p:text-ink-200 prose-a:text-ork-amber bg-ink-900/40 p-3 rounded border border-ink-800 overflow-auto max-h-[400px]">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.text}</ReactMarkdown>
              </div>
            </div>
          )}
          {turn.tool_uses.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-ink-400 mb-1 flex items-center gap-1">
                <Wrench size={10} /> tool uses
              </div>
              <ul className="space-y-1.5">
                {turn.tool_uses.map((tu) => (
                  <li
                    key={tu.id}
                    className="bg-ink-900/40 p-2 rounded border border-ink-800 text-xs"
                    data-testid={`agent-turn-${turn.turn_index}-tool-${tu.name}`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <code className="text-ork-amber">{tu.name}</code>
                      <span className="text-ink-500 font-mono">{tu.id?.slice(0, 12)}…</span>
                    </div>
                    <pre className="text-[10px] text-ink-300 overflow-auto max-h-32">
                      {JSON.stringify(tu.input, null, 2)}
                    </pre>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {turn.session_id && (
            <div className="text-[10px] text-ink-500 font-mono">
              session: {turn.session_id}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function parseMeta(s: string | null | undefined): Record<string, any> {
  if (!s) return {};
  try {
    return JSON.parse(s) || {};
  } catch {
    return {};
  }
}
