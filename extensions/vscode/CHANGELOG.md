# Changelog

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
