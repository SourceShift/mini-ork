/**
 * Thin API client over the FastAPI observability surface.
 *
 * All endpoints are read-only. In dev the Vite proxy forwards /api → :7090;
 * in prod the FastAPI app serves the SPA + the same /api routes off one origin.
 */

const BASE = "";

// --- workspace (multi-project) --------------------------------------------
// The server resolves each request's .mini-ork home from the X-Mini-Ork-Home
// header (or `home` query param for SSE). The chosen workspace lives in the
// URL (?ws=) so deep links and reloads land in the right project, plus
// localStorage as a fallback for ws-less URLs.

const WS_KEY = "mini-ork.workspace";
let workspaceHome: string | null = null;

export function getWorkspaceHome(): string | null {
  return workspaceHome;
}

export function setWorkspaceHome(home: string | null): void {
  workspaceHome = home;
  try {
    if (home) localStorage.setItem(WS_KEY, home);
    else localStorage.removeItem(WS_KEY);
  } catch {
    // private browsing — header still works for this session
  }
}

/** Call once before first render: URL ?ws= wins, then localStorage. */
export function initWorkspaceFromUrl(): void {
  const ws = new URLSearchParams(window.location.search).get("ws");
  if (ws) {
    setWorkspaceHome(ws);
    return;
  }
  try {
    workspaceHome = localStorage.getItem(WS_KEY);
  } catch {
    workspaceHome = null;
  }
}

/** Append `home=` query param (EventSource can't send headers). */
export function withWorkspaceParam(path: string): string {
  if (!workspaceHome) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}home=${encodeURIComponent(workspaceHome)}`;
}

function wsHeaders(): Record<string, string> {
  return workspaceHome ? { "X-Mini-Ork-Home": workspaceHome } : {};
}

// --- operator token (privileged writes) -----------------------------------
// stop/kill trust the loopback bind, but the cost-approval + steering writes
// are Bearer-gated and fail-closed server-side (see mini_ork/web/auth.py). The
// token identifies the operator on the audit record, so it lives per-browser in
// localStorage and rides along on every request — harmless on endpoints that
// don't check it.

const TOKEN_KEY = "mini-ork.operator-token";
let operatorToken: string | null = null;

export function getOperatorToken(): string | null {
  return operatorToken;
}

export function setOperatorToken(token: string | null): void {
  operatorToken = token && token.trim() ? token.trim() : null;
  try {
    if (operatorToken) localStorage.setItem(TOKEN_KEY, operatorToken);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // private browsing — header still works for this session
  }
}

export function initOperatorTokenFromStorage(): void {
  try {
    operatorToken = localStorage.getItem(TOKEN_KEY);
  } catch {
    operatorToken = null;
  }
}

function authHeaders(): Record<string, string> {
  return operatorToken ? { Authorization: `Bearer ${operatorToken}` } : {};
}

async function get<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...wsHeaders(), ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${body || path}`);
  }
  return (await res.json()) as T;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      ...wsHeaders(),
      ...authHeaders(),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${body || path}`);
  }
  return (await res.json()) as T;
}

// --- types ---------------------------------------------------------------

export type Health = {
  ok: boolean;
  db_path: string;
  has_task_runs: boolean;
  has_mo_events: boolean;
  has_self_improve_runs: boolean;
};

export type TaskRun = {
  id: string;
  task_class: string;
  recipe: string | null;
  workflow_version: string;
  status: "classified" | "planned" | "executing" | "verifying" | "reviewing" | "published" | "rolled_back" | "failed";
  verdict: "APPROVE" | "REQUEST_CHANGES" | "ESCALATE" | "CRASH" | null;
  cost_usd: number;
  duration_ms: number;
  created_at: number;
  updated_at: number;
  ended_at: number | null;
  trace_id: string | null;
  kickoff_path: string;
  plan_path: string | null;
  artifact_path: string | null;
  /** Detail endpoint only: non-terminal run with no activity past the cutoff. */
  stale?: boolean;
  last_activity_at?: number;
};

export type ActiveRun = {
  id: number | string;
  epic_id: string | null;
  run_dir: string;
  branch: string | null;
  agent: string;
  started_at: string | number | null;
  last_heartbeat_at: string | number | null;
  pid: number | null;
  host: string | null;
  test_status: string | null;
  trace_status: string | null;
  cost_usd: number;
  epic_title: string | null;
  epic_status: string | null;
  lane: string | null;
  source: "runs" | "task_runs";
  task_run_id: string | null;
  task_class?: string | null;
  recipe?: string | null;
  status?: string | null;
  verdict?: string | null;
};

export type TaskRunsSummary = {
  by_recipe: { recipe: string; count: number; cost: number }[];
  by_status: { status: string; count: number }[];
  total_cost_usd: number;
};

export type BridgeMethod = "trace_id" | "run_id" | "time-window";

export type RunEvent = {
  id: number | string;
  ts: string | number;
  event_type: string;
  actor: string | null;
  status: string | null;
  duration_ms: number | null;
  cost_usd: number | null;
  artifact_path: string | null;
  payload_json: string | null;
  source: "mo_events" | "run_events";
  bridge: BridgeMethod;
};

export type LlmCall = {
  id: number;
  provider: string;
  model_id: string;
  tier: string;
  feature_name: string;
  actor: string | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  duration_ms: number;
  status: "success" | "failed";
  finish_reason: string | null;
  ts: string;
  bridge: BridgeMethod;
};

export type Correlation = {
  task_run_id: string;
  trace_id: string | null;
  bridge_methods: string[];
  issues: string[];
  remediation: string | null;
};

export type VerifierResult = {
  verifier: string;
  pass: boolean;
  result_file: string;
  log_file: string | null;
  evidence_path: string | null;
  payload: Record<string, unknown>;
};

export type EvidenceRef = { verifier: string; evidence_path: string };

export type Diagnostic = {
  task_run_id: string;
  run_dir: string;
  run_dir_exists: boolean;
  summary: string;
  execute_log: {
    size: number;
    tail: string[];
    failure_lines: { line_no: number; text: string }[];
    total_lines: number;
  } | null;
  verifier_results: VerifierResult[];
  evidence_refs: EvidenceRef[];
  self_improve_notes: { raw: string | null; outcome: string } | null;
  trace_verdicts: Array<{
    trace_id: string;
    task_class: string;
    status: string;
    reviewer_verdict: string | null;
    verifier_excerpt: string | null;
    final_artifact_ref: string | null;
    created_at: string;
  }>;
};

export type EvidenceContent = {
  path: string;
  relpath: string;
  size: number;
  truncated: boolean;
  content: string;
};

export type AgentSummary = {
  node_id: string;
  node_type: string;
  model_lane: string | null;
  family: string | null;
  prompt_ref: string | null;
  verifier_ref: string | null;
  dispatch_mode: string | null;
  gates: string[];
  status: NodeStatus;
  duration_ms: number | null;
  verdict: string | null;
  started_at: number | null;
  ended_at: number | null;
  artifact_files: string[];
  llm_call_count: number;
  llm_cost_usd: number;
  llm_total_tokens: number;
  llm_attribution_fallback: boolean;
};

export type AgentChild = {
  spawn_id: string;
  child_run_id: string;
  depth: number;
  recipe: string | null;
  kickoff_path?: string;
  authority_level?: number;
  allow_child_spawn?: number;
  status: string;
  created_at: number;
};

export type AgentListResponse = {
  task_run: TaskRun;
  recipe: string | null;
  edges: DagEdge[];
  agents: AgentSummary[];
  children: AgentChild[];
};

export type AgentTurn = {
  turn_index: number;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  stop_reason: string | null;
  session_id: string | null;
  text: string;
  tool_uses: Array<{ id: string; name: string; input: Record<string, unknown> }>;
};

export type AgentTranscript = {
  turns: AgentTurn[];
  available: boolean;
  reason?: string;
  transcript_path?: string;
  truncated?: boolean;
  fallback?: "text-output";
};

export type AgentDetail = {
  task_run_id: string;
  node: DagNode;
  status: NodeStatus;
  duration_ms: number | null;
  verdict: string | null;
  started_at: number | null;
  ended_at: number | null;
  prompt: { path: string | null; content: string | null };
  artifacts: ArtifactContent[];
  llm_calls: (LlmCall & { traceparent?: string; error_message?: string | null; metadata_json?: string })[];
  transcript: AgentTranscript;
  children: AgentChild[];
};

export type RunInput = {
  key: "kickoff" | "plan" | "run_profile" | "profile_answers" | string;
  label: string;
  path: string;
  name: string;
  kind: "markdown" | "plan" | "json" | string;
  size: number;
  mtime: number;
};

export type RunInputContent = RunInput & {
  content: string;
};

export type LearningAttribution = {
  node_id: string | null;
  source: string;
  confidence: "high" | "medium" | "low" | "mixed" | "none" | string;
  agent_version_id?: string;
  candidate_node_ids?: string[];
};

export type LearningGradient = {
  gradient_id: string;
  target: string;
  signal: string;
  suggested_change: string;
  evidence: string;
  confidence: number;
  created_at: number;
  agent_attribution: LearningAttribution;
};

export type LearningPattern = {
  pattern_id: string;
  description: string;
  evidence_trace_ids: string[];
  frequency: number;
  first_seen: string;
  last_seen: string;
  output_type: string;
  promoted_to: string | null;
  status: string;
  agent_attribution: LearningAttribution;
};

export type LearningRecord = {
  id: number;
  run_id: string;
  iter: number;
  rank: number;
  category: string;
  title: string;
  evidence_paths: string[];
  arxiv_refs: string[];
  patch_summary: string | null;
  outcome: string;
  severity: string;
  confidence: number;
  benchmark_delta: number | null;
  created_at: number;
  updated_at: number;
  agent_attribution: LearningAttribution;
};

export type PriorSimilarRun = {
  trace_id: string;
  task_class: string;
  status: string;
  cost_usd: number;
  duration_ms: number;
  reviewer_verdict: string | null;
  final_artifact_ref: string | null;
  created_at: string;
};

export type LearningInjectionPoint = {
  name: string;
  where: string;
  how: string;
  wired?: boolean;
};

export type RunLearning = {
  task_run_id: string;
  task_class: string;
  trace_id: string | null;
  summary: {
    gradients_produced: number;
    patterns_evidenced: number;
    learning_records: number;
    prior_similar_runs_available: number;
    known_failure_modes_available: number;
  };
  produced: {
    gradients: LearningGradient[];
    patterns: LearningPattern[];
  };
  self_improve: {
    records: LearningRecord[];
  };
  injected_candidates: {
    prior_similar_runs: PriorSimilarRun[];
    known_failure_modes: LearningGradient[];
    source: string;
    injection_points: LearningInjectionPoint[];
  };
};

export type GlobalGradient = {
  gradient_id: string;
  target: string;
  signal: string;
  suggested_change: string;
  confidence: number;
  created_at: number;
};

export type ArtifactEntry = {
  name: string;
  relpath: string;
  size: number;
  mtime: number;
  kind: "markdown" | "json" | "log" | "yaml" | "shell" | "other";
};

export type ArtifactContent = ArtifactEntry & {
  content: string;
  truncated: boolean;
  binary?: boolean;
};

export type NodeStatus = "never_seen" | "running" | "done" | "failed";

export type DagNode = {
  name: string;
  type: string;
  lane: string | null;
  family: string | null;
  prompt_ref: string | null;
  verifier_ref: string | null;
  dispatch_mode: string | null;
  gates: string[];
  status?: NodeStatus;
  started_at?: number | string | null;
  ended_at?: number | string | null;
  duration_ms?: number | null;
  verdict?: string | null;
  artifact_path?: string | null;
};

export type DagEdge = { from: string; to: string; edge_type: string };

export type Fingerprint = {
  recipe: string;
  nodes: DagNode[];
  edges: DagEdge[];
  families_used: Record<string, number>;
  dominant_family: string | null;
  dominant_share: number;
  coalition: "heterogeneous" | "low" | "medium" | "high";
};

export type SelfImproveRun = {
  run_id: string;
  iter: number;
  outcome: "pending" | "success" | "partial" | "rejected" | "failed" | "converged" | "timed_out" | "aborted";
  started_at: number;
  finished_at: number | null;
  soft_deadline_at: number;
  hard_deadline_at: number;
  parent_run_id: string | null;
  branch_name: string | null;
  worktree_path: string | null;
  notes: string | null;
};

export type ParsedNote = { key: string; value: string; kind: "flag" | "kv" | "sha" };

export type SelfImproveDetail = SelfImproveRun & {
  parsed_notes: ParsedNote[];
  children: Array<Pick<SelfImproveRun, "run_id" | "iter" | "outcome" | "started_at" | "finished_at">>;
  siblings: Array<Pick<SelfImproveRun, "run_id" | "iter" | "outcome">>;
  linked_task_run: TaskRun | null;
};

export type IdeaTreeStatus = "harvested" | "pruned" | "pending" | "running" | "rejected" | string;

export type IdeaTreeRoot = {
  node_id: string;
  recipe: string | null;
  hypothesis: string | null;
  status: IdeaTreeStatus;
  created_at: string | null;
  updated_at: string | null;
  node_count: number;
  harvested_count: number;
  pruned_count: number;
};

export type IdeaTreeNode = {
  node_id: string;
  parent_node_id: string | null;
  recipe: string | null;
  task_run_id: string | null;
  self_improve_run_id: string | null;
  hypothesis: string | null;
  status: IdeaTreeStatus;
  score_dev: number | null;
  score_test: number | null;
  insights: unknown[];
  depth: number;
  created_at: string | null;
  updated_at: string | null;
};

export type IdeaTreeEdge = { from: string; to: string };

export type IdeaTreeResponse = {
  root_node_id: string;
  nodes: IdeaTreeNode[];
  edges: IdeaTreeEdge[];
  stats: {
    total?: number;
    by_status?: Record<string, number>;
    max_depth?: number;
  };
};

export type IdeaTreeNodeDetail = Omit<IdeaTreeNode, "depth"> & {
  root_node_id: string;
  children: Array<Pick<IdeaTreeNode, "node_id" | "hypothesis" | "status" | "score_dev" | "score_test">>;
};

export type CostByDayRow = { day: string; recipe: string; cost: number; run_count: number };
export type WallTimeRow = { day: string; recipe: string; avg_ms: number; max_ms: number; run_count: number };

// --- dispatch (the composer: price an edit against measured evidence) ------
// Mirrors mini_ork/web/routes/dispatch.py. HONESTY CONTRACT: any lane/topology
// with evidence:"none" has fewer than `min_samples` runs — success_rate/win_rate
// and ci are null, and the UI must NOT invent a number for it. A 75% from n=4 is
// a coincidence with a percent sign; the backend refuses to ship it, so must we.

export type Evidence = "measured" | "none";

export type DispatchRecipeNode = {
  name: string | null;
  type: string | null;
  lane: string | null;
  dispatch_mode: string;
  gates: string[];
  verifier_ref: string | null;
};

export type DispatchRecipe = {
  recipe: string;
  task_class: string | null;
  description: string | null;
  nodes: DispatchRecipeNode[];
};

export type DispatchLane = {
  lane: string;
  task_class: string | null;
  runs: number;
  successes: number;
  avg_cost_usd: number | null;
  avg_duration_ms: number | null;
  advantage: number | null;
  top_failure_modes: string | null;
  /** null when evidence==="none" (thin sample). Never render a fabricated rate. */
  success_rate: number | null;
  /** Wilson 95% interval [lo, hi]; null when thin. */
  ci: [number, number] | null;
  evidence: Evidence;
};

export type DispatchTopology = {
  topology_id: string;
  workflow_name: string | null;
  task_class: string | null;
  wins: number;
  losses: number;
  sample_size: number;
  win_rate: number | null;
  ci: [number, number] | null;
  avg_cost_usd: number | null;
  avg_duration_ms: number | null;
  evidence: Evidence;
};

export type DispatchOptions = {
  recipes: DispatchRecipe[];
  lanes: DispatchLane[];
  topologies: DispatchTopology[];
  min_samples: number;
  note: string;
};

export type DispatchRequest = {
  kickoff: string;
  recipe: string;
  task_class?: string | null;
  worktree?: string | null;
  /** What the machine suggested before the human touched it — half the label. */
  proposed_recipe?: string | null;
  proposed_topology?: string | null;
  proposed_lane_hints?: string | null;
  /** What the human chose. node → lane; rides into the run as MINI_ORK_LANE_<NODE>. */
  lane_overrides?: Record<string, string>;
  override_reason?: string | null;
  epic_id?: string | null;
};

export type DispatchResponse = {
  run_id: string;
  command: string;
  env: Record<string, string>;
  decision_id: number | null;
  decided_by: "human" | "conductor";
  overrode: boolean;
};

// --- durable-dag recovery projection --------------------------------------

export type RecoveryProjection = {
  run_id?: string;
  status?: string;
  nodes: Array<Record<string, unknown>>;
  [k: string]: unknown;
};

// --- endpoints -----------------------------------------------------------

export type Project = {
  home: string;
  name: string;
  exists: boolean;
  active: boolean;
};

export const api = {
  health: () => get<Health>("/api/v1/health"),
  projects: () => get<{ active: string; projects: Project[] }>("/api/v1/projects"),
  validateProject: (path: string) =>
    get<{ ok: boolean; error?: string; home?: string; name?: string; registered?: boolean }>(
      `/api/v1/projects/validate?path=${encodeURIComponent(path)}`,
    ),
  addProject: (home: string) => post<{ ok: boolean; project: Project }>("/api/v1/projects/add", { home }),
  browseDirs: (path?: string) =>
    get<{
      path: string;
      parent: string | null;
      is_project: boolean;
      dirs: { name: string; path: string; is_project: boolean }[];
    }>(`/api/v1/projects/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  removeProject: (home: string) => post<{ ok: boolean; removed: string }>("/api/v1/projects/remove", { home }),
  switchProject: (home: string) =>
    post<{ ok: boolean; active: string; name: string }>("/api/v1/projects/switch", { home }),
  activeRuns: () => get<ActiveRun[]>("/api/v1/runs/active"),
  taskRuns: (opts: { limit?: number; recipe?: string; status?: string; verdict?: string } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.recipe) q.set("recipe", opts.recipe);
    if (opts.status) q.set("status", opts.status);
    if (opts.verdict) q.set("verdict", opts.verdict);
    const qs = q.toString();
    return get<TaskRun[]>(`/api/v1/task-runs${qs ? `?${qs}` : ""}`);
  },
  taskRunsSummary: () => get<TaskRunsSummary>("/api/v1/task-runs/summary"),
  taskRun: (id: string) => get<TaskRun>(`/api/v1/task-runs/${encodeURIComponent(id)}`),
  events: (id: string) => get<RunEvent[]>(`/api/v1/task-runs/${encodeURIComponent(id)}/events`),
  llmCalls: (id: string) => get<LlmCall[]>(`/api/v1/task-runs/${encodeURIComponent(id)}/llm-calls`),
  artifacts: (id: string) => get<ArtifactEntry[]>(`/api/v1/task-runs/${encodeURIComponent(id)}/artifacts`),
  artifact: (id: string, relpath: string) =>
    get<ArtifactContent>(`/api/v1/task-runs/${encodeURIComponent(id)}/artifacts/${relpath.split("/").map(encodeURIComponent).join("/")}`),
  dag: (id: string) => get<Fingerprint & { task_run_id: string }>(`/api/v1/task-runs/${encodeURIComponent(id)}/dag`),
  correlation: (id: string) => get<Correlation>(`/api/v1/task-runs/${encodeURIComponent(id)}/correlation`),
  why: (id: string) => get<Diagnostic>(`/api/v1/task-runs/${encodeURIComponent(id)}/why`),
  agents: (id: string) => get<AgentListResponse>(`/api/v1/task-runs/${encodeURIComponent(id)}/agents`),
  agent: (id: string, nodeId: string) =>
    get<AgentDetail>(`/api/v1/task-runs/${encodeURIComponent(id)}/agents/${encodeURIComponent(nodeId)}`),
  inputs: (id: string) => get<RunInput[]>(`/api/v1/task-runs/${encodeURIComponent(id)}/inputs`),
  input: (id: string, key: string) =>
    get<RunInputContent>(`/api/v1/task-runs/${encodeURIComponent(id)}/inputs/${encodeURIComponent(key)}`),
  learning: (id: string) => get<RunLearning>(`/api/v1/task-runs/${encodeURIComponent(id)}/learning`),
  // ── interactive Q&A ──
  profile: (id: string) =>
    get<{
      task_run_id: string;
      profile_status: string;
      confidence: number;
      questions: (string | { text?: string; question?: string })[];
      answers: Record<string, string>;
      needs_answers: boolean;
      profile_path: string | null;
      answers_path: string | null;
    }>(`/api/v1/task-runs/${encodeURIComponent(id)}/profile`),
  saveAnswers: async (id: string, answers: Record<string, string>) => {
    const res = await fetch(`/api/v1/task-runs/${encodeURIComponent(id)}/answers`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", ...wsHeaders() },
      body: JSON.stringify(answers),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`${res.status} ${res.statusText} — ${body}`);
    }
    return (await res.json()) as {
      ok: boolean;
      task_run_id: string;
      answers_saved: string[];
      next_step_cli: string;
      note: string;
    };
  },
  // ── control plane (POST) ──
  stop: (id: string) =>
    post<{ ok: boolean; task_run_id: string; action: string; flag_path: string; note: string }>(
      `/api/v1/task-runs/${encodeURIComponent(id)}/stop`,
    ),
  kill: (id: string) =>
    post<{
      ok: boolean;
      task_run_id: string;
      action: string;
      pids_targeted: number[];
      pids_signaled: number[];
      pids_survived_permission_denied: number[];
      note: string;
    }>(`/api/v1/task-runs/${encodeURIComponent(id)}/kill`),
  evidence: (id: string, path: string) =>
    get<EvidenceContent>(`/api/v1/task-runs/${encodeURIComponent(id)}/evidence?path=${encodeURIComponent(path)}`),
  selfImprove: (limit = 100) => get<SelfImproveRun[]>(`/api/v1/trajectory/self-improve?limit=${limit}`),
  gradients: (limit = 25) => get<GlobalGradient[]>(`/api/v1/trajectory/gradients?limit=${limit}`),
  selfImproveDetail: (runId: string) =>
    get<SelfImproveDetail>(`/api/v1/trajectory/self-improve/${encodeURIComponent(runId)}`),
  ideaTree: {
    roots: () => get<IdeaTreeRoot[]>("/api/v1/idea-tree/roots"),
    read: (rootId: string) => get<IdeaTreeResponse>(`/api/v1/idea-tree/${encodeURIComponent(rootId)}`),
    node: (nodeId: string) => get<IdeaTreeNodeDetail>(`/api/v1/idea-tree/node/${encodeURIComponent(nodeId)}`),
  },
  costByDay: () => get<CostByDayRow[]>("/api/v1/trajectory/cost-by-day"),
  wallTime: () => get<WallTimeRow[]>("/api/v1/trajectory/wall-time"),
  recipes: () => get<string[]>("/api/v1/fingerprint/recipes"),
  fingerprint: (recipe: string) => get<Fingerprint>(`/api/v1/fingerprint?recipe=${encodeURIComponent(recipe)}`),
  // ── dispatch (compose + decide, does NOT spawn) ──
  dispatchOptions: (taskClass?: string) =>
    get<DispatchOptions>(`/api/v1/dispatch/options${taskClass ? `?task_class=${encodeURIComponent(taskClass)}` : ""}`),
  dispatch: (req: DispatchRequest) => post<DispatchResponse>("/api/v1/dispatch", req),
  // ── privileged control (Bearer-token gated server-side) ──
  pauseCost: (id: string, thresholdUsd = 25.0) =>
    post<{ ok: boolean; task_run_id: string; operator?: string; note?: string }>(
      `/api/v1/task-runs/${encodeURIComponent(id)}/pause-cost`,
      { threshold_usd: thresholdUsd },
    ),
  resumeCost: (id: string) =>
    post<{ ok: boolean; task_run_id: string; operator?: string; note?: string }>(
      `/api/v1/task-runs/${encodeURIComponent(id)}/resume-cost`,
    ),
  steer: (
    id: string,
    opts: {
      message: string;
      role_target?: "planner" | "implementer" | "reviewer" | "verifier" | "any";
      severity?: "info" | "warn" | "critical";
      confidence?: number;
      ttl_secs?: number;
    },
  ) =>
    post<{ ok: boolean; task_run_id: string; operator?: string; note?: string }>(
      `/api/v1/task-runs/${encodeURIComponent(id)}/steer`,
      {
        message: opts.message,
        role_target: opts.role_target ?? "any",
        severity: opts.severity ?? "info",
        confidence: opts.confidence ?? 0.8,
        ttl_secs: opts.ttl_secs ?? 3600,
      },
    ),
  recovery: (runId: string) =>
    get<RecoveryProjection>(`/api/v1/runs/${encodeURIComponent(runId)}/recovery`),
};

/** WebSocket URL for the opt-in PTY bridge (server gates on MO_PTY_ENABLED=1).
 * EventSource/WebSocket can't send headers, so workspace rides as ?home=. */
export function ptyWebsocketUrl(opts: { runId?: string; cmd?: "shell" | "opencode"; cols: number; rows: number }): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const q = new URLSearchParams();
  if (opts.runId) q.set("run_id", opts.runId);
  q.set("cmd", opts.cmd ?? "shell");
  q.set("cols", String(opts.cols));
  q.set("rows", String(opts.rows));
  if (workspaceHome) q.set("home", workspaceHome);
  return `${proto}//${window.location.host}/api/v1/pty?${q.toString()}`;
}
