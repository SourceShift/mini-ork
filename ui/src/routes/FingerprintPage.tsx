import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, FileText, ShieldAlert, ShieldCheck } from "lucide-react";

import { api, type GlobalGradient } from "@/lib/api";
import { FamilyPill } from "@/components/Pill";

const SCORE: Record<string, number> = {
  heterogeneous: 95,
  low: 78,
  medium: 52,
  high: 22,
};

export function FingerprintPage() {
  const recipes = useQuery({ queryKey: ["recipes"], queryFn: api.recipes });
  const [selected, setSelected] = useState<string | null>(null);
  const recipe = selected ?? recipes.data?.[0] ?? null;
  const fp = useQuery({
    queryKey: ["fingerprint", recipe],
    queryFn: () => api.fingerprint(recipe!),
    enabled: !!recipe,
  });
  const gradients = useQuery({
    queryKey: ["gradients", 25],
    queryFn: () => api.gradients(25),
    refetchInterval: 15_000,
  });
  const selfImprove = useQuery({
    queryKey: ["self-improve", "fingerprint"],
    queryFn: () => api.selfImprove(10),
    refetchInterval: 15_000,
  });

  const verdict = fp.data?.coalition ?? "medium";
  const score = SCORE[verdict] ?? 52;
  const color = verdict === "high" ? "var(--red)" : verdict === "medium" ? "var(--amb)" : "var(--grn)";
  const Icon = verdict === "high" || verdict === "medium" ? ShieldAlert : ShieldCheck;
  const familyEntries = Object.entries(fp.data?.families_used ?? {});

  return (
    <div className="min-h-full overflow-auto" data-testid="fingerprint-page">
      <div className="mx-auto max-w-[880px] px-10 py-10">
        <div className="mb-7 flex flex-wrap items-center justify-between gap-3">
          <span className="text-[9.5px] font-bold uppercase tracking-[0.28em] text-ink-500">◆ detection fingerprint</span>
          <div className="flex flex-wrap gap-1" data-testid="fingerprint-recipe-picker">
            {recipes.data?.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setSelected(item)}
                data-testid={`fingerprint-recipe-${item}`}
                data-active={item === recipe}
                className={`btn h-6 ${item === recipe ? "border-[var(--grn)] bg-[var(--w-grn)] text-[var(--grn)]" : ""}`}
              >
                {item}
              </button>
            ))}
          </div>
        </div>

        <blockquote className="mb-10 border-l-2 border-[var(--red)] pl-5" data-testid="fingerprint-thesis">
          <p className="m-0 max-w-[760px] text-[21px] font-medium leading-[1.55] tracking-[-0.01em] text-ink-100">
            “If the list reads <span className="font-mono text-[18px] text-[#e0975f]">Sonnet, Sonnet, Sonnet, Sonnet, Opus</span> you have an evaluative
            coalition, not an audit.”
          </p>
          <cite className="mt-3 block text-[9.5px] not-italic uppercase tracking-[0.12em] text-ink-500">
            — why-mini-ork.md · the heterogeneity thesis
          </cite>
        </blockquote>

        {fp.data && (
          <>
            <section className="card !p-0 overflow-hidden" data-testid="fingerprint-coalition" data-coalition={verdict}>
              <div className="panel-title !m-0">
                Coalition verdict
                <span className="ml-auto font-mono text-[9.5px] font-normal lowercase tracking-normal text-ink-500">{fp.data.recipe}</span>
              </div>
              <div className="flex flex-col gap-5 px-7 py-8 md:flex-row md:items-center">
                <Gauge score={score} color={color} />
                <div className="min-w-0">
                  <div className="mb-2 flex items-center gap-2">
                    <Icon size={18} style={{ color }} />
                    <span className="font-mono text-[12px] text-ink-400">{fp.data.recipe}</span>
                  </div>
                  <div className="mb-2 text-[40px] font-black capitalize leading-none tracking-[-0.025em]" style={{ color }}>
                    {verdict === "heterogeneous" ? "Heterogeneous" : `${verdict} risk`}
                  </div>
                  <p className="m-0 max-w-[480px] text-[15px] leading-relaxed text-ink-400">
                    {verdictCopy(verdict, Object.keys(fp.data.families_used).length, fp.data.nodes.length)}
                  </p>
                </div>
              </div>
            </section>

            <hr className="my-8 border-0 border-t border-[var(--hair)]" />

            <div className="mb-4 flex items-center justify-between">
              <span className="text-[10px] font-black uppercase tracking-[0.2em] text-ink-500">Family distribution</span>
              <span className="font-mono text-[9.5px] text-ink-500">
                {familyEntries.length} distinct · {fp.data.nodes.length} nodes
              </span>
            </div>
            <div className="mb-9 flex flex-wrap gap-2">
              {familyEntries.map(([family, count]) => (
                <div
                  key={family}
                  className="flex items-center gap-2 rounded-[3px] border border-[var(--hair-2)] bg-[var(--panel)] px-3 py-2"
                  style={{ borderLeft: `3px solid ${familyCssColor(family)}` }}
                >
                  <span className="font-mono text-[12px] text-ink-200">{family}</span>
                  <span className="font-mono text-[12px] text-ink-500">×{count}</span>
                </div>
              ))}
            </div>

            <div className="mb-3 text-[10px] font-black uppercase tracking-[0.2em] text-ink-500">
              Per-node attribution · the receipt
            </div>
            <div className="card !p-0 overflow-hidden">
              <table className="tbl">
                <thead>
                  <tr>
                    <Th className="w-1"> </Th>
                    <Th>node_id</Th>
                    <Th>family</Th>
                    <Th>model_lane</Th>
                    <Th>model_id</Th>
                    <Th>provider</Th>
                  </tr>
                </thead>
                <tbody>
                  {fp.data.nodes.map((node) => (
                    <tr key={node.name} data-testid={`fp-node-row-${node.name}`} data-family={node.family ?? "none"}>
                      <Td className="!p-0">
                        <span className="block h-7 w-[3px]" style={{ background: familyCssColor(node.family) }} />
                      </Td>
                      <Td className="font-mono text-ink-100">{node.name}</Td>
                      <Td><FamilyPill family={node.family} /></Td>
                      <Td className="font-mono text-[11px] text-ink-500">{node.lane ?? "—"}</Td>
                      <Td className="font-mono text-[11px] text-ink-400">{modelLabel(node.family, node.lane)}</Td>
                      <Td className="font-mono text-[11px] text-ink-500">{providerLabel(node.family)}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {verdict === "high" && (
              <div className="card mt-5 border-[rgba(240,88,78,0.35)]">
                <div className="mb-2 flex items-center gap-2 text-[13px] font-bold text-ink-100">
                  <ShieldAlert size={15} style={{ color: "var(--red)" }} /> What to do
                </div>
                <p className="m-0 text-[12px] leading-relaxed text-ink-400">
                  Multiple reviewer lanes share one family. Reassign at least one lane to a different family so the verdict reflects independent judgement.
                </p>
              </div>
            )}

            <div className="mt-6 flex flex-wrap gap-3">
              <button className="btn" type="button"><FileText size={13} /> read the thesis</button>
              <button className="btn" type="button">runs on this recipe <ExternalLink size={13} /></button>
            </div>

            <LearningSignalsPanel gradients={gradients.data ?? []} selfImproveCount={selfImprove.data?.length ?? 0} />
          </>
        )}
      </div>
    </div>
  );
}

function verdictCopy(verdict: string, families: number, nodes: number): string {
  if (verdict === "heterogeneous") {
    return "Five families, no quorum any single family can swing. This is an audit, not an echo chamber — findings that survive disagreement are load-bearing.";
  }
  if (verdict === "low") {
    return `${families} families across ${nodes} nodes with minor overlap. Coverage is broad; watch the lanes that share a family.`;
  }
  if (verdict === "medium") {
    return "Some lanes share a family. A finding agreed on by same-family nodes carries less independent weight — read the attribution before trusting consensus.";
  }
  return "A coalition. Multiple nodes share one family and can swing the verdict together. Reassign lanes before treating this as an independent audit.";
}

function Gauge({ score, color }: { score: number; color: string }) {
  const dash = (score / 100) * 148;
  return (
    <svg width="180" height="116" viewBox="0 0 180 116" className="shrink-0" aria-hidden="true">
      <path d="M16 100 A74 74 0 0 1 164 100" fill="none" stroke="var(--panel-3)" strokeWidth="11" strokeLinecap="round" />
      <path
        d="M16 100 A74 74 0 0 1 164 100"
        fill="none"
        stroke={color}
        strokeWidth="11"
        strokeLinecap="round"
        strokeDasharray={`${dash.toFixed(1)} 999`}
      />
      <text x="90" y="86" textAnchor="middle" fill={color} style={{ fontSize: 36, fontWeight: 800, fontFamily: "var(--mono)" }}>
        {score}
      </text>
      <text x="90" y="104" textAnchor="middle" fill="var(--tx-5)" style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase" }}>
        coalition
      </text>
    </svg>
  );
}

function LearningSignalsPanel({
  gradients,
  selfImproveCount,
}: {
  gradients: GlobalGradient[];
  selfImproveCount: number;
}) {
  return (
    <section className="card mt-8" data-testid="fingerprint-learning-signals">
      <div className="panel-title">Learning Signals</div>
      <div className="grid grid-cols-1 xl:grid-cols-[180px_1fr] gap-3">
        <div className="grid grid-cols-2 xl:grid-cols-1 gap-2">
          <MiniMetric label="gradients" value={gradients.length} />
          <MiniMetric label="self improve" value={selfImproveCount} />
        </div>
        <div className="overflow-x-auto">
          <table className="tbl">
            <thead>
              <tr>
                <Th>Target</Th>
                <Th>Signal</Th>
                <Th>Suggested change</Th>
                <Th className="text-right">Confidence</Th>
              </tr>
            </thead>
            <tbody>
              {gradients.slice(0, 6).map((gradient) => (
                <tr key={gradient.gradient_id}>
                  <Td className="font-mono text-[10.5px] text-ink-400">{gradient.target}</Td>
                  <Td className="max-w-[260px] truncate text-ink-200">{gradient.signal}</Td>
                  <Td className="max-w-[360px] truncate text-ink-400">{gradient.suggested_change}</Td>
                  <Td className="text-right font-mono text-[10.5px] text-ink-400">
                    {Math.round((gradient.confidence ?? 0) * 100)}%
                  </Td>
                </tr>
              ))}
              {!gradients.length && (
                <tr>
                  <Td colSpan={4} className="text-ink-500">No gradient_records available yet.</Td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function MiniMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] p-3">
      <div className="text-[9.5px] uppercase tracking-[0.13em] text-ink-500">{label}</div>
      <div className="mt-1 font-mono text-[22px] font-black leading-none text-ink-100">{value}</div>
    </div>
  );
}

function modelLabel(family: string | null | undefined, lane: string | null | undefined): string {
  if (family === "opus") return "claude-opus-4.2";
  if (family === "sonnet") return "claude-sonnet-4.6";
  if (family === "codex") return "gpt-5-codex";
  if (family === "kimi") return "kimi-k2";
  if (family === "glm") return "glm-4.6";
  return lane ?? "—";
}

function providerLabel(family: string | null | undefined): string {
  if (family === "opus" || family === "sonnet") return "anthropic";
  if (family === "codex") return "openai";
  if (family === "kimi") return "moonshot";
  if (family === "glm") return "z.ai";
  return "—";
}

function familyCssColor(family: string | null | undefined): string {
  return {
    opus: "#a394e8",
    sonnet: "#e0975f",
    glm: "#46c0b6",
    kimi: "#5f97e6",
    codex: "#d4b24f",
    gemini: "#74c463",
  }[String(family ?? "").toLowerCase()] ?? "var(--tx-4)";
}

const Th = ({ children, className = "" }: { children: React.ReactNode; className?: string }) => (
  <th className={className}>{children}</th>
);

const Td = ({
  children,
  className = "",
  ...props
}: React.TdHTMLAttributes<HTMLTableCellElement>) => (
  <td className={className} {...props}>{children}</td>
);
