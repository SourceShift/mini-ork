/** Small formatting helpers shared across views. */

export function formatCost(usd: number | null | undefined): string {
  if (usd == null || isNaN(usd)) return "—";
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  if (usd < 1) return `$${usd.toFixed(3)}`;
  return `$${usd.toFixed(2)}`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || ms <= 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = s / 60;
  if (m < 60) return `${m.toFixed(1)}m`;
  const h = m / 60;
  return `${h.toFixed(2)}h`;
}

export function formatRelative(ts: number | string | null | undefined): string {
  if (ts == null) return "—";
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  if (isNaN(t)) return String(ts);
  const diff = Date.now() - t;
  if (diff < 1000) return "just now";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export function formatTokens(n: number | null | undefined): string {
  if (!n) return "—";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export const FAMILY_COLORS: Record<string, string> = {
  opus: "#a855f7",      // Anthropic Opus — purple
  sonnet: "#7c3aed",    // Anthropic Sonnet — slightly darker purple
  glm: "#f43f5e",       // Zhipu GLM — rose
  kimi: "#06b6d4",      // Moonshot Kimi — cyan
  codex: "#22c55e",     // OpenAI Codex — green
  deepseek: "#3b82f6",  // DeepSeek — blue
  minimax: "#f59e0b",   // MiniMax — amber
  gemini: "#10b981",    // Google Gemini — emerald
};

export function familyColor(family: string | null | undefined): string {
  if (!family) return "#64748b"; // slate-500
  return FAMILY_COLORS[family.toLowerCase()] ?? "#64748b";
}

export function statusPillClass(status: string | null | undefined): string {
  switch (status) {
    case "published":
    case "success":
    case "converged":
    case "APPROVE":
    case "pass":
      return "pill-ok";
    case "executing":
    case "verifying":
    case "reviewing":
    case "planned":
    case "in_progress":
    case "pending":
    case "running":
    case "partial":
    // vacuous = zero verifiers executed: not a pass, not a failure — amber
    // so it reads as "needs attention" rather than laundering into green.
    case "vacuous":
      return "pill-warn";
    case "failed":
    case "failure":
    case "fail":
    case "rolled_back":
    case "aborted":
    case "timed_out":
    case "CRASH":
    case "REQUEST_CHANGES":
    case "ESCALATE":
      return "pill-err";
    default:
      return "pill-muted";
  }
}
