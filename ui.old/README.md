# mini-ork observability UI (React SPA)

Frontend for `mini_ork/web/` — the FastAPI read-only observability API.

> Two `web`-adjacent dirs by design: `mini_ork/web/` is the Python FastAPI
> subpackage (backend), `ui/` is the React/Vite project (frontend).
> Renamed from `web/` so the dichotomy is unambiguous.

## Dev

```bash
# One-shot (recommended) — boots API on :7090 + Vite on :7070 in parallel
make web-up

# Or step by step:
pnpm install                      # in ui/
mini-ork serve --reload           # backend on :7090
pnpm dev                          # Vite on :7070, proxies /api → :7090
```

Open http://localhost:7070.

## Build & ship

```bash
pnpm build
# emits to ../mini_ork/web/static/ — picked up by `mini-ork serve` automatically.
```

After `pnpm build`, `mini-ork serve` serves the SPA + API from a single
origin on `:7090`. No CORS in prod.

## Routes

| Path | Purpose |
|---|---|
| `/` | Fleet: active runs + recent task_runs |
| `/runs/:taskRunId` | Per-run forensics: DAG, artifacts, events, LLM calls |
| `/trajectory` | Self-improve convergence + cost/wall-time trends |
| `/fingerprint` | Detection-fingerprint receipts per recipe |

## Stack

- Vite + React 18 + TypeScript
- TanStack Router (typesafe links) + TanStack Query (server cache)
- Tailwind (no shadcn yet — added on demand)
- @xyflow/react for the recipe DAG
- Recharts for trajectory
- react-markdown for `synthesis.md` / lens-*.md
- EventSource for SSE-driven live updates
