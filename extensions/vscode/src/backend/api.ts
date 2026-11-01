import {
  BoardShell,
  DagResponse,
  DispatchOptions,
  Health,
  LaunchResult,
  LearningPattern,
  LearningSummary,
  RunCard,
  RunsResponse,
  TaskRun,
} from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly path: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
  timeoutMs?: number;
  /** Write endpoints need a bearer token; reads do not. */
  auth?: boolean;
}

/**
 * Typed client for the mini-ork observability API.
 *
 * Every call goes out from the extension host, never from a webview. VS Code
 * webviews have an opaque `vscode-webview://` origin that the server's CORS
 * allow-list cannot name, so a webview-side fetch would be blocked; proxying
 * through this host-side client sidesteps CORS entirely and keeps the bearer
 * token out of the rendered page.
 */
export class MiniOrkApi {
  constructor(
    private readonly getBaseUrl: () => string | undefined,
    private readonly getToken: () => string | undefined,
  ) {}

  get baseUrl(): string | undefined {
    return this.getBaseUrl();
  }

  private url(path: string): string {
    const base = this.getBaseUrl();
    if (!base) {
      throw new ApiError('mini-ork backend is not running', 0, path);
    }
    const suffix = path.startsWith('/') ? path : `/${path}`;
    return `${base}${suffix}`;
  }

  async request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const { method = 'GET', body, timeoutMs = 20_000, auth = false } = opts;
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    if (auth) {
      const token = this.getToken();
      if (!token) {
        throw new ApiError(
          'no auth token: write endpoints are fail-closed. Create one with `mini-ork auth` or set miniOrk.authToken.',
          401,
          path,
        );
      }
      headers.Authorization = `Bearer ${token}`;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(this.url(path), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await res.text();
      if (!res.ok) {
        throw new ApiError(extractDetail(text) ?? `HTTP ${res.status}`, res.status, path);
      }
      return (text ? JSON.parse(text) : {}) as T;
    } catch (err) {
      if (err instanceof ApiError) {
        throw err;
      }
      if (err instanceof Error && err.name === 'AbortError') {
        throw new ApiError(`request timed out after ${timeoutMs}ms`, 0, path);
      }
      throw new ApiError(err instanceof Error ? err.message : String(err), 0, path);
    } finally {
      clearTimeout(timer);
    }
  }

  /** WebSocket URL for the same origin this client talks to. */
  wsUrl(path: string): string | undefined {
    const base = this.getBaseUrl();
    if (!base) {
      return undefined;
    }
    const suffix = path.startsWith('/') ? path : `/${path}`;
    return `${base.replace(/^http/, 'ws')}${suffix}`;
  }

  // --- reads -------------------------------------------------------------

  health(): Promise<Health> {
    return this.request<Health>('/api/v1/health', { timeoutMs: 3_000 });
  }

  runs(): Promise<RunsResponse> {
    return this.request<RunsResponse>('/ide/runs');
  }

  taskRuns(): Promise<TaskRun[]> {
    return this.request<TaskRun[]>('/api/v1/task-runs');
  }

  boardShell(): Promise<BoardShell> {
    return this.request<BoardShell>('/ide/board?shell=true');
  }

  board(): Promise<BoardShell> {
    return this.request<BoardShell>('/ide/board');
  }

  recipes(): Promise<DispatchOptions> {
    return this.request<DispatchOptions>('/api/v1/dispatch/options');
  }

  dag(runId: string): Promise<DagResponse> {
    return this.request<DagResponse>(
      `/api/v1/task-runs/${encodeURIComponent(runId)}/dag`,
    );
  }

  artifacts(runId: string): Promise<unknown> {
    return this.request(`/api/v1/task-runs/${encodeURIComponent(runId)}/artifacts`);
  }

  patterns(): Promise<LearningPattern[]> {
    return this.request<LearningPattern[]>('/api/v1/learning/patterns');
  }

  learningSummary(): Promise<LearningSummary> {
    return this.request<LearningSummary>('/api/v1/learning/summary');
  }

  /** Any `/ide/page` document, the same JSON the Zed surface renders. */
  page(key: string, opts: { tab?: string; args?: string[]; spec?: string } = {}): Promise<unknown> {
    const q = new URLSearchParams({ key });
    if (opts.tab) {
      q.set('tab', opts.tab);
    }
    if (opts.spec) {
      q.set('spec', opts.spec);
    }
    for (const a of opts.args ?? []) {
      q.append('arg', a);
    }
    return this.request<unknown>(`/ide/page?${q.toString()}`);
  }

  // --- writes ------------------------------------------------------------

  launch(input: {
    recipe: string;
    kickoffMarkdown: string;
    runId?: string;
    extraEnv?: Record<string, string>;
  }): Promise<LaunchResult> {
    return this.request<LaunchResult>('/api/v1/runs', {
      method: 'POST',
      auth: true,
      timeoutMs: 30_000,
      body: {
        recipe: input.recipe,
        kickoff_markdown: input.kickoffMarkdown,
        ...(input.runId ? { run_id: input.runId } : {}),
        ...(input.extraEnv ? { extra_env: input.extraEnv } : {}),
      },
    });
  }

  steer(runId: string): Promise<RunCard> {
    return this.request<RunCard>(
      `/api/v1/task-runs/${encodeURIComponent(runId)}/steer`,
      { method: 'POST', auth: true, body: {} },
    );
  }

  pauseCost(runId: string): Promise<unknown> {
    return this.request(`/api/v1/task-runs/${encodeURIComponent(runId)}/pause-cost`, {
      method: 'POST',
      auth: true,
      body: {},
    });
  }

  resumeCost(runId: string): Promise<unknown> {
    return this.request(`/api/v1/task-runs/${encodeURIComponent(runId)}/resume-cost`, {
      method: 'POST',
      auth: true,
      body: {},
    });
  }

  /** Read-only: recovery is a computed view of what a run would need, not an action. */
  recovery(runId: string): Promise<unknown> {
    return this.request(`/api/v1/runs/${encodeURIComponent(runId)}/recovery`);
  }
}

function extractDetail(text: string): string | undefined {
  if (!text) {
    return undefined;
  }
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === 'string') {
      return parsed.detail;
    }
    if (parsed.detail !== undefined) {
      return JSON.stringify(parsed.detail);
    }
  } catch {
    /* not JSON — fall through to the raw body */
  }
  return text.slice(0, 300);
}
