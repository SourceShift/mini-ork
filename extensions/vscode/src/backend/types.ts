// Wire types for the mini-ork observability API.
//
// These mirror the FastAPI payloads served by `mini-ork serve` (see
// `mini_ork/web/routes/*.py` and the live `/openapi.json`). They are
// deliberately permissive: mini-ork adds fields between releases, and a
// missing optional field must not blank out a whole view.

export interface Health {
  ok: boolean;
  db_path?: string;
  has_task_runs?: boolean;
  has_mo_events?: boolean;
  has_self_improve_runs?: boolean;
}

/** The UI-shaped run card served by `/ide/runs`. */
export interface RunCard {
  id: string;
  title: string;
  recipe?: string;
  state: string;
  mark?: string;
  step?: string;
  started_at?: number | null;
  ended_at?: number | null;
  cost_usd?: number | null;
  added?: number | null;
  removed?: number | null;
  has_workspace?: boolean;
  verdict?: string | null;
  error?: string | null;
}

export interface RunsResponse {
  ok: boolean;
  runs: RunCard[];
}

export interface RunCardResponse {
  ok: boolean;
  run?: RunCard;
  /** Present when the server could not resolve the id. */
  error?: string;
}

/** The raw `task_runs` row served by `/api/v1/task-runs`. */
export interface TaskRun {
  id: string;
  task_class: string;
  recipe: string;
  workflow_version?: string;
  status: string;
  verdict?: string | null;
  cost_usd?: number | null;
  duration_ms?: number | null;
  created_at?: number;
  updated_at?: number;
  ended_at?: number | null;
  trace_id?: string | null;
  kickoff_path?: string | null;
  plan_path?: string | null;
  [key: string]: unknown;
}

export interface RecipeOption {
  recipe: string;
  task_class: string;
  description?: string;
}

export interface DispatchOptions {
  recipes: RecipeOption[];
  [key: string]: unknown;
}

export interface LearningPattern {
  pattern_id: string;
  cluster_label: string;
  strength_score: number;
  suggested_meta_adr?: string;
  status: string;
  detected_at?: number;
  resolved_at?: number | null;
}

export interface LearningSummary {
  bandit_arms: number;
  gradients: number;
  promotions: number;
  promoted: number;
  prompt_versions_scored: number;
  failures: number;
  semantic_memories: number;
  semantic_memory_uses: number;
  patterns: number;
  topologies: number;
  open_circuit_breakers: number;
  traces: number;
  llm_calls: number;
}

/** Board header — the shell poll the Zed surface reads every few seconds. */
export interface BoardHeader {
  project: string;
  branch?: string;
  worktrees?: number;
  needs_you?: number;
  working?: number;
  failed?: number;
  done?: number;
  today_usd?: number;
  cap_usd?: number;
  scheduler_on?: boolean;
  contextnest?: boolean;
  version?: string;
  nodes?: { configured: number; names: string[] };
  lanes?: Array<{
    lane: string;
    calls: number;
    failed: number;
    usd: number;
    last_error?: string;
  }>;
}

export interface BoardShell {
  version: number;
  project: string;
  home: string;
  generated_at: number;
  header: BoardHeader;
  runs?: RunCard[];
  [key: string]: unknown;
}

/** A node in a run's DAG. Fields vary by node type; all optional. */
export interface DagNode {
  node_id: string;
  type?: string;
  status?: string;
  phase?: string;
  started_at?: number | null;
  ended_at?: number | null;
  cost_usd?: number | null;
  verdict?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

export interface DagResponse {
  nodes?: DagNode[];
  edges?: Array<{ from: string; to: string; [key: string]: unknown }>;
  [key: string]: unknown;
}

export type LaunchResult = {
  ok: boolean;
  run_id?: string;
  pid?: number;
  operator?: string;
  error?: string;
} & Record<string, unknown>;

/** Server-sent event frames on `/sockets/events/{conversation_id}`. */
export interface StreamEvent {
  type?: string;
  event?: string;
  run_id?: string;
  node_id?: string;
  at?: number;
  payload?: unknown;
  [key: string]: unknown;
}
