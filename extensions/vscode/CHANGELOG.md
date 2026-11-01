# Changelog

## 0.2.1

Second pass over the console, from the pre-push review and the rest of the
page contract.

- **The shell poll no longer rebuilds the page.** It carries a `pageRev` stamp
  and only re-renders the body when that stamp moves, so a sentence half-typed
  into a composer survives the five-second poll. Composer drafts are also
  keyed by page + section, and the caret returns to the same field when a page
  that asked to refresh does re-render.
- **`page.refresh` is honoured.** A page that asks to be re-fetched (a run in
  flight, a lane burning budget) now gets a timer in the host; the timer
  belongs to the page, so switching tabs cancels it.
- **Stale responses are dropped.** Rapid page switches increment a fetch
  sequence, so the slower, older page can no longer land on top of the newer.
- **Contract v2.1 header fields render:** `desc` under the subtitle, `crumbs`
  above the title, `updated_at` beside the actions, and `menu` behind a `▾`.
- **Tab badges read `count` + `tone`** (the engine's shape) instead of `n`.
- **Destructive-action guard is wider:** `kill`, `merge`, `cancel`, `revert`,
  `unset-env`, and `race-*` sub-commands now force a confirm sheet, and the
  verb is found where it actually sits (`board kill`, `pm remove`). The rule
  moved to a `vscode`-free `actions/verbs.ts` so it has unit tests.
- **CSS:** the shell's `.body { display: flex }` is scoped to `#root > .body`;
  it was leaking into every `.card .body` and laying card contents in a row.

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
