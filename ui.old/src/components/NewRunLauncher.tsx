import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Check, ChevronDown, Copy, Rocket, TriangleAlert } from "lucide-react";
import clsx from "clsx";

import { api, type DispatchRecipe, type DispatchResponse } from "@/lib/api";
import { formatCost } from "@/lib/format";
import { laneEvidence, topologyEvidence, type EvidenceView } from "@/lib/evidence";
import { EvidenceBadge } from "@/components/Pill";

/**
 * The composer. Turns app-main from an observatory into a launch surface — but
 * an HONEST one: mini-ork does NOT spawn the process here. POST /dispatch mints
 * the run-id, records the "machine proposed X · human chose Y" decision, and
 * hands back the exact command. The operator copies it (or, once the embedded
 * terminal lands in Phase 4, auto-types it into the allow-listed PTY shell).
 */
export function NewRunLauncher({ compact = false }: { compact?: boolean }) {
  const options = useQuery({
    queryKey: ["dispatch-options"],
    queryFn: () => api.dispatchOptions(),
    staleTime: 30_000,
  });

  const recipes = options.data?.recipes ?? [];
  const minSamples = options.data?.min_samples ?? 5;

  // The machine's proposal: the recipe the deck highlights by default. Captured
  // once so a later human pick becomes a labelled override, not a silent choice.
  const proposed = recipes[0]?.recipe ?? null;
  const [recipe, setRecipe] = useState<string | null>(null);
  const chosen = recipe ?? proposed;
  const [kickoff, setKickoff] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [result, setResult] = useState<DispatchResponse | null>(null);

  const selected = recipes.find((r) => r.recipe === chosen) ?? null;

  const dispatch = useMutation({
    mutationFn: () =>
      api.dispatch({
        kickoff,
        recipe: chosen ?? "",
        task_class: selected?.task_class ?? null,
        proposed_recipe: proposed,
      }),
    onSuccess: (res) => setResult(res),
  });

  const canDispatch = !!chosen && kickoff.trim().length > 0 && !dispatch.isPending;
  const overrode = !!proposed && !!chosen && proposed !== chosen;

  return (
    <section className="card" data-testid="new-run-launcher">
      <div className="panel-title">
        <Rocket size={13} /> New Run · compose &amp; dispatch
      </div>

      {options.isLoading && <p className="text-[12px] text-ink-400">loading recipes…</p>}
      {options.isError && (
        <p className="text-[12px] text-[var(--red)]" data-testid="launcher-error">
          could not load dispatch options — is the API on :7090?
        </p>
      )}

      {options.data && (
        <div className="space-y-3">
          {/* ── recipe picker: each card priced against measured evidence ── */}
          <div>
            <FieldLabel>Recipe {overrode && <OverrideTag />}</FieldLabel>
            <div
              className={clsx(
                "grid gap-2",
                compact ? "grid-cols-1 sm:grid-cols-2" : "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3",
              )}
              data-testid="launcher-recipe-grid"
            >
              {recipes.map((r) => (
                <RecipeCard
                  key={r.recipe}
                  recipe={r}
                  evidence={recipeEvidence(r, options.data, minSamples)}
                  cost={recipeCost(r, options.data)}
                  selected={r.recipe === chosen}
                  isProposal={r.recipe === proposed}
                  onSelect={() => {
                    setRecipe(r.recipe);
                    setResult(null);
                  }}
                />
              ))}
              {!recipes.length && (
                <p className="text-[12px] text-ink-400" data-testid="launcher-no-recipes">
                  No recipes with a workflow.yaml found on disk.
                </p>
              )}
            </div>
          </div>

          {/* ── kickoff ── */}
          <div>
            <FieldLabel>Kickoff — what should the fleet do?</FieldLabel>
            <textarea
              value={kickoff}
              onChange={(e) => {
                setKickoff(e.target.value);
                setResult(null);
              }}
              rows={compact ? 2 : 3}
              placeholder="e.g. Fix the null-deref in the auth middleware; files in scope: mini_ork/web/auth.py"
              className="w-full rounded-[3px] border border-[var(--hair-2)] bg-[var(--bg)] px-2 py-1.5 text-[12px] font-mono text-ink-200 outline-none placeholder:text-ink-600 focus:border-[var(--cyan)]"
              data-testid="launcher-kickoff"
            />
          </div>

          {/* ── dry-run / live + dispatch ── */}
          <div className="flex flex-wrap items-center gap-3">
            <DryRunToggle dryRun={dryRun} onChange={setDryRun} />
            <button
              type="button"
              disabled={!canDispatch}
              onClick={() => dispatch.mutate()}
              className="btn ml-auto disabled:opacity-40 disabled:hover:border-[var(--hair-2)]"
              style={{ borderColor: canDispatch ? "var(--grn)" : undefined, color: canDispatch ? "var(--grn)" : undefined }}
              data-testid="launcher-dispatch"
            >
              <Rocket size={13} />
              {dispatch.isPending ? "preparing…" : "Prepare dispatch"}
            </button>
          </div>

          {dispatch.isError && (
            <p className="text-[12px] text-[var(--red)]" data-testid="launcher-dispatch-error">
              {(dispatch.error as Error).message}
            </p>
          )}

          {result && <CommandBlock result={result} dryRun={dryRun} />}
        </div>
      )}
    </section>
  );
}

/** Match a recipe to its topology win-rate evidence (best-effort by name). */
function recipeEvidence(
  r: DispatchRecipe,
  opts: { topologies: { workflow_name: string | null }[]; min_samples: number },
  minSamples: number,
): EvidenceView | null {
  const topo = opts.topologies.find((t) => t.workflow_name === r.recipe);
  return topo ? topologyEvidence(topo as never, minSamples) : null;
}

/** Cheapest measured signal for a recipe: its topology avg cost, else the
 * mean of its nodes' lane costs. Returns null when nothing is measured. */
function recipeCost(
  r: DispatchRecipe,
  opts: { topologies: { workflow_name: string | null; avg_cost_usd: number | null }[]; lanes: { lane: string; avg_cost_usd: number | null }[] },
): number | null {
  const topo = opts.topologies.find((t) => t.workflow_name === r.recipe);
  if (topo?.avg_cost_usd != null) return topo.avg_cost_usd;
  const laneCosts = r.nodes
    .map((n) => opts.lanes.find((l) => l.lane === n.lane)?.avg_cost_usd)
    .filter((c): c is number => c != null);
  if (!laneCosts.length) return null;
  return laneCosts.reduce((a, b) => a + b, 0);
}

function RecipeCard({
  recipe,
  evidence,
  cost,
  selected,
  isProposal,
  onSelect,
}: {
  recipe: DispatchRecipe;
  evidence: EvidenceView | null;
  cost: number | null;
  selected: boolean;
  isProposal: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={clsx(
        "flex flex-col gap-1.5 rounded-[3px] border p-2 text-left transition-colors",
        selected
          ? "border-[var(--grn)] bg-[var(--w-grn)]"
          : "border-[var(--hair-2)] bg-[var(--panel-3)] hover:border-[var(--edge)]",
      )}
      data-testid={`launcher-recipe-${recipe.recipe}`}
      data-selected={selected}
    >
      <div className="flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] font-bold text-ink-100">{recipe.recipe}</span>
        {isProposal && <span className="pill-muted shrink-0">default</span>}
        {selected && <Check size={13} className="shrink-0 text-[var(--grn)]" />}
      </div>
      {recipe.description && (
        <p className="line-clamp-2 text-[10.5px] leading-snug text-ink-400">{recipe.description}</p>
      )}
      <div className="mt-auto flex items-center gap-2 pt-0.5">
        {evidence ? <EvidenceBadge view={evidence} /> : <span className="pill-muted">no runs</span>}
        <span className="ml-auto font-mono text-[10px] text-ink-500">
          {cost != null ? `~${formatCost(cost)}` : "cost n/a"}
        </span>
        <span className="font-mono text-[10px] text-ink-600">{recipe.nodes.length} nodes</span>
      </div>
    </button>
  );
}

function DryRunToggle({ dryRun, onChange }: { dryRun: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="inline-flex overflow-hidden rounded-[3px] border border-[var(--hair-2)]" role="group" aria-label="Run mode" data-testid="launcher-mode">
      <button
        type="button"
        onClick={() => onChange(true)}
        aria-pressed={dryRun}
        className={clsx(
          "px-2.5 py-1 text-[10.5px] font-bold uppercase tracking-[0.06em]",
          dryRun ? "bg-white/[0.06] text-[var(--cyan)]" : "text-ink-500 hover:text-ink-300",
        )}
      >
        dry-run
      </button>
      <button
        type="button"
        onClick={() => onChange(false)}
        aria-pressed={!dryRun}
        className={clsx(
          "border-l border-[var(--hair-2)] px-2.5 py-1 text-[10.5px] font-bold uppercase tracking-[0.06em]",
          !dryRun ? "bg-[var(--w-amb)] text-[var(--amb)]" : "text-ink-500 hover:text-ink-300",
        )}
      >
        live
      </button>
    </div>
  );
}

/** The dispatch receipt: run-id minted, decision recorded, exact command to run.
 * mini-ork did NOT spawn it — this is copy-to-run, honest about the boundary. */
function CommandBlock({ result, dryRun }: { result: DispatchResponse; dryRun: boolean }) {
  const [copied, setCopied] = useState(false);
  const env = { ...result.env, ...(dryRun ? { MINI_ORK_DRY_RUN: "1" } : {}) };
  const envPrefix = Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join(" ");
  const fullCommand = `${envPrefix} ${result.command}`.trim();

  const copy = () => {
    void navigator.clipboard?.writeText(fullCommand).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="rounded-[3px] border border-[var(--grn)] bg-[var(--w-grn)] p-2.5" data-testid="launcher-command-block">
      <div className="mb-1.5 flex items-center gap-2 text-[10px] uppercase tracking-[0.1em] text-ink-400">
        <span className="pill-ok">run minted</span>
        <span className="font-mono normal-case tracking-normal text-ink-300">{result.run_id}</span>
        <span className="pill-muted">{result.decided_by}</span>
        {result.overrode && <span className="pill-warn">override recorded</span>}
      </div>
      <div className="flex items-start gap-2">
        <pre className="min-w-0 flex-1 overflow-x-auto rounded-[2px] bg-[var(--bg)] p-2 text-[11px] leading-relaxed text-ink-200 thin">
          <code>{fullCommand}</code>
        </pre>
        <button
          type="button"
          onClick={copy}
          className="btn shrink-0"
          title="copy command"
          data-testid="launcher-copy-command"
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-3 text-[10px] text-ink-500">
        <span className="inline-flex items-center gap-1">
          <TriangleAlert size={11} className="text-[var(--amb)]" />
          mini-ork did not spawn this — copy to run in your shell.
        </span>
        <Link
          to="/runs/$taskRunId"
          params={{ taskRunId: result.run_id }}
          className="ml-auto inline-flex items-center gap-1 text-[var(--cyan)] hover:underline"
          data-testid="launcher-track-run"
        >
          track this run <ChevronDown size={11} className="-rotate-90" />
        </Link>
      </div>
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1 flex items-center gap-2 text-[9.5px] uppercase tracking-[0.13em] text-ink-500">{children}</div>
  );
}

function OverrideTag() {
  return (
    <span className="pill-warn" data-testid="launcher-override-tag" title="You picked a recipe other than the default — recorded as a human override.">
      override
    </span>
  );
}
