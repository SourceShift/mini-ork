import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { RotateCw, TerminalSquare } from "lucide-react";
import "@xterm/xterm/css/xterm.css";

import { ptyWebsocketUrl } from "@/lib/api";

type ConnState = "connecting" | "open" | "closed" | "disabled" | "error";

// xterm renders to a canvas, so it needs concrete colours (CSS vars won't
// resolve there). These track the app's dark palette closely enough to feel
// like one surface.
const THEME = {
  background: "#0a0c0e",
  foreground: "#c7cdd4",
  cursor: "#e6b04a",
  selectionBackground: "#2a2f36",
  black: "#0a0c0e",
  brightBlack: "#5b636d",
  red: "#f0584e",
  green: "#4fd1a0",
  yellow: "#e6b04a",
  blue: "#58b9c9",
  magenta: "#9b8cdf",
  cyan: "#58b9c9",
  white: "#c7cdd4",
};

/** Live shell into a run (or opencode) over the opt-in PTY bridge. The server
 * gates on MO_PTY_ENABLED=1 and closes with code 4403 when it's off — we surface
 * that as an actionable "disabled" state rather than a dead black box.
 *
 * `autoType` types a command into the shell WITHOUT pressing enter: this is the
 * Phase-1 promise made honest — the composer hands the exact command here, the
 * terminal types it, and the human decides whether to run it. */
export function TerminalPanel({
  runId,
  cmd = "shell",
  autoType,
  className,
}: {
  runId?: string;
  cmd?: "shell" | "opencode";
  autoType?: string;
  className?: string;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<ConnState>("connecting");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    setState("connecting");
    const term = new Terminal({
      theme: THEME,
      fontFamily: "var(--mono, ui-monospace), SFMono-Regular, Menlo, monospace",
      fontSize: 12,
      cursorBlink: true,
      scrollback: 5000,
      convertEol: false,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    try {
      fit.fit();
    } catch {
      // host not laid out yet — the ResizeObserver below will fit on first frame
    }

    const url = ptyWebsocketUrl({ runId, cmd, cols: term.cols, rows: term.rows });
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    const decoder = new TextDecoder();

    ws.onopen = () => {
      setState("open");
      pushResize();
      if (autoType) ws.send("0" + autoType);
      term.focus();
    };
    ws.onmessage = (ev) => {
      // string frame = the server's out-of-band notice (e.g. [pty disabled]);
      // ArrayBuffer = raw terminal output.
      if (typeof ev.data === "string") term.write(ev.data);
      else term.write(decoder.decode(new Uint8Array(ev.data as ArrayBuffer)));
    };
    ws.onclose = (ev) => setState(ev.code === 4403 ? "disabled" : "closed");
    ws.onerror = () => setState("error");

    const onData = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send("0" + data);
    });

    function pushResize() {
      try {
        fit.fit();
      } catch {
        return;
      }
      if (ws.readyState === WebSocket.OPEN) {
        ws.send("1" + JSON.stringify({ rows: term.rows, cols: term.cols }));
      }
    }
    const ro = new ResizeObserver(() => pushResize());
    ro.observe(host);

    return () => {
      ro.disconnect();
      onData.dispose();
      try {
        ws.close();
      } catch {
        /* already closing */
      }
      term.dispose();
    };
  }, [runId, cmd, autoType, attempt]);

  return (
    <div className={`card !p-0 overflow-hidden ${className ?? ""}`} data-testid="terminal-panel" data-state={state}>
      <div className="panel-title !m-0 flex items-center gap-2">
        <TerminalSquare size={13} />
        <span>{cmd === "opencode" ? "opencode" : "shell"}{runId ? ` · ${runId.slice(0, 14)}` : ""}</span>
        <StateBadge state={state} />
        <button
          type="button"
          onClick={() => setAttempt((a) => a + 1)}
          className="ml-auto flex items-center gap-1 rounded-[3px] border border-[var(--hair-2)] px-1.5 py-0.5 text-[9.5px] uppercase tracking-[0.08em] text-ink-400 hover:border-[var(--cyan)] hover:text-[var(--cyan)]"
          data-testid="terminal-reconnect"
          title="reconnect"
        >
          <RotateCw size={11} /> reconnect
        </button>
      </div>
      {state === "disabled" && (
        <div className="border-b border-[var(--hair)] bg-[var(--w-amb)] px-3 py-1.5 text-[10.5px] text-ork-amber" data-testid="terminal-disabled">
          PTY is off. Start the API with <code className="font-mono">MO_PTY_ENABLED=1 mini-ork serve</code> to enable shell access.
        </div>
      )}
      <div ref={hostRef} className="h-[420px] w-full bg-[#0a0c0e] p-1" data-testid="terminal-host" />
    </div>
  );
}

function StateBadge({ state }: { state: ConnState }) {
  const map: Record<ConnState, { cls: string; label: string }> = {
    connecting: { cls: "pill-muted", label: "connecting" },
    open: { cls: "pill-ok", label: "live" },
    closed: { cls: "pill-muted", label: "closed" },
    disabled: { cls: "pill-warn", label: "disabled" },
    error: { cls: "pill-err", label: "error" },
  };
  const { cls, label } = map[state];
  return (
    <span className={cls} data-testid="terminal-state" data-value={state}>
      {label}
    </span>
  );
}
