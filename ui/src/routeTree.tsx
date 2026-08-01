import { createRootRoute, createRoute } from "@tanstack/react-router";

import { Shell } from "./components/Shell";
import { FleetPage } from "./routes/FleetPage";
import { NewRunPage } from "./routes/NewRunPage";
import { RecipesPage } from "./routes/RecipesPage";
import { RunDetailPage } from "./routes/RunDetailPage";
import { AgentDetailPage } from "./routes/AgentDetailPage";
import { RunInputPage } from "./routes/RunInputPage";
import { TrajectoryPage } from "./routes/TrajectoryPage";
import { SelfImproveDetailPage } from "./routes/SelfImproveDetailPage";
import { FingerprintPage } from "./routes/FingerprintPage";
import { TerminalPage } from "./routes/TerminalPage";

const rootRoute = createRootRoute({ component: Shell });

const fleetRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: FleetPage,
});

const newRunRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/new",
  component: NewRunPage,
});

const recipesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/recipes",
  component: RecipesPage,
});

const runDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$taskRunId",
  validateSearch: (search: Record<string, unknown>): { tab?: string; artifact?: string } => ({
    ...(typeof search.tab === "string" ? { tab: search.tab } : {}),
    ...(typeof search.artifact === "string" ? { artifact: search.artifact } : {}),
  }),
  component: RunDetailPage,
});

const agentDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$taskRunId/agents/$nodeId",
  validateSearch: (search: Record<string, unknown>): { tab?: string } =>
    typeof search.tab === "string" ? { tab: search.tab } : {},
  component: AgentDetailPage,
});

const runInputRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$taskRunId/inputs/$inputKey",
  component: RunInputPage,
});

const trajectoryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/trajectory",
  component: TrajectoryPage,
});

const selfImproveDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/trajectory/self-improve/$runId",
  component: SelfImproveDetailPage,
});

const fingerprintRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/fingerprint",
  component: FingerprintPage,
});

const terminalRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/terminal",
  validateSearch: (search: Record<string, unknown>): { run?: string; cmd?: string } => ({
    ...(typeof search.run === "string" ? { run: search.run } : {}),
    ...(typeof search.cmd === "string" ? { cmd: search.cmd } : {}),
  }),
  component: TerminalPage,
});

export const routeTree = rootRoute.addChildren([
  fleetRoute,
  newRunRoute,
  recipesRoute,
  runDetailRoute,
  agentDetailRoute,
  runInputRoute,
  trajectoryRoute,
  selfImproveDetailRoute,
  fingerprintRoute,
  terminalRoute,
]);
