import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { BookMarked, Rocket, ShieldCheck } from "lucide-react";

import { api, type DispatchOptions, type DispatchRecipe } from "@/lib/api";
import { formatCost } from "@/lib/format";
import { topologyEvidence, type EvidenceView } from "@/lib/evidence";
import { EvidenceBadge, FamilyPill } from "@/components/Pill";

/** Capability catalog: every recipe on disk, its pipeline, and what it has
 * actually cost + won HERE. The catalog is the honest answer to "what can this
 * thing do?" — measured, not marketed. */
export function RecipesPage() {
  const options = useQuery({
    queryKey: ["dispatch-options"],
    queryFn: () => api.dispatchOptions(),
    staleTime: 30_000,
  });

  const recipes = options.data?.recipes ?? [];

  return (
    <div className="p-3 space-y-3 max-w-[1500px] mx-auto" data-testid="recipes-page">
      <header className="card">
        <div className="panel-title">
          <BookMarked size={13} /> Recipe Catalog
        </div>
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="m-0 text-[21px] font-black uppercase tracking-[0.08em] text-ink-100">Capabilities</h1>
            <p className="text-[12px] text-ink-400 mt-1">
              Every topology on disk, its pipeline, and what it has measurably cost + won on this repo.
            </p>
          </div>
          <Link to="/new" className="btn" data-testid="recipes-to-launcher">
            <Rocket size={13} /> New Run
          </Link>
        </div>
      </header>

      {options.isLoading && <p className="text-[12px] text-ink-400">loading recipes…</p>}
      {options.isError && (
        <p className="card text-[12px] text-[var(--red)]">could not load recipes — is the API on :7090?</p>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3" data-testid="recipes-grid">
        {recipes.map((r) => (
          <RecipeCatalogCard
            key={r.recipe}
            recipe={r}
            evidence={options.data ? matchEvidence(r, options.data) : null}
            cost={options.data ? matchCost(r, options.data) : null}
          />
        ))}
      </div>
      {options.data && !recipes.length && (
        <p className="card text-[12px] text-ink-400">No recipes with a workflow.yaml found on disk.</p>
      )}
    </div>
  );
}

function matchEvidence(r: DispatchRecipe, opts: DispatchOptions): EvidenceView | null {
  const topo = opts.topologies.find((t) => t.workflow_name === r.recipe);
  return topo ? topologyEvidence(topo, opts.min_samples) : null;
}

function matchCost(r: DispatchRecipe, opts: DispatchOptions): number | null {
  const topo = opts.topologies.find((t) => t.workflow_name === r.recipe);
  if (topo?.avg_cost_usd != null) return topo.avg_cost_usd;
  const laneCosts = r.nodes
    .map((n) => opts.lanes.find((l) => l.lane === n.lane)?.avg_cost_usd)
    .filter((c): c is number => c != null);
  return laneCosts.length ? laneCosts.reduce((a, b) => a + b, 0) : null;
}

function RecipeCatalogCard({
  recipe,
  evidence,
  cost,
}: {
  recipe: DispatchRecipe;
  evidence: EvidenceView | null;
  cost: number | null;
}) {
  return (
    <section className="card space-y-2" data-testid={`recipe-card-${recipe.recipe}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-mono text-[13px] font-bold text-ink-100">{recipe.recipe}</div>
          {recipe.task_class && (
            <div className="mt-0.5 font-mono text-[10px] text-ink-500">task_class · {recipe.task_class}</div>
          )}
        </div>
        <Link
          to="/new"
          className="btn shrink-0"
          title={`launch ${recipe.recipe}`}
          data-testid={`recipe-launch-${recipe.recipe}`}
        >
          <Rocket size={12} /> launch
        </Link>
      </div>

      {recipe.description && <p className="text-[11.5px] leading-snug text-ink-300">{recipe.description}</p>}

      <div className="flex flex-wrap items-center gap-2 border-y border-[var(--hair)] py-1.5">
        <span className="text-[9.5px] uppercase tracking-[0.12em] text-ink-500">measured</span>
        {evidence ? <EvidenceBadge view={evidence} /> : <span className="pill-muted">no runs</span>}
        <span className="font-mono text-[10.5px] text-ink-400">{cost != null ? `~${formatCost(cost)}/run` : "cost n/a"}</span>
      </div>

      {/* the pipeline — type → lane → gates → verifier, per node */}
      <div className="space-y-1" data-testid={`recipe-pipeline-${recipe.recipe}`}>
        {recipe.nodes.map((n, i) => (
          <div key={`${n.name}-${i}`} className="flex items-center gap-2 text-[10.5px]">
            <span className="w-4 shrink-0 text-right font-mono text-ink-600">{i + 1}</span>
            <span className="min-w-0 flex-1 truncate">
              <span className="font-mono text-ink-200">{n.name ?? "—"}</span>
              <span className="ml-1.5 font-mono text-ink-500">{n.type}</span>
            </span>
            {n.lane && <FamilyPill family={laneFamily(n.lane)} />}
            {n.dispatch_mode && n.dispatch_mode !== "serial" && (
              <span className="pill-muted">{n.dispatch_mode}</span>
            )}
            {n.gates.length > 0 && (
              <span className="inline-flex items-center gap-1 text-[9.5px] text-ink-500" title={n.gates.join(", ")}>
                <ShieldCheck size={11} /> {n.gates.length}
              </span>
            )}
            {n.verifier_ref && (
              <span className="pill-ok" title={`verifier: ${n.verifier_ref}`}>
                verified
              </span>
            )}
          </div>
        ))}
        {!recipe.nodes.length && <p className="text-[10.5px] text-ink-500">no nodes declared</p>}
      </div>
    </section>
  );
}

/** Lane ids look like "opus-4", "codex-medium" — the family is the leading token. */
function laneFamily(lane: string): string {
  return lane.split(/[-_/]/)[0] ?? lane;
}
