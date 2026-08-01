import { useState } from "react";
import { useSearch } from "@tanstack/react-router";
import { TerminalSquare, Info } from "lucide-react";

import { TerminalPanel } from "@/components/TerminalPanel";

type Cmd = "shell" | "opencode";

/** Standalone live shell into the workspace (or a run's directory) over the
 * opt-in PTY bridge. A deep link like `/terminal?run=<id>&cmd=opencode` lands
 * the operator straight in that run's cwd — the command palette + run pages
 * use exactly that. The bridge is off unless the API is started with
 * MO_PTY_ENABLED=1; the panel surfaces that as an actionable state, so this
 * page is honest even when the feature is disabled. */
export function TerminalPage() {
  const search = useSearch({ from: "/terminal" });
  const runFromUrl = typeof search.run === "string" ? search.run : "";
  const cmdFromUrl: Cmd = search.cmd === "opencode" ? "opencode" : "shell";

  const [cmd, setCmd] = useState<Cmd>(cmdFromUrl);
  const [runDraft, setRunDraft] = useState(runFromUrl);
  const [runId, setRunId] = useState(runFromUrl);

  return (
    <div className="p-3 space-y-3 max-w-[1500px] mx-auto" data-testid="terminal-page">
      <header className="card">
        <div className="panel-title">
          <TerminalSquare size={13} /> Terminal
        </div>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="m-0 text-[21px] font-black uppercase tracking-[0.08em] text-ink-100">Live shell</h1>
            <p className="mt-1 text-[12px] text-ink-400">
              A real PTY into the workspace, or a specific run&apos;s directory. Off unless the API runs with{" "}
              <code className="font-mono text-[11px] text-ork-amber">MO_PTY_ENABLED=1</code>.
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <CmdToggle cmd={cmd} onChange={setCmd} />
            <form
              className="flex items-end gap-1.5"
              onSubmit={(e) => {
                e.preventDefault();
                setRunId(runDraft.trim());
              }}
            >
              <label className="flex flex-col gap-1">
                <span className="text-[9.5px] uppercase tracking-[0.13em] text-ink-500">attach to run (optional)</span>
                <input
                  value={runDraft}
                  onChange={(e) => setRunDraft(e.target.value)}
                  placeholder="run id — blank = repo root"
                  className="h-7 w-[240px] rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-2 font-mono text-[11px] text-ink-200 outline-none placeholder:text-ink-600 focus:border-[var(--cyan)]"
                  data-testid="terminal-run-input"
                />
              </label>
              <button
                type="submit"
                className="h-7 rounded-[3px] border border-[var(--hair-2)] px-3 text-[10.5px] font-bold uppercase tracking-[0.08em] text-ink-200 hover:border-[var(--cyan)] hover:text-[var(--cyan)]"
                data-testid="terminal-attach"
              >
                attach
              </button>
            </form>
          </div>
        </div>
      </header>

      <div className="flex items-start gap-2 rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-3 py-2 text-[10.5px] text-ink-400">
        <Info size={13} className="mt-0.5 shrink-0 text-[var(--cyan)]" />
        <span>
          Keystrokes go straight to the shell — nothing is auto-run. When the composer or a run page hands a command
          here, it is typed but NOT entered; you press enter. cwd is the run&apos;s directory when attached, else the
          repo root.
        </span>
      </div>

      {/* key on runId+cmd so changing either tears down the old socket and
          mounts a fresh terminal against the new target. */}
      <TerminalPanel key={`${runId}·${cmd}`} runId={runId || undefined} cmd={cmd} />
    </div>
  );
}

function CmdToggle({ cmd, onChange }: { cmd: Cmd; onChange: (c: Cmd) => void }) {
  const opts: Cmd[] = ["shell", "opencode"];
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[9.5px] uppercase tracking-[0.13em] text-ink-500">command</span>
      <div className="flex overflow-hidden rounded-[3px] border border-[var(--hair-2)]" data-testid="terminal-cmd-toggle">
        {opts.map((o) => (
          <button
            key={o}
            type="button"
            onClick={() => onChange(o)}
            data-active={cmd === o}
            className="px-3 py-1 text-[10.5px] font-bold uppercase tracking-[0.08em] text-ink-500 hover:text-ink-100 data-[active=true]:bg-[var(--cyan)]/15 data-[active=true]:text-[var(--cyan)]"
          >
            {o}
          </button>
        ))}
      </div>
    </div>
  );
}
