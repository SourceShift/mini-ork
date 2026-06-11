import { useMemo } from "react";
import { Link } from "@tanstack/react-router";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  type Edge,
  type Node,
  Panel,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { IdeaTreeEdge, IdeaTreeNode, IdeaTreeResponse } from "@/lib/api";

const NODE_W = 210;
const NODE_H = 104;

const STATUS_STYLE: Record<
  string,
  { ring: string; fill: string; label: string; muted?: boolean; animated?: boolean }
> = {
  harvested: { ring: "var(--grn)", fill: "rgba(34, 197, 94, 0.13)", label: "harvested" },
  pruned: { ring: "var(--red)", fill: "rgba(240, 88, 78, 0.14)", label: "pruned" },
  pending: { ring: "var(--amb)", fill: "rgba(230, 176, 74, 0.13)", label: "pending" },
  running: { ring: "#38bdf8", fill: "rgba(56, 189, 248, 0.13)", label: "running", animated: true },
  rejected: { ring: "#64748b", fill: "rgba(100, 116, 139, 0.13)", label: "rejected", muted: true },
};

const LEGEND_ORDER = ["harvested", "pruned", "pending", "running", "rejected"] as const;

function styleFor(status: string) {
  return STATUS_STYLE[status] ?? STATUS_STYLE.pending!;
}

function layout(nodes: IdeaTreeNode[], edges: IdeaTreeEdge[]): { nodes: Node[]; edges: Edge[] } {
  const byDepth = new Map<number, IdeaTreeNode[]>();
  nodes.forEach((node) => {
    const depth = Number.isFinite(node.depth) ? node.depth : 0;
    if (!byDepth.has(depth)) byDepth.set(depth, []);
    byDepth.get(depth)!.push(node);
  });

  for (const layer of byDepth.values()) {
    layer.sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.node_id.localeCompare(b.node_id));
  }

  const flowNodes: Node[] = nodes.map((node) => {
    const depth = Number.isFinite(node.depth) ? node.depth : 0;
    const layer = byDepth.get(depth) ?? [];
    const idx = layer.findIndex((candidate) => candidate.node_id === node.node_id);
    return {
      id: node.node_id,
      type: "idea",
      data: { node },
      position: {
        x: Math.max(0, idx) * (NODE_W + 72),
        y: depth * (NODE_H + 62),
      },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      width: NODE_W,
      height: NODE_H,
    };
  });

  const flowEdges: Edge[] = edges.map((edge, index) => {
    const child = nodes.find((node) => node.node_id === edge.to);
    const status = child ? styleFor(child.status) : STATUS_STYLE.pending!;
    return {
      id: `idea-edge-${index}`,
      source: edge.from,
      target: edge.to,
      animated: Boolean(status.animated),
      style: {
        stroke: status.ring,
        strokeWidth: status.animated ? 1.7 : 1.2,
        opacity: status.muted ? 0.55 : 0.85,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: status.ring,
        width: 16,
        height: 16,
      },
    };
  });

  return { nodes: flowNodes, edges: flowEdges };
}

function IdeaNode({ data }: { data: { node: IdeaTreeNode } }) {
  const node = data.node;
  const style = styleFor(node.status);
  const score = [scoreLabel("dev", node.score_dev), scoreLabel("test", node.score_test)].filter(Boolean).join(" · ");

  const body = (
    <div
      className="relative h-full w-full rounded-[3px] border px-3 py-2 text-left shadow-[0_8px_28px_rgba(0,0,0,0.18)]"
      data-testid={`idea-tree-node-${node.node_id}`}
      data-status={node.status}
      style={{
        borderColor: style.ring,
        background: style.fill,
        opacity: style.muted ? 0.72 : 1,
      }}
    >
      <Handle type="target" position={Position.Top} className="!bg-ink-500" />
      {style.animated ? (
        <span
          className="absolute right-2 top-2 h-2 w-2 rounded-full"
          style={{ background: style.ring, animation: "ork-ping 1.2s ease-out infinite" }}
        />
      ) : null}
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: style.ring }} />
        <span className="truncate font-mono text-[10px] font-bold uppercase text-ink-300">{style.label}</span>
        <span className="ml-auto shrink-0 font-mono text-[9px] text-ink-500">d{node.depth}</span>
      </div>
      <div className="mt-2 line-clamp-3 min-h-[42px] text-[11.5px] leading-snug text-ink-100">
        {node.hypothesis || node.node_id}
      </div>
      <div className="mt-2 flex items-center justify-between gap-2 font-mono text-[9.5px] text-ink-500">
        <span className="truncate">{node.recipe ?? "no recipe"}</span>
        {score ? <span className="shrink-0 text-ink-400">{score}</span> : null}
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-ink-500" />
    </div>
  );

  if (node.task_run_id) {
    return (
      <Link
        to="/runs/$taskRunId"
        params={{ taskRunId: node.task_run_id }}
        className="block h-full w-full cursor-pointer no-underline"
        aria-label={`Open task run for ${node.node_id}`}
      >
        {body}
      </Link>
    );
  }

  if (node.self_improve_run_id) {
    return (
      <Link
        to="/trajectory/self-improve/$runId"
        params={{ runId: node.self_improve_run_id }}
        className="block h-full w-full cursor-pointer no-underline"
        aria-label={`Open self-improve run for ${node.node_id}`}
      >
        {body}
      </Link>
    );
  }

  return body;
}

function scoreLabel(label: string, value: number | null): string | null {
  if (value == null) return null;
  return `${label} ${Number(value).toFixed(2)}`;
}

function Legend() {
  return (
    <div
      className="flex items-center gap-3 rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2.5 py-1.5"
      data-testid="idea-tree-legend"
    >
      {LEGEND_ORDER.map((key) => {
        const style = STATUS_STYLE[key]!;
        return (
          <span key={key} className="flex items-center gap-1.5 font-mono text-[9.5px] text-ink-400">
            <span className="h-2 w-2 rounded-full" style={{ background: style.ring }} />
            {style.label}
          </span>
        );
      })}
    </div>
  );
}

export function IdeaTreePanel({ tree }: { tree: IdeaTreeResponse | null | undefined }) {
  const { nodes, edges } = useMemo(() => {
    return layout(tree?.nodes ?? [], tree?.edges ?? []);
  }, [tree?.nodes, tree?.edges]);

  if (!tree?.nodes.length) {
    return (
      <p className="p-4 text-sm text-ink-400" data-testid="idea-tree-empty">
        No hypothesis tree nodes recorded.
      </p>
    );
  }

  return (
    <div
      className="h-[460px] rounded-[3px] border border-[var(--hair)] bg-[var(--panel)]"
      data-testid="idea-tree-canvas"
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={{ idea: IdeaNode }}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        zoomOnScroll
        panOnDrag
      >
        <Background color="#1a2228" gap={22} />
        <Controls className="!border-[var(--hair-2)] !bg-[var(--panel-2)] !text-ink-400" />
        <Panel position="top-left">
          <Legend />
        </Panel>
        <Panel position="top-right">
          <div className="rounded-[3px] border border-[var(--hair)] bg-[var(--panel-2)] px-2 py-1 font-mono text-[10px] text-ink-400">
            {tree.stats.total ?? tree.nodes.length} nodes · depth {tree.stats.max_depth ?? 0}
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}
