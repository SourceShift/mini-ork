import { useMemo } from "react";
import { useNavigate } from "@tanstack/react-router";
import {
  Background,
  Controls,
  Handle,
  type Node,
  type Edge,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { DagEdge, DagNode } from "@/lib/api";
import { familyColor } from "@/lib/format";

const NODE_W = 150;
const NODE_H = 82;

function layout(nodes: DagNode[], edges: DagEdge[]): { nodes: Node[]; edges: Edge[] } {
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
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      width: NODE_W,
      height: NODE_H,
    };
  });

  const flowEdges: Edge[] = edges.map((e, i) => ({
    id: `e${i}`,
    source: e.from,
    target: e.to,
    label: e.edge_type === "depends_on" ? undefined : e.edge_type,
    labelStyle: { fontSize: 9, fill: "var(--tx-4)", fontFamily: "var(--mono)" },
    style: { stroke: "#242e35", strokeWidth: 1.2 },
    animated: false,
  }));

  return { nodes: flowNodes, edges: flowEdges };
}

const STATUS_STYLE: Record<string, { ring: string; dot: string; opacity: number; label: string }> = {
  never_seen: { ring: "#64748b", dot: "#64748b", opacity: 0.62, label: "queued" },
  startup:    { ring: "#f59e0b", dot: "#f59e0b", opacity: 1.0,  label: "startup" },
  running:    { ring: "#22c55e", dot: "#22c55e", opacity: 1.0,  label: "running" },
  done:       { ring: "#c084fc", dot: "#c084fc", opacity: 1.0,  label: "succeeded" },
  failed:     { ring: "#ef4444", dot: "#ef4444", opacity: 1.0,  label: "failed" },
};

function visualStatus(n: DagNode): string {
  if (n.status === "running" && !n.started_at) return "startup";
  return n.status ?? "never_seen";
}

function OrkNode({ data }: { data: { node: DagNode } }) {
  const n = data.node;
  const status = visualStatus(n);
  const s = STATUS_STYLE[status]!;
  const color = n.family ? familyColor(n.family) : s.ring;
  const failed = status === "failed";
  const running = status === "running";
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
      <Handle type="target" position={Position.Left} className="!bg-ink-500" />
      <div className="relative flex flex-col items-center">
        {running && (
          <span
            className="absolute top-0 h-12 w-12 rounded-full border opacity-50"
            style={{ borderColor: "var(--grn)", animation: "ork-ping 1.8s ease-out infinite" }}
          />
        )}
        <svg width="54" height="54" viewBox="0 0 54 54" aria-hidden="true">
          <polygon
            points={hexPoints(27, 27, 22)}
            fill={failed ? "var(--w-red)" : color}
            fillOpacity={failed ? 0.75 : 0.16}
            stroke={failed ? "var(--red)" : color}
            strokeWidth={failed ? 2.1 : 1.6}
          />
          <path d="M18 23 L23 25 M36 23 L31 25" stroke={failed ? "var(--red)" : color} strokeWidth="2" strokeLinecap="round" />
          <circle cx="21" cy="27" r="1.8" fill={failed ? "var(--red)" : color} />
          <circle cx="33" cy="27" r="1.8" fill={failed ? "var(--red)" : color} />
          <path d="M23 37 L21.8 31 M31 37 L32.2 31" stroke={failed ? "var(--red)" : color} strokeWidth="2" strokeLinecap="round" />
          <path d="M24 34 Q27 37 30 34" stroke={failed ? "var(--red)" : color} strokeWidth="1.5" fill="none" strokeLinecap="round" opacity="0.75" />
        </svg>
        {failed && (
          <span className="absolute right-10 top-1 grid h-4 w-4 place-items-center rounded-full border border-[var(--red)] bg-[var(--bg)] text-[10px] text-[var(--red)]">
            !
          </span>
        )}
        <div className="mt-1 max-w-[140px] truncate text-center font-mono text-[10.5px] font-bold text-ink-200">
          {n.name}
        </div>
        <div className="max-w-[140px] truncate text-center font-mono text-[9px] text-ink-500">
          {s.label}{n.duration_ms ? ` · ${Math.round(n.duration_ms / 1000)}s` : ""}
        </div>
      </div>
      <Handle type="source" position={Position.Right} className="!bg-ink-500" />
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
  const { nodes: fNodes, edges: fEdges } = useMemo(() => layout(nodes, edges), [nodes, edges]);
  const navigate = useNavigate();

  if (!nodes.length)
    return (
      <p className="text-sm text-ink-400 p-4" data-testid="dag-empty">
        No DAG for this recipe.
      </p>
    );

  return (
    <div
      className={`${compact ? "h-[330px]" : "h-[420px]"} rounded-[3px] border border-[var(--hair)] bg-[var(--panel)]`}
      data-testid="dag-canvas"
    >
      <ReactFlow
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
      </ReactFlow>
    </div>
  );
}
