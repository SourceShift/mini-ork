# Framework Edit: wholesale OpenHands UI fork

## Goal

Replace mini-ork's bespoke `ui/` with the OpenHands `agent-canvas` frontend as the SE-3 foundation. Preserve the old tree as `ui.old/`, remove OpenHands-only analytics/branding/client dependency, and make the cloned SPA boot against mini-ork's existing local web ports.

## Files in scope

- `ui/` — replace wholesale with the OpenHands `agent-canvas` tree
- `ui.old/` — rename target containing the current mini-ork UI
- `ui/package.json`
- `ui/vite.config.ts`
- `ui/tailwind.config.ts`
- `ui/tsconfig.json`
- `ui/pnpm-workspace.yaml`
- `Makefile`
- `.gitignore`

Do not modify `mini_ork/`, `tests/`, or any backend route. Do not modify parity tests or delete `ui.old/` after the rename.

## Required implementation

1. Move the current `ui/` directory to `ui.old/` so rollback remains possible.
2. Clone `git@github.com:OpenHands/OpenHands.git` and copy only its `frontend/agent-canvas` application tree into the new `ui/`. Do not retain the OpenHands repository's `.git` metadata or unrelated applications.
3. Port the cloned app to the existing mini-ork dev contract: Vite UI on port `7070`, API proxy to `http://127.0.0.1:7090`, websocket proxy enabled, and production output at `../mini_ork/web/static`.
4. Adapt package/workspace/type/Tailwind configuration to the cloned tree and the repository's installed pnpm workflow. Remove `@openhands/typescript-client` if the app does not require it after the port; do not add a replacement backend implementation in this epic.
5. Remove PostHog imports, initialization, environment variables, and references from `ui/src` and its package/config files. Remove OpenHands product branding/copy from the shell where it is straightforward, but do not implement mini-ork's final branding system; that belongs to SE-10.
6. Preserve the existing `web-snapshot` Makefile target and avoid reordering unrelated targets. Update ignore rules only for generated clone/build artifacts required by the new tree.

## Proof command

`cd ui && pnpm install && pnpm build && ! grep -RniE "posthog|VITE_POSTHOG" src package.json vite.config.ts && ! grep -Rni "openhands" src | grep -v "@openhands"`

- `cd ui && pnpm install`
- `cd ui && pnpm build`
- `grep -ri "posthog\|VITE_POSTHOG" ui/src ui/package.json ui/vite.config.ts` returns no matches
- `grep -ri "openhands" ui/src | grep -v "@openhands"` returns no product-branding matches where the shell was ported
- `make web-up` boots the API and Vite dev server; the UI responds on port 7070

If the upstream clone cannot be fetched, stop with a clear failure rather than substituting a guessed local implementation. Keep the patch limited to this kickoff's files.

## Done when

The old UI is recoverable at `ui.old/`, the OpenHands agent-canvas tree is the new `ui/`, package/config/build contracts are adapted, PostHog is absent, the existing parity baseline remains untouched, and the cloned app builds successfully.
