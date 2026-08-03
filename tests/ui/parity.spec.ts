import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";
import path from "node:path";

const snapshotDir = path.resolve(__dirname, "snapshots");

type TaskRun = { id: string };
type Agent = { node_id: string };
type AgentResponse = { agents: Agent[] };
type RunInput = { key: string };
type SelfImproveRun = { run_id: string };

async function visit(page: Page, route: string) {
  await page.goto(route, { waitUntil: "networkidle" });
  await expect(page.getByRole("main")).toBeVisible();
}

async function snapshot(page: Page, name: string) {
  await page.screenshot({ path: path.join(snapshotDir, `${name}.png`), fullPage: true });
}

async function readArray<T>(request: APIRequestContext, endpoint: string): Promise<T[]> {
  const response = await request.get(endpoint);
  return response.ok() ? ((await response.json()) as T[]) : [];
}

async function readObject<T>(request: APIRequestContext, endpoint: string): Promise<T | undefined> {
  const response = await request.get(endpoint);
  return response.ok() ? ((await response.json()) as T) : undefined;
}

function markSkippedWhenEmpty(testInfo: TestInfo, route: string) {
  testInfo.annotations.push({
    type: "skipped-when-empty",
    description: `${route} needs persisted run data; captured its source route instead`,
  });
}

async function firstRun(request: APIRequestContext): Promise<TaskRun | undefined> {
  return (await readArray<TaskRun>(request, "/api/v1/task-runs?limit=1"))[0];
}

test("fleet route", async ({ page }) => {
  await visit(page, "/");
  await expect(page.getByRole("heading", { name: "Fleet", level: 1 })).toBeVisible();
  await expect(page.getByRole("navigation").getByRole("link", { name: /Fleet/ })).toBeVisible();
  await snapshot(page, "fleet");
});

test("new run route", async ({ page }) => {
  await visit(page, "/new");
  await expect(page.getByRole("heading", { name: "New Run", level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: /Catalog/ })).toBeVisible();
  await snapshot(page, "new-run");
});

test("recipes route", async ({ page }) => {
  await visit(page, "/recipes");
  await expect(page.getByRole("heading", { name: "Capabilities", level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: /New Run/ })).toBeVisible();
  await snapshot(page, "recipes");
});

test("run detail route", async ({ page, request }, testInfo) => {
  const run = await firstRun(request);
  if (!run) {
    markSkippedWhenEmpty(testInfo, "/runs/$taskRunId");
    await visit(page, "/");
    await expect(page.getByRole("heading", { name: "Fleet", level: 1 })).toBeVisible();
  } else {
    await visit(page, `/runs/${encodeURIComponent(run.id)}`);
    await expect(page.getByRole("link", { name: /fleet/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /agents/i })).toBeVisible();
  }
  await snapshot(page, "run-detail");
});

test("agent detail route", async ({ page, request }, testInfo) => {
  const run = await firstRun(request);
  const agentResponse = run
    ? await readObject<AgentResponse>(request, `/api/v1/task-runs/${encodeURIComponent(run.id)}/agents`)
    : undefined;
  const agent = agentResponse?.agents[0];
  if (!run || !agent) {
    markSkippedWhenEmpty(testInfo, "/runs/$taskRunId/agents/$nodeId");
    await visit(page, "/");
    await expect(page.getByRole("heading", { name: "Fleet", level: 1 })).toBeVisible();
  } else {
    await visit(page, `/runs/${encodeURIComponent(run.id)}/agents/${encodeURIComponent(agent.node_id)}`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("link", { name: new RegExp(run.id) })).toBeVisible();
  }
  await snapshot(page, "agent-detail");
});

test("run input route", async ({ page, request }, testInfo) => {
  const run = await firstRun(request);
  const inputs = run
    ? await readArray<RunInput>(request, `/api/v1/task-runs/${encodeURIComponent(run.id)}/inputs`)
    : [];
  const input = inputs[0];
  if (!run || !input) {
    markSkippedWhenEmpty(testInfo, "/runs/$taskRunId/inputs/$inputKey");
    await visit(page, "/");
    await expect(page.getByRole("heading", { name: "Fleet", level: 1 })).toBeVisible();
  } else {
    await visit(page, `/runs/${encodeURIComponent(run.id)}/inputs/${encodeURIComponent(input.key)}`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("link", { name: new RegExp(run.id) })).toBeVisible();
  }
  await snapshot(page, "run-input");
});

test("trajectory route", async ({ page }) => {
  await visit(page, "/trajectory");
  await expect(page.getByRole("heading", { name: "Trajectory", level: 1 })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Select hypothesis tree root" })).toBeVisible();
  await snapshot(page, "trajectory");
});

test("self-improve detail route", async ({ page, request }, testInfo) => {
  const run = (await readArray<SelfImproveRun>(request, "/api/v1/trajectory/self-improve?limit=1"))[0];
  if (!run) {
    markSkippedWhenEmpty(testInfo, "/trajectory/self-improve/$runId");
    await visit(page, "/trajectory");
    await expect(page.getByRole("heading", { name: "Trajectory", level: 1 })).toBeVisible();
  } else {
    await visit(page, `/trajectory/self-improve/${encodeURIComponent(run.run_id)}`);
    await expect(page.getByRole("heading", { name: /^Iter /, level: 1 })).toBeVisible();
    await expect(page.getByRole("link", { name: /trajectory/i })).toBeVisible();
  }
  await snapshot(page, "self-improve-detail");
});

test("fingerprint route", async ({ page }) => {
  await visit(page, "/fingerprint");
  await expect(page.getByRole("navigation").getByRole("link", { name: /Fingerprint/ })).toBeVisible();
  await snapshot(page, "fingerprint");
});

test("terminal route", async ({ page }) => {
  await visit(page, "/terminal");
  await expect(page.getByRole("heading", { name: "Live shell", level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "attach" })).toBeVisible();
  await snapshot(page, "terminal");
});
