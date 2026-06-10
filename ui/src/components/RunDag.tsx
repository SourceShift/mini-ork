import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Terminal } from "lucide-react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  type Node,
  type Edge,
  Panel,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { DagEdge, DagNode } from "@/lib/api";
import { familyColor } from "@/lib/format";

const NODE_W = 150;
const NODE_H = 82;
const LOBE_ICON_BASE = "https://unpkg.com/@lobehub/icons-static-svg@latest/icons";

function layout(
  nodes: DagNode[],
  edges: DagEdge[],
  statusOf: (name: string) => string,
): { nodes: Node[]; edges: Edge[] } {
  // Simple longest-path layering (Sugiyama-light): topological depth → x,
  // ordinal within layer → y. Good enough for ≤20-node recipe DAGs without
  // pulling in dagre.
  const adj = new Map<string, string[]>();
  const indeg = new Map<string, number>();
  nodes.forEach((n) => {
    adj.set(n.name, []);
    indeg.set(n.name, 0);
  });
  edges.forEach((e) => {
    adj.get(e.from)?.push(e.to);
    indeg.set(e.to, (indeg.get(e.to) ?? 0) + 1);
  });

  const depth = new Map<string, number>();
  const queue = nodes.filter((n) => (indeg.get(n.name) ?? 0) === 0).map((n) => n.name);
  queue.forEach((n) => depth.set(n, 0));
  while (queue.length) {
    const cur = queue.shift()!;
    const d = depth.get(cur) ?? 0;
    for (const next of adj.get(cur) ?? []) {
      const nd = Math.max(depth.get(next) ?? 0, d + 1);
      depth.set(next, nd);
      indeg.set(next, (indeg.get(next) ?? 0) - 1);
      if ((indeg.get(next) ?? 0) === 0) queue.push(next);
    }
  }

  const byLayer = new Map<number, string[]>();
  nodes.forEach((n) => {
    const d = depth.get(n.name) ?? 0;
    if (!byLayer.has(d)) byLayer.set(d, []);
    byLayer.get(d)!.push(n.name);
  });

  const flowNodes: Node[] = nodes.map((n) => {
    const d = depth.get(n.name) ?? 0;
    const layer = byLayer.get(d) ?? [];
    const idx = layer.indexOf(n.name);
    return {
      id: n.name,
      data: { node: n },
      position: {
        x: idx * (NODE_W + 90) + (layer.length === 1 ? 260 : 0),
        y: d * (NODE_H + 54),
      },
      type: "ork",
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      width: NODE_W,
      height: NODE_H,
    };
  });

  const flowEdges: Edge[] = edges.map((e, i) => {
    const targetStatus = statusOf(e.to);
    const live = targetStatus === "running" || targetStatus === "startup";
    return {
      id: `e${i}`,
      source: e.from,
      target: e.to,
      label: e.edge_type === "depends_on" ? undefined : e.edge_type,
      labelStyle: { fontSize: 9, fill: "var(--tx-4)", fontFamily: "var(--mono)" },
      style: {
        stroke: live ? STATUS_STYLE.running!.ring : "#242e35",
        strokeWidth: live ? 1.6 : 1.2,
        opacity: live ? 0.9 : 1,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: live ? STATUS_STYLE.running!.ring : "#242e35",
        width: 16,
        height: 16,
      },
      animated: live,
    };
  });

  return { nodes: flowNodes, edges: flowEdges };
}

const STATUS_STYLE: Record<
  string,
  { ring: string; opacity: number; label: string; dashed?: boolean }
> = {
  never_seen: { ring: "#64748b", opacity: 0.55, label: "queued", dashed: true },
  startup:    { ring: "#f59e0b", opacity: 1.0,  label: "startup" },
  running:    { ring: "#38bdf8", opacity: 1.0,  label: "running" },
  done:       { ring: "#22c55e", opacity: 1.0,  label: "done" },
  failed:     { ring: "#ef4444", opacity: 1.0,  label: "failed" },
};

const LEGEND_ORDER = ["never_seen", "running", "done", "failed"] as const;

function visualStatus(n: DagNode): string {
  if (n.status === "running" && !n.started_at) return "startup";
  return n.status ?? "never_seen";
}

function StatusBadge({ status, ring }: { status: string; ring: string }) {
  if (status === "done")
    return (
      <span
        className="absolute right-9 top-0 grid h-4 w-4 place-items-center rounded-full border bg-[var(--bg)] text-[10px] font-bold"
        style={{ borderColor: ring, color: ring }}
      >
        ✓
      </span>
    );
  if (status === "failed")
    return (
      <span
        className="absolute right-9 top-0 grid h-4 w-4 place-items-center rounded-full border bg-[var(--bg)] text-[10px] font-bold"
        style={{ borderColor: ring, color: ring }}
      >
        !
      </span>
    );
  if (status === "running" || status === "startup")
    return (
      <span
        className="absolute right-9 top-0 grid h-4 w-4 place-items-center rounded-full border bg-[var(--bg)]"
        style={{ borderColor: ring }}
      >
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ background: ring, animation: "ork-ping 1.2s ease-out infinite" }}
        />
      </span>
    );
  return null;
}

type RuntimeBadge =
  | { kind: "llm"; label: string; provider: string; color: string }
  | { kind: "bash"; label: string; color: string };

function runtimeBadgeFor(node: DagNode, familyColorValue: string): RuntimeBadge {
  if (isBashNode(node)) {
    return { kind: "bash", label: "bash", color: "#38bdf8" };
  }

  const provider = lobeProviderFor(node.family, node.lane);
  if (provider) {
    return {
      kind: "llm",
      label: node.family ?? node.lane ?? provider,
      provider,
      color: familyColorValue,
    };
  }

  return { kind: "bash", label: node.type || "system", color: "#64748b" };
}

function isBashNode(node: DagNode): boolean {
  return Boolean(
    node.verifier_ref ||
      node.type === "verifier" ||
      (!node.lane && ["publisher", "rollback"].includes(node.type)),
  );
}

function lobeProviderFor(family?: string | null, lane?: string | null): string | null {
  const raw = `${family ?? ""} ${lane ?? ""}`.toLowerCase();
  if (["opus", "sonnet", "claude", "anthropic"].some((key) => raw.includes(key))) return "claude";
  if (["codex", "openai", "gpt"].some((key) => raw.includes(key))) return "openai";
  if (["glm", "zhipu", "zhi pu"].some((key) => raw.includes(key))) return "zhipu";
  if (["kimi", "moonshot"].some((key) => raw.includes(key))) return "moonshot";
  if (["minimax", "mini max"].some((key) => raw.includes(key))) return "minimax";
  if (raw.includes("deepseek")) return "deepseek";
  if (["gemini", "google"].some((key) => raw.includes(key))) return "gemini";
  if (raw.includes("mistral")) return "mistral";
  if (raw.includes("qwen")) return "qwen";
  if (raw.includes("perplexity")) return "perplexity";
  if (raw.includes("human")) return null;
  return family ? family.toLowerCase().replace(/[^a-z0-9]+/g, "") : null;
}

function lobeIconUrl(provider: string): string {
  return `${LOBE_ICON_BASE}/${provider}.svg`;
}

function RuntimeIconBadge({ badge, status }: { badge: RuntimeBadge; status: string }) {
  const done = status === "done";
  const style = { borderColor: badge.color, color: badge.color };
  return (
    <span
      className={
        done
          ? "absolute right-1 top-[-2px] z-10 grid h-5 w-5 place-items-center text-[10px] font-bold"
          : "absolute right-1 top-[-2px] z-10 grid h-5 w-5 place-items-center rounded-full border bg-[var(--bg)] text-[10px] font-bold shadow-[0_0_0_2px_var(--panel)]"
      }
      style={style}
      title={badge.kind === "llm" ? `LLM provider: ${badge.label}` : "Bash / deterministic runner"}
      aria-label={badge.kind === "llm" ? `LLM provider: ${badge.label}` : "Bash runner"}
      data-testid="dag-node-runtime-icon"
      data-runtime={badge.kind}
      data-provider={badge.kind === "llm" ? badge.provider : "bash"}
    >
      {badge.kind === "llm" ? (
        <span
          className="h-3.5 w-3.5"
          style={{
            backgroundColor: badge.color,
            WebkitMask: `url(${lobeIconUrl(badge.provider)}) center / contain no-repeat`,
            mask: `url(${lobeIconUrl(badge.provider)}) center / contain no-repeat`,
          }}
          aria-hidden="true"
        />
      ) : (
        <Terminal size={12} strokeWidth={2.4} aria-hidden="true" />
      )}
    </span>
  );
}

function OrkNode({ data }: { data: { node: DagNode } }) {
  const n = data.node;
  const status = visualStatus(n);
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.never_seen!;
  const family = n.family ? familyColor(n.family) : "#64748b";
  const runtimeBadge = runtimeBadgeFor(n, family);
  const running = status === "running" || status === "startup";
  return (
    <div
      className="relative grid place-items-center text-xs"
      data-testid={`dag-node-${n.name}`}
      data-status={status}
      data-family={n.family ?? "none"}
      data-type={n.type}
      style={{
        width: NODE_W,
        height: NODE_H,
        opacity: s.opacity,
      }}
    >
      <Handle type="target" position={Position.Top} className="!bg-ink-500" />
      <div className="relative flex flex-col items-center">
        {running && (
          <span
            className="absolute top-0 h-12 w-12 rounded-full border opacity-50"
            style={{ borderColor: s.ring, animation: "ork-ping 1.8s ease-out infinite" }}
          />
        )}
        <svg width="54" height="54" viewBox="0 0 54 54" aria-hidden="true">
          {/* Outer ring = lifecycle status; inner glyph = model family. */}
          <polygon
            points={hexPoints(27, 27, 25)}
            fill="none"
            stroke={s.ring}
            strokeWidth={status === "failed" ? 2.4 : 1.9}
            strokeDasharray={s.dashed ? "4 3" : undefined}
          />
          <polygon
            points={hexPoints(27, 27, 20)}
            fill={status === "failed" ? "var(--w-red)" : family}
            fillOpacity={status === "failed" ? 0.6 : 0.16}
            stroke={family}
            strokeWidth={1.3}
          />
          <path d="M19 24 L23.5 25.8 M35 24 L30.5 25.8" stroke={family} strokeWidth="2" strokeLinecap="round" />
          <circle cx="21.5" cy="27.5" r="1.7" fill={family} />
          <circle cx="32.5" cy="27.5" r="1.7" fill={family} />
          <path d="M23.5 36 L22.4 31 M30.5 36 L31.6 31" stroke={family} strokeWidth="2" strokeLinecap="round" />
          <path d="M24.5 33.5 Q27 36 29.5 33.5" stroke={family} strokeWidth="1.4" fill="none" strokeLinecap="round" opacity="0.75" />
        </svg>
        <StatusBadge status={status} ring={s.ring} />
        <RuntimeIconBadge badge={runtimeBadge} status={status} />
        <div className="mt-1 max-w-[140px] truncate text-center font-mono text-[10.5px] font-bold text-ink-200">
          {n.name}
        </div>
        <div
          className="max-w-[140px] truncate text-center font-mono text-[9px] font-semibold"
          style={{ color: s.ring }}
        >
          {s.label}
          {n.duration_ms ? (
            <span className="font-normal text-ink-500"> · {Math.round(n.duration_ms / 1000)}s</span>
          ) : null}
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-ink-500" />
    </div>
  );
}

function hexPoints(x: number, y: number, r: number): string {
  const points: string[] = [];
  for (let i = 0; i < 6; i += 1) {
    const angle = (Math.PI / 180) * (60 * i - 90);
    points.push(`${(x + r * Math.cos(angle)).toFixed(1)},${(y + r * Math.sin(angle)).toFixed(1)}`);
  }
  return points.join(" ");
}

function Legend() {
  return (
    <div
      className="flex items-center gap-3 rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2.5 py-1.5"
      data-testid="dag-legend"
    >
      {LEGEND_ORDER.map((key) => {
        const s = STATUS_STYLE[key]!;
        return (
          <span key={key} className="flex items-center gap-1.5 font-mono text-[9.5px] text-ink-400">
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
              <polygon
                points={hexPoints(6, 6, 5)}
                fill="none"
                stroke={s.ring}
                strokeWidth={1.5}
                strokeDasharray={s.dashed ? "2.5 2" : undefined}
              />
            </svg>
            {s.label}
          </span>
        );
      })}
      <span className="border-l border-[var(--hair)] pl-3 font-mono text-[9.5px] text-ink-500">
        glyph colour = model family
      </span>
      <span className="border-l border-[var(--hair)] pl-3 font-mono text-[9.5px] text-ink-500">
        badge = LLM / bash
      </span>
    </div>
  );
}

export function RunDag({
  nodes,
  edges,
  taskRunId,
  compact = false,
}: {
  nodes: DagNode[];
  edges: DagEdge[];
  taskRunId?: string;
  compact?: boolean;
}) {
  const [fullscreen, setFullscreen] = useState(false);
  const { nodes: fNodes, edges: fEdges } = useMemo(() => {
    const byName = new Map(nodes.map((n) => [n.name, visualStatus(n)]));
    return layout(nodes, edges, (name) => byName.get(name) ?? "never_seen");
  }, [nodes, edges]);
  const navigate = useNavigate();

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  if (!nodes.length)
    return (
      <p className="text-sm text-ink-400 p-4" data-testid="dag-empty">
        No DAG for this recipe.
      </p>
    );

  return (
    <div
      className={
        fullscreen
          ? "fixed inset-0 z-50 bg-[var(--bg)]"
          : `${compact ? "h-[330px]" : "h-[420px]"} rounded-[3px] border border-[var(--hair)] bg-[var(--panel)]`
      }
      data-testid="dag-canvas"
      data-fullscreen={fullscreen}
    >
      <ReactFlow
        key={fullscreen ? "dag-fs" : "dag-inline"}
        nodes={fNodes}
        edges={fEdges}
        nodeTypes={{ ork: OrkNode }}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        zoomOnScroll
        panOnDrag
        onNodeClick={(_, node) => {
          if (taskRunId) {
            navigate({
              to: "/runs/$taskRunId/agents/$nodeId",
              params: { taskRunId, nodeId: String(node.id) },
            });
          }
        }}
      >
        <Background color="#1a2228" gap={22} />
        <Controls className="!border-[var(--hair-2)] !bg-[var(--panel-2)] !text-ink-400" />
        <Panel position="top-left">
          <Legend />
        </Panel>
        <Panel position="top-right">
          <button
            type="button"
            className="rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2 py-1 font-mono text-[10px] text-ink-300 hover:border-[var(--hair-2)] hover:text-ink-100"
            data-testid="dag-fullscreen-toggle"
            onClick={() => setFullscreen((v) => !v)}
            title={fullscreen ? "Exit fullscreen (Esc)" : "Fullscreen"}
          >
            {fullscreen ? "✕ exit fullscreen" : "⛶ fullscreen"}
          </button>
        </Panel>
      </ReactFlow>
    </div>
  );
}
