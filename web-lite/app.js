// mini-ork · lite — zero-build Preact + htm + xterm observability/terminal UI.
//
// No bundler, no node_modules: every dependency is a pinned ESM import from a
// CDN, so `mini-ork serve` can hand this out as static files. To add a view:
//   1. write a component below
//   2. add one { pattern, view } row to ROUTES
//   3. (optional) add a nav link in <Nav/>

import { h, render } from "https://esm.sh/preact@10.24.3";
import {
  useState,
  useEffect,
  useRef,
} from "https://esm.sh/preact@10.24.3/hooks";
import htm from "https://esm.sh/htm@3.1.1";
import { Terminal } from "https://esm.sh/@xterm/xterm@5.5.0";
import { FitAddon } from "https://esm.sh/@xterm/addon-fit@0.10.0";
import { api, ptyURL } from "./api.js";

const html = htm.bind(h);

// ── helpers ──────────────────────────────────────────────────────────────
function parseHash() {
  const raw = location.hash || "#/";
  const [path, qs = ""] = raw.split("?");
  return { path, query: new URLSearchParams(qs) };
}

function useHash() {
  const [hash, setHash] = useState(location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(location.hash || "#/");
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return hash;
}

// Fetch-on-mount with optional polling. Returns {loading, data, error}.
function useAsync(fn, deps = [], pollMs = 0) {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await fn();
        if (alive) setState({ loading: false, data, error: null });
      } catch (e) {
        if (alive) setState({ loading: false, data: null, error: String(e) });
      }
    };
    load();
    const t = pollMs ? setInterval(load, pollMs) : null;
    return () => {
      alive = false;
      if (t) clearInterval(t);
    };
    // eslint-disable-next-line
  }, deps);
  return state;
}

const fmtCost = (c) => (c == null ? "—" : `$${Number(c).toFixed(3)}`);
const fmtDur = (ms) => {
  if (!ms) return "—";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s - m * 60)}s`;
};
const fmtTime = (ts) => {
  if (!ts) return "—";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  return isNaN(d) ? String(ts) : d.toLocaleString();
};
const Badge = ({ v }) =>
  html`<span class=${"badge " + (v || "").toLowerCase()}>${v || "—"}</span>`;

function Loading({ s, children }) {
  if (s.loading) return html`<p class="loading">loading…</p>`;
  if (s.error) return html`<p class="err">${s.error}</p>`;
  return children(s.data);
}

// ── views ────────────────────────────────────────────────────────────────
function RunsView() {
  const runs = useAsync(() => api.taskRuns(100), [], 5000);
  const sum = useAsync(() => api.summary(), [], 5000);
  return html`
    <h1>Runs</h1>
    <p class="sub">Every task-run this workspace has executed. Polls every 5s.</p>
    <${Loading} s=${sum}>
      ${(d) => html`
        <div class="stats">
          <div class="stat">
            <div class="n">${(d.by_status || []).reduce((a, x) => a + x.count, 0)}</div>
            <div class="l">total runs</div>
          </div>
          ${(d.by_status || []).slice(0, 5).map(
            (x) => html`
              <div class="stat">
                <div class="n">${x.count}</div>
                <div class="l">${x.status || "—"}</div>
              </div>
            `,
          )}
          <div class="stat">
            <div class="n">${fmtCost(d.total_cost_usd)}</div>
            <div class="l">total spend</div>
          </div>
        </div>
      `}
    <//>
    <${Loading} s=${runs}>
      ${(rows) =>
        !rows.length
          ? html`<p class="muted">No runs yet.</p>`
          : html`
              <table>
                <thead>
                  <tr>
                    <th>run</th>
                    <th>recipe</th>
                    <th>status</th>
                    <th>verdict</th>
                    <th>cost</th>
                    <th>dur</th>
                    <th>created</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map(
                    (r) => html`
                      <tr>
                        <td>
                          <a href=${`#/run/${encodeURIComponent(r.id)}`}>${r.id}</a>
                        </td>
                        <td class="name">${r.recipe || r.task_class || "—"}</td>
                        <td><${Badge} v=${r.status} /></td>
                        <td><${Badge} v=${r.verdict} /></td>
                        <td>${fmtCost(r.cost_usd)}</td>
                        <td>${fmtDur(r.duration_ms)}</td>
                        <td class="muted">${fmtTime(r.created_at)}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            `}
    <//>
  `;
}

function RunDetailView({ id }) {
  const run = useAsync(() => api.run(id), [id], 4000);
  const dag = useAsync(() => api.dag(id), [id], 4000);
  const agents = useAsync(() => api.agents(id), [id], 4000);
  return html`
    <div class="row" style="justify-content:space-between">
      <div>
        <h1>${id}</h1>
        <p class="sub">Stage pipeline + dispatched agents. Polls every 4s.</p>
      </div>
      <div class="row">
        <a class="btn" href="#/">← runs</a>
        <a class="btn" href=${`#/terminal?run=${encodeURIComponent(id)}`}>shell into run ⟩</a>
      </div>
    </div>

    <${Loading} s=${run}>
      ${(r) => html`
        <div class="stats">
          <div class="stat"><div class="n"><${Badge} v=${r.status} /></div><div class="l">status</div></div>
          <div class="stat"><div class="n"><${Badge} v=${r.verdict} /></div><div class="l">verdict</div></div>
          <div class="stat"><div class="n">${fmtCost(r.cost_usd)}</div><div class="l">cost</div></div>
          <div class="stat"><div class="n">${fmtDur(r.duration_ms)}</div><div class="l">duration</div></div>
          <div class="stat"><div class="n">${r.recipe || "—"}</div><div class="l">recipe</div></div>
        </div>
      `}
    <//>

    <div class="section">Stages</div>
    <${Loading} s=${dag}>
      ${(d) => html`
        <div class="pipeline">
          ${(d.nodes || []).map(
            (n) => html`
              <div class=${"node " + (n.status || "never_seen")}>
                <div class="nname">${n.name}</div>
                <div class="ntype">${n.type || ""} · ${n.status || "—"}</div>
                ${n.duration_ms ? html`<div class="ntype">${fmtDur(n.duration_ms)}</div>` : ""}
              </div>
            `,
          )}
        </div>
      `}
    <//>

    <div class="section">Agents</div>
    <${Loading} s=${agents}>
      ${(d) => {
        const list = d.agents || d || [];
        return !list.length
          ? html`<p class="muted">No agents dispatched yet.</p>`
          : html`
              <table>
                <thead>
                  <tr><th>node</th><th>role</th><th>lane</th><th>status</th><th>cost</th><th>calls</th></tr>
                </thead>
                <tbody>
                  ${list.map(
                    (a) => html`
                      <tr>
                        <td>${a.node_id || a.node || a.name || "—"}</td>
                        <td class="name">${a.role || a.agent_role || "—"}</td>
                        <td>${a.lane || a.model || "—"}</td>
                        <td><${Badge} v=${a.status || a.verdict} /></td>
                        <td>${fmtCost(a.cost_usd)}</td>
                        <td>${a.llm_calls ?? a.calls ?? "—"}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            `;
      }}
    <//>
  `;
}

function LearningsView() {
  const bandit = useAsync(() => api.bandit(), [], 10000);
  const gepa = useAsync(() => api.gepa(), [], 10000);
  return html`
    <h1>Learnings</h1>
    <p class="sub">
      The router's learned lane policy (contextual bandit) and GEPA prompt
      evolution outcomes — what the loop actually learned.
    </p>

    <div class="section">Lane advantage — what the router trusts</div>
    <${Loading} s=${bandit}>
      ${(d) => {
        const rows = (d.domain || []).slice(0, 40);
        return !rows.length
          ? html`<p class="muted">No lane advantage recorded yet.</p>`
          : html`
              <table>
                <thead>
                  <tr><th>task class</th><th>node</th><th>domain</th><th>rel. advantage</th><th>runs</th><th>wins</th></tr>
                </thead>
                <tbody>
                  ${rows.map(
                    (r) => html`
                      <tr>
                        <td class="name">${r.task_class}</td>
                        <td>${r.node_type}</td>
                        <td>${r.objective_domain}</td>
                        <td>${Number(r.relative_advantage).toFixed(3)}</td>
                        <td>${r.runs_count}</td>
                        <td>${r.success_count}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            `;
      }}
    <//>

    <div class="section">GEPA — prompt evolution outcomes</div>
    <${Loading} s=${gepa}>
      ${(d) => html`
        <div class="stats">
          <div class="stat"><div class="n">${d.gradient_count ?? 0}</div><div class="l">gradients</div></div>
          <div class="stat"><div class="n">${(d.win_rates || []).length}</div><div class="l">prompt variants</div></div>
          <div class="stat"><div class="n">${(d.promotions || []).length}</div><div class="l">promotions</div></div>
        </div>
        ${(d.win_rates || []).length
          ? html`
              <table>
                <thead>
                  <tr><th>task class</th><th>role</th><th>node</th><th>win rate</th><th>w/l/t</th><th>n</th></tr>
                </thead>
                <tbody>
                  ${d.win_rates.slice(0, 30).map(
                    (r) => html`
                      <tr>
                        <td class="name">${r.task_class}</td>
                        <td>${r.agent_role}</td>
                        <td>${r.node_type}</td>
                        <td>${r.win_rate == null ? "—" : Number(r.win_rate).toFixed(2)}</td>
                        <td>${r.wins}/${r.losses}/${r.ties}</td>
                        <td>${r.sample_size}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            `
          : html`<p class="muted">No prompt win-rates yet.</p>`}
      `}
    <//>
  `;
}

function TerminalView() {
  const { query } = parseHash();
  const runId = query.get("run") || "";
  const initialCmd = query.get("cmd") || "shell";
  const [cmd, setCmd] = useState(initialCmd);
  const [status, setStatus] = useState("connecting");
  const [nonce, setNonce] = useState(0);
  const holderRef = useRef(null);

  useEffect(() => {
    const holder = holderRef.current;
    if (!holder) return;
    const term = new Terminal({
      fontFamily: "ui-monospace, Menlo, monospace",
      fontSize: 13,
      cursorBlink: true,
      theme: { background: "#000000", foreground: "#d7dde5" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(holder);
    fit.fit();

    const ws = new WebSocket(
      ptyURL({ runId, cmd, cols: term.cols, rows: term.rows }),
    );
    ws.binaryType = "arraybuffer";
    setStatus("connecting");
    ws.onopen = () => {
      setStatus("connected");
      term.focus();
    };
    ws.onclose = () => setStatus("closed");
    ws.onerror = () => setStatus("error");
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") term.write(ev.data);
      else term.write(new Uint8Array(ev.data));
    };

    const dataSub = term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) ws.send("0" + d);
    });
    const doResize = () => {
      try {
        fit.fit();
      } catch (_) {}
      if (ws.readyState === WebSocket.OPEN)
        ws.send("1" + JSON.stringify({ cols: term.cols, rows: term.rows }));
    };
    const ro = new ResizeObserver(doResize);
    ro.observe(holder);

    return () => {
      ro.disconnect();
      dataSub.dispose();
      try {
        ws.close();
      } catch (_) {}
      term.dispose();
    };
    // eslint-disable-next-line
  }, [runId, cmd, nonce]);

  return html`
    <div class="term-bar">
      <span class=${"dot " + status}></span>
      <span>${status}</span>
      <span class="muted">
        ${runId ? `cwd: runs/${runId}` : "cwd: repo root"}
      </span>
      <span class="spacer" style="flex:1"></span>
      <label>
        cmd
        <select
          value=${cmd}
          onChange=${(e) => setCmd(e.currentTarget.value)}
        >
          <option value="shell">shell</option>
          <option value="opencode">opencode</option>
        </select>
      </label>
      <button class="btn" onClick=${() => setNonce((n) => n + 1)}>reconnect</button>
      <a class="btn" href="#/">← runs</a>
    </div>
    <div class="term-holder" ref=${holderRef}></div>
  `;
}

function NotFound() {
  return html`<h1>Not found</h1>
    <p class="muted">No view for <code>${location.hash}</code>.</p>
    <a class="btn" href="#/">← runs</a>`;
}

// ── router ───────────────────────────────────────────────────────────────
const ROUTES = [
  { pattern: /^#\/$/, flush: false, view: () => html`<${RunsView} />` },
  {
    pattern: /^#\/run\/([^/?]+)$/,
    flush: false,
    view: (m) => html`<${RunDetailView} id=${decodeURIComponent(m[1])} />`,
  },
  { pattern: /^#\/learnings$/, flush: false, view: () => html`<${LearningsView} />` },
  { pattern: /^#\/terminal/, flush: true, view: () => html`<${TerminalView} />` },
];

function Nav({ path }) {
  // "Runs" stays lit on run-detail pages too; others match exactly.
  const active = (href) =>
    href === "#/" ? path === "#/" || path.startsWith("#/run/") : path === href;
  const link = (href, label) =>
    html`<a href=${href} class=${active(href) ? "active" : ""}>${label}</a>`;
  return html`
    <nav class="side">
      <div class="brand">mini-ork ·lite</div>
      ${link("#/", "Runs")}
      ${link("#/learnings", "Learnings")}
      ${link("#/terminal", "Terminal")}
      <div class="spacer"></div>
      <div class="foot">observability + shell</div>
    </nav>
  `;
}

function App() {
  const hash = useHash();
  const path = hash.split("?")[0];
  const route = ROUTES.find((r) => r.pattern.test(path));
  const m = route ? path.match(route.pattern) : null;
  return html`
    <div class="layout">
      <${Nav} path=${path} />
      <main class=${"content" + (route && route.flush ? " flush" : "")}>
        ${route ? route.view(m) : html`<${NotFound} />`}
      </main>
    </div>
  `;
}

render(html`<${App} />`, document.getElementById("app"));
