// Thin fetch layer over the mini-ork observability API (/api/v1/*).
// Add a method here + a view in app.js = a new panel. That's the whole
// "fast to extend" contract.

const BASE = "/api/v1";

async function j(path) {
  const r = await fetch(BASE + path, { headers: { accept: "application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${path}`);
  return r.json();
}

export const api = {
  health: () => j("/health"),
  summary: () => j("/task-runs/summary"),
  taskRuns: (limit = 100) => j(`/task-runs?limit=${limit}`),
  run: (id) => j(`/task-runs/${encodeURIComponent(id)}`),
  dag: (id) => j(`/task-runs/${encodeURIComponent(id)}/dag`),
  agents: (id) => j(`/task-runs/${encodeURIComponent(id)}/agents`),
  events: (id, limit = 80) =>
    j(`/task-runs/${encodeURIComponent(id)}/events?limit=${limit}`),
  bandit: () => j("/learning/bandit"),
  gepa: () => j("/learning/gepa"),
};

// WebSocket URL for the server-side PTY (opt-in via MO_PTY_ENABLED=1).
export function ptyURL({ runId, cmd = "shell", cols = 80, rows = 24 }) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const p = new URLSearchParams({ cmd, cols: String(cols), rows: String(rows) });
  if (runId) p.set("run_id", runId);
  return `${proto}://${location.host}${BASE}/pty?${p.toString()}`;
}
