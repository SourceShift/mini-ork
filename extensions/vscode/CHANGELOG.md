# Changelog

## 0.2.0

The Dispatch Console: the IDE from the design brief, implemented over the
engine's page spec rather than as a second app.

- `MiniOrkPanel` is now a generic renderer for `/ide/page?spec=2` — one
  renderer per section type (table, list, kv, bars, flow, code, markdown,
  pills, chips, dag, inspector, hero, triage, callout, story, files, findings,
  checks, levels, agents, composer, columns, document) plus an explicit
  fallback for types this build does not know.
- Page rail, tab strip (one tab per page, per run), page sub-tabs, chips and
  `⌘K` palette over pages and runs.
- The orchestrator (Dispatch) page: start a run from a sentence, pick a saved
  kickoff, and answer what is blocked — every button the engine declares.
- `ActionDispatcher` + `runCli`: the spec's `do` payloads execute without a
  shell, with `MINI_ORK_HOME` exported, and destructive/costly verbs always
  confirmed with the exact command shown.
- Streamed command output in a log drawer; toasts for success, failure and
  timeout.
- Replaced the old four-tab dashboard webview.

## 0.1.0

Initial release.

- Backend supervisor: discovers the `mini-ork` binary, spawns and health-probes
  `mini-ork serve`, attaches to a running server instead of spawning a second
  one, auto-restarts on unexpected exit, and reaps the child on deactivate.
- Dashboard webview with Board, Runs, Learnings, and Recipes tabs, rendering
  host-proxied snapshots under a strict CSP.
- Tree views: runs grouped by board state, recipe catalogue, learning-loop
  counters and promoted patterns.
- Commands: launch from selection/file/typed kickoff, steer, pause/resume cost,
  trajectory, artifacts, recovery view, attach PTY terminal, copy run id.
- Status bar: backend state, work needing the operator, today's spend.
