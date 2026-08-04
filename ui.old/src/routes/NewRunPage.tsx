import { Link } from "@tanstack/react-router";
import { BookMarked } from "lucide-react";

import { NewRunLauncher } from "@/components/NewRunLauncher";

/** Full-page composer. The launcher itself is the whole page; the catalog link
 * sits alongside for operators who want to browse capabilities first. */
export function NewRunPage() {
  return (
    <div className="p-3 space-y-3 max-w-[1100px] mx-auto" data-testid="new-run-page">
      <header className="card">
        <div className="panel-title">Dispatch</div>
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="m-0 text-[21px] font-black uppercase tracking-[0.08em] text-ink-100">New Run</h1>
            <p className="text-[12px] text-ink-400 mt-1">
              Compose a topology, price it against measured evidence, and mint a run — mini-ork records the
              decision and hands you the exact command.
            </p>
          </div>
          <Link to="/recipes" className="btn" data-testid="new-run-to-catalog">
            <BookMarked size={13} /> Catalog
          </Link>
        </div>
      </header>

      <NewRunLauncher />
    </div>
  );
}
