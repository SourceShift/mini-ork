import { useEffect, useRef, useState } from "react";
import { KeyRound, Lock, ShieldCheck } from "lucide-react";

import { setOperatorToken } from "@/lib/api";
import { useOperatorToken } from "@/lib/useOperatorToken";

/** Footer control for the operator Bearer token. stop/kill trust the loopback
 * bind, but pause-cost / resume-cost / steer / launch are token-gated and
 * fail-closed server-side. When no token is set the deck is read-plus-abort
 * only; setting one unlocks the write plane and stamps the operator on every
 * audit record. The token is stored per-browser (localStorage) via the api
 * store, so it survives reloads and rides on every request. */
export function OperatorTokenControl() {
  const token = useOperatorToken();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setDraft(token ?? "");
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!document.contains(t)) return;
      if (boxRef.current && !boxRef.current.contains(t)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, token]);

  const hasToken = token != null;

  return (
    <div ref={boxRef} className="relative" data-testid="operator-token-control">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="operator-token-toggle"
        data-authorized={hasToken}
        className={
          hasToken
            ? "pill-ok rounded-none flex items-center gap-1"
            : "pill-warn rounded-none flex items-center gap-1"
        }
        title={hasToken ? "Operator authorized — click to manage token" : "Read-only — click to set an operator token"}
      >
        {hasToken ? <KeyRound size={11} /> : <Lock size={11} />}
        {hasToken ? "operator" : "read-only"}
      </button>

      {open && (
        <div
          className="absolute bottom-full left-0 z-50 mb-1 w-[320px] rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] p-3 shadow-lg"
          data-testid="operator-token-popover"
        >
          <div className="mb-1 text-[9.5px] uppercase tracking-[0.18em] text-ink-500">Operator token</div>
          <p className="mb-2 text-[10.5px] leading-snug text-ink-400">
            Unlocks the write plane — cost pause/resume, steer, and detached
            launch. stop/kill work without it. Server is fail-closed: a wrong or
            missing token gets a 401.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setOperatorToken(draft);
              setOpen(false);
            }}
          >
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="paste token_hex from auth-tokens.txt"
              className="mb-2 h-7 w-full rounded-[3px] border border-[var(--hair-2)] bg-[var(--panel)] px-2 font-mono text-[10.5px] text-ink-200 outline-none placeholder:text-ink-600 focus:border-[var(--cyan)]"
              data-testid="operator-token-input"
            />
            <div className="flex items-center gap-1.5">
              <button
                type="submit"
                disabled={!draft.trim() || draft.trim() === token}
                className="h-6 flex-1 rounded-[3px] border border-[var(--grn)] bg-[var(--w-grn)] text-[10.5px] font-bold uppercase tracking-[0.08em] text-[var(--grn)] hover:bg-[rgba(79,209,160,0.18)] disabled:opacity-40"
                data-testid="operator-token-save"
              >
                Authorize
              </button>
              <button
                type="button"
                disabled={!hasToken}
                onClick={() => {
                  setOperatorToken(null);
                  setDraft("");
                  setOpen(false);
                }}
                className="h-6 rounded-[3px] border border-[var(--hair-2)] px-2 text-[10.5px] uppercase tracking-[0.08em] text-ink-400 hover:border-[var(--red)] hover:text-[var(--red)] disabled:opacity-40 disabled:hover:border-[var(--hair-2)] disabled:hover:text-ink-400"
                data-testid="operator-token-clear"
              >
                Clear
              </button>
            </div>
          </form>
          {hasToken && (
            <div className="mt-2 flex items-center gap-1.5 text-[9.5px] text-[var(--grn)]">
              <ShieldCheck size={11} />
              authorized · token stored in this browser
            </div>
          )}
        </div>
      )}
    </div>
  );
}
