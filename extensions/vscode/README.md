# mini-ork for VS Code

Launch, steer, and observe [mini-ork](../../README.md) runs without leaving the
editor.

The extension is a **thin client**: it does not reimplement any orchestration.
It finds a `mini-ork` binary, supervises `mini-ork serve` as a background
process, and drives that server's 111 REST operations and its event streams
from VS Code surfaces.

## Architecture

```mermaid
flowchart LR
    subgraph editor["VS Code"]
        WV["Dashboard webview<br/>(no network access)"]
        TV["Tree views<br/>Runs / Recipes / Learnings"]
        SB["Status bar"]
        TERM["Pseudoterminal"]
        HOST["Extension host<br/>(Node)"]
    end
    subgraph backend["mini-ork serve"]
        API["REST /api/v1/*"]
        IDE["/ide/* UI shapes"]
        SSE["SSE /api/v1/stream"]
        PTY["WS /api/v1/pty"]
    end

    WV -- postMessage --> HOST
    TV --> HOST
    SB --> HOST
    TERM -- onDidWrite / handleInput --> HOST
    HOST -- "fetch (no CORS)" --> API
    HOST --> IDE
    HOST --> SSE
    HOST -- "ws" --> PTY
```

The webview is deliberately isolated: its `vscode-webview://` origin cannot be
named in the server's CORS allow-list, so **the webview never makes HTTP calls**.
Every request is proxied by the extension host and the render comes back over
`postMessage`. The bearer token therefore never enters the rendered page.

## Quick start (simple tier)

No server to start, no config to write:

1. Install the extension.
2. Open a folder that has a `mini-ork` checkout (or put `mini-ork` on `PATH`).
3. `mini-ork: Initialize Project` if the folder has no `.mini-ork` yet.
4. The status bar shows `mini-ork` and the backend starts on a free port.

Everything runs locally on `127.0.0.1`. The extension allocates a port, spawns
`mini-ork serve`, probes `/api/v1/health` until it answers, and kills the child
on deactivate. If a healthy server already listens on the configured port, the
extension **attaches** instead of spawning a second one, and never kills a
process it did not start.

## Secure / shared tier

For a shared or remote deployment, point the extension at a server that is not
on your laptop:

- Bind the server deliberately: `mini-ork serve --host 0.0.0.0 --port 7090`
  behind TLS (a reverse proxy), never plain HTTP over a network.
- Create operator tokens and put them in `<home>/auth-tokens.txt`
  (`<token>  <operator-label>`), or set `miniOrk.authToken`.
- Set `miniOrk.serverHost` / `miniOrk.binaryPath` to empty and provide
  `miniOrk.home` pointing at the project you want to observe.

Write endpoints are **fail-closed**: with no token, launches, steering, and
cost controls return 401 and the extension says so. It never sends an empty
bearer.

## What it exposes

| Surface | Commands |
|---|---|
| Lifecycle | start / stop / restart backend, show log, init project, pick home |
| Runs | grouped tree (Needs You / Working / Failed / Done), launch from selection, file, or typed kickoff |
| Steering | steer, pause cost, resume cost |
| Inspection | trajectory (page + DAG), artifacts, recovery view |
| Terminal | attach to a run's PTY as a real terminal tab |
| Dashboard | board KPIs, lanes, runs table, learning counters, recipe catalogue |

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `miniOrk.binaryPath` | `""` | Explicit `mini-ork` binary; empty means discover |
| `miniOrk.home` | `""` | `.mini-ork` home; empty means `${workspaceFolder}/.mini-ork` |
| `miniOrk.autoStart` | `true` | Spawn and supervise the backend on activation |
| `miniOrk.attachToExisting` | `true` | Attach to a healthy server instead of spawning |
| `miniOrk.serverPort` | `0` | `0` auto-selects a free port |
| `miniOrk.serverHost` | `127.0.0.1` | Bind address |
| `miniOrk.startupTimeoutMs` | `30000` | Health-probe budget before declaring failure |
| `miniOrk.refreshIntervalMs` | `5000` | Poll interval; `0` disables polling |
| `miniOrk.authToken` | `""` | Bearer token; empty reads `auth-tokens.txt` |

## Development

```bash
npm install
npm run typecheck     # tsc --noEmit
npm test              # sse + discovery suites
npm run build         # dist/extension.js
npm run package       # produces mini-ork-<version>.vsix
```

Press `F5` in this directory to launch an Extension Development Host.

## Known limits

- **Read-heavy, not real-time.** Tree views poll; only the dashboard's run
  events use SSE. A push-driven tree is a later change.
- **PTY needs the server to allow it.** `/api/v1/pty` answers
  `[pty disabled]` unless the server runs with `MO_PTY_ENABLED=1`.
- **No marketplace publish yet.** The VSIX is produced and installable; the
  listing does not exist.
