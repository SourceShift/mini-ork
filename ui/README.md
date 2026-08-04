# mini-ork UI

The mini-ork frontend is the OpenHands agent-canvas SPA, forked for the
mini-ork backend contract. The shell renders the upstream routes against the
runtime on `127.0.0.1:7090` (Vite dev proxy on `:7070`).

## Prerequisites

- Node `>=22.12.0` (see `engines` / `volta` in `package.json`)
- pnpm `11.0.9` (pinned via `packageManager`; `corepack enable` will honor it)

## Local dev

```bash
pnpm install            # installs deps + runs approved build scripts
pnpm run dev:frontend   # Vite dev server on http://localhost:7070, proxying the API to :7090
pnpm build              # production build → ../mini_ork/web/static
```

> Use `pnpm run dev:frontend`, **not** bare `pnpm dev`. `pnpm dev` runs the
> upstream OpenHands automation launcher (`scripts/dev-with-automation.mjs`),
> which is not the mini-ork dev contract.

The simplest way to bring the whole stack up (API + Vite, both hot-reloading)
is from the repo root:

```bash
make web-deps   # one-time: python deps + pnpm install
make web-up     # boots mini-ork serve on :7090 + Vite dev on :7070 (Ctrl-C stops both)
```

`make web-up` is idempotent — it frees `:7090` and `:7070` before booting, so a
wedged server from a prior session won't block a restart.

### Production (served by the backend)

```bash
make web-build  # pnpm build → emits the SPA into mini_ork/web/static/
make web-serve  # mini-ork serve on :7090, serving the built SPA (no Vite)
```

The React Router `buildEnd` hook (`react-router.config.ts`) copies
`build/client/*` into `../mini_ork/web/static/`, which `mini-ork serve` mounts
directly — no separate deploy step.

## Contract

| Surface | Value |
| --- | --- |
| Dev port | `7070` (`VITE_FRONTEND_PORT`) |
| API proxy target | `http://127.0.0.1:7090` (`VITE_BACKEND_HOST`), websocket passthrough on `/api` + `/sockets` |
| Prod outDir | `../mini_ork/web/static` (mounted by `mini-ork serve`) |

Proxy targets are configurable via env (`VITE_BACKEND_HOST`,
`VITE_FRONTEND_PORT`, `VITE_USE_TLS`, `VITE_BASE_PATH`); see `vite.config.ts`.

## Build-script approval (pnpm 11)

pnpm 11 gates dependency build scripts behind an explicit allow-list in
`pnpm-workspace.yaml`:

```yaml
allowBuilds:
  esbuild: true
  msw: true
  unrs-resolver: true
  # …
```

If you add a dependency whose install runs a build script, pnpm will
**hard-fail** the next `pnpm install`/`pnpm build` with
`ERR_PNPM_IGNORED_BUILDS` until you add it here (`true` to run it, `false` to
silence it). This replaced the older `onlyBuiltDependencies` list.

## Notes

- The OpenHands analytics client (PostHog) is stripped — the mini-ork backend
  owns run-time telemetry. The related stub lives in
  `src/services/telemetry.ts` so call sites still compile.
- `pnpm install` prints a harmless `husky … .git can't be found` line: the
  upstream `prepare` script expects a `.git` beside `package.json`, but
  mini-ork's git root is one level up and hooks are managed via `.githooks/`,
  not husky. The step exits successfully.
- The `examples/`, `docs/`, `electron/`, `helm/`, `specs/`, `__tests__/`,
  `tests/` directories from the upstream tree are intentionally omitted.
- Branding passes (HTML title, README) are best-effort; the full white-labelling
  pass is owned by SE-10.
- Rollback: the pre-fork mini-ork UI is preserved at `../ui.old/`.
