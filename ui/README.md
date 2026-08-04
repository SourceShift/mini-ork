# mini-ork UI

The minio-ork frontend is the OpenHands agent-canvas SPA, forked for the
minio-ork backend contract. The shell renders the upstream routes against
the runtime on `127.0.0.1:7090` (Vite dev proxy on `:7070`).

## Local dev

```bash
pnpm install
pnpm dev          # Vite dev server on http://localhost:7070
pnpm build        # production build → ../mini_ork/web/static
```

The upstream agent-canvas upstream is fetched from `git@github.com:OpenHands/OpenHands.git`
and pruned to the SPA tree only. The `pnpm-lock.yaml` is regenerated from
that exact tree; do not hand-edit.

## Contract

| Surface | Value |
| --- | --- |
| Dev port | `7070` |
| API proxy | `http://127.0.0.1:7090` (websocket passthrough for `/api/v1/pty`) |
| Prod outDir | `../mini_ork/web/static` (mounted by `mini-ork serve`) |

## Notes

- The OpenHands analytics client (PostHog) is stripped — the minio-ork
  backend owns run-time telemetry. The related stub lives in
  `src/services/telemetry.ts` so call sites still compile.
- The `examples/`, `docs/`, `electron/`, `helm/`, `specs/`, `__tests__/`,
  `tests/` directories from the upstream tree are intentionally omitted.
- Branding passes (HTML title, README) are best-effort; the full white-labelling
  pass is owned by SE-10.
