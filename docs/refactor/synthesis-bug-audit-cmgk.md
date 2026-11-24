# Bug audit (synthesis) — report only, no fixes

## Summary
- Total unique bugs: 54
- By severity: P0 0 · P1 5 · P2 23 · P3 26
- By consensus: 4/4 1 · 3/4 1 · 2/4 5 · 1/4 47
- Coverage: A1 file-write containment (3 findings; containment itself verified sound) · A2 network guardrails (1 finding; allow/block enforcement verified sound) · A3 data leakage (4 findings; Clearcut payload pipeline verified clean) · A4 resource bounds (7 findings) · A5 process/command handling (7 findings) · contract drift across navigation, console, emulation, memory and comments tools (remaining findings). Zero-bug areas listed in the coverage gap report.

## P0 — Critical bugs
None. No lens found a reachable guardrail bypass, file-write escape, URL-pattern bypass or credential exfiltration that survives the SECURITY.md out-of-scope preconditions.

## P1 — Visible breakage

- [BUG-001] [CONSENSUS: 4/4] `evaluate_script` with an http(s) `sourcePath` rewrites the URL into a bogus local `file:` path and never fetches it
  - [DISPUTED-SEVERITY: P1 (kimi) vs P2 (codex, minimax) vs P3 (glm)]
  - File: `src/tools/script.ts:234-236` (implementation); contract at `src/tools/script.ts:111-115`
  - Feature: `evaluate_script` (`sourcePath`)
  - Symptom: `sourcePath: "https://example.com/app.js"` fails with an access-denied/ENOENT error naming `<cwd>/https:/example.com/app.js`; the documented server-side URL fetch never happens.
  - Root cause: `validateAndResolvePathOrUrl` passes an `http(s)` value through unchanged (`src/ToolHandler.ts:152-154`), then `resolveScriptSource` tests only `sourcePath.startsWith('file:')` and otherwise calls `pathToFileURL(sourcePath).href`, producing a `file:` URL; `McpContext.loadResource` (`src/McpContext.ts:1279-1288`) therefore takes the `file:` branch and reads a local path.
  - Reproduction: call `evaluate_script` with `{sourcePath: "https://example.com/x.js"}` and a valid `pageId`; with default roots it is "Access denied", with unrestricted paths it is ENOENT.
  - Impact: a documented parameter form is dead for every caller; the `https:`/`http:` branch of `loadResource` is unreachable from this tool.
  - Fix shape: branch on the parsed protocol in `resolveScriptSource` and pass `http(s)` URLs to `loadResource` unchanged.

- [BUG-002] [CONSENSUS: 2/4] Every tool call's raw parameters and every tool result are written unredacted to the log file/stderr, and the docs tell users to submit those logs
  - [DISPUTED-SEVERITY: P1 (minimax) vs P2 (glm)]
  - File: `src/ToolHandler.ts:558` (params); `src/daemon/client.ts:203` (full result); `src/daemon/daemon.ts:238` (full request); `src/utils/logger.ts:19` (world-readable `0o644` log file); help text at `src/config/mcp-options.ts:82-83`
  - Feature: logging — all tools, `--logFile`, `NODE_DEBUG=*`, daemon transport
  - Symptom: with `--logFile /tmp/mcp.log`, `emulate` `extraHttpHeaders` (e.g. `Authorization: Bearer …`), authenticated URLs with query tokens, `evaluate_script` function bodies and full page/network results are persisted verbatim; the `--logFile` help text calls these logs "Useful for submitting bug reports".
  - Root cause: `logger?.(\`${tool.name} request: ${JSON.stringify(params, null, '  ')}\`)` applies no redaction although `redactUrlForLogging`/`redactDaemonArgsForLogging` exist in `src/utils/logger.ts:74-148`; the daemon request/response log sites have the same gap.
  - Reproduction: start with `--logFile /tmp/mcp.log`, call `emulate` with `extraHttpHeaders: '{"Authorization":"Bearer sekrit"}'`, then `grep sekrit /tmp/mcp.log`.
  - Impact: credentials, cookies, page text and authenticated URLs are persisted to a world-readable file that the product tells users to publish.
  - Fix shape: route the log calls at `ToolHandler.ts:558` and the daemon/client/performance sites through the existing redaction helpers.

- [BUG-003] [CONSENSUS: 1/4] `chrome-devtools status` prints the daemon's raw argv, exposing a `--ws-headers` bearer token on stdout
  - File: `src/bin/chrome-devtools.ts:175` (sink); fed by `src/daemon/daemon.ts:202` from `process.argv.slice(2)` at `src/daemon/daemon.ts:136`; serialized at `src/daemon/utils.ts:198-200`
  - Feature: `chrome-devtools` CLI — `start` / `status` (daemon mode)
  - Symptom: `status` prints `args=["--ws-headers={\"Authorization\":\"Bearer sk-live-…\"}"]` in cleartext.
  - Root cause: the status payload returns `args: mcpServerArgs` unredacted, and the CLI `console.log`s it; the startup log at `src/daemon/client.ts:141` uses `redactDaemonArgsForLogging`, but the status route bypasses that helper.
  - Reproduction: `chrome-devtools start --ws-headers '{"Authorization":"Bearer sk-live-X"}'`, then `chrome-devtools status | grep sk-live`.
  - Impact: a live credential is printed into the terminal output users paste into issues/CI logs; only rotation recovers.
  - Fix shape: apply `redactDaemonArgsForLogging` to `args` in the daemon status result (`src/daemon/daemon.ts:200-203`).

- [BUG-004] [CONSENSUS: 1/4] `mock_network` buffers the entire upstream body in the MCP server, so a large upstream OOM-kills the process
  - File: `src/NetworkOverrideManager.ts:493` (`fetchWithValidatedRedirects`, no size limit) and `src/NetworkOverrideManager.ts:518` (`new Uint8Array(await response.arrayBuffer())`)
  - Feature: `mock_network` (`upstream`)
  - Symptom: mocking an upstream serving a large asset stalls the call, then the MCP server is killed; every later tool call fails with "Connection closed".
  - Root cause: the full response body is materialized as an `ArrayBuffer` and then handed to CDP as one message; no streaming or cap exists. Quantified: an N-byte upstream costs ≈N bytes for the buffer + ≈1.33N bytes of base64 in the CDP message ⇒ ≈2.3–3.3×N peak RSS; a 500 MB upstream exceeds Node's default heap.
  - Reproduction: `mock_network` with `upstream` pointing at a multi-hundred-MB file, then trigger the mocked request.
  - Impact: process kill, not a tool error; the browser session and agent context are lost.
  - Fix shape: cap/stream the upstream body before `request.respond` in `src/NetworkOverrideManager.ts:479-520`.

- [BUG-005] [CONSENSUS: 1/4] `mock_network` mocks with `resourceType: "preflight"` / `"prefetch"` can never match a request
  - File: `src/NetworkOverrideManager.ts:389-394` (matching); enum contract at `src/tools/network.ts:34,40`; classification rationale at `src/utils/resourceType.ts:9-14`
  - Feature: `mock_network` (`resourceType`)
  - Symptom: a configured preflight/prefetch mock reports `hits: 0` forever while the request is served normally.
  - Root cause: `#findOverride` compares against raw `request.resourceType()`; Chrome reports both kinds as `other`, and `getFilterableResourceType(request)` — which maps them from `OPTIONS`/`Sec-Purpose` headers — is used by `list_network_requests` (`src/McpResponse.ts:821`) but not by mock matching.
  - Reproduction: `mock_network {route:"*://*:*/*", resourceType:"preflight", abort:true}`, reload a page issuing a CORS preflight, inspect diagnostics.
  - Impact: the two halves of the same feature classify requests differently; a documented filter silently never fires.
  - Fix shape: use `getFilterableResourceType(request)` in `#findOverride` (`src/NetworkOverrideManager.ts:389-394`).

## P2 — Degraded behaviour

- [BUG-006] [CONSENSUS: 2/4] Tool failures are returned as successful MCP calls (`isError` unset, telemetry `success: true`)
  - File: `src/tools/pages.ts:291-296` (goto), `src/tools/pages.ts:299-314` (goBack/goForward), `src/tools/pages.ts:96-101` (close_page), `src/tools/performance.ts:65-70,173-178`, `src/tools/screencast.ts:98-103,186-190`; response gating at `src/ToolHandler.ts:585-587`
  - Feature: `navigate_page`, `close_page`, `performance_start/stop_trace`, `screencast_start/stop`
  - Symptom: a failed navigation, a refused last-page close, or a "already running" guard produces `isError: false` with a prose "Error: …" / "Unable to navigate …" line; `goBack`/`goForward` returning `null` is reported as a successful navigation.
  - Root cause: handlers catch and `appendResponseLine` instead of `throw`/`setError`, and `goBack`/`goForward` results are discarded; the response pipeline only sets `isError` from `response.error`.
  - Reproduction: navigate to `http://127.0.0.1:1/`; call `performance_start_trace` twice; call `navigate_page {type:"back"}` on a fresh tab.
  - Impact: clients keying off the protocol error flag believe the page is on the requested URL / that the trace started, and telemetry records success.
  - Fix shape: use `response.setError(...)`/throws in `pages.ts`, `performance.ts` and `screencast.ts` instead of prose-only failure lines.

- [BUG-007] [CONSENSUS: 3/4] Heap-snapshot state is retained without bound: per-path snapshots and per-path task queues live for the whole process
  - [DISPUTED-SEVERITY: P2 (minimax) vs P3 (codex, glm)]
  - File: `src/processors/HeapSnapshotManager.ts:133` (`#snapshots` map), `:143` (`#fileTaskQueues` map), `:154-164` (queue creation), `:577-598` (`disposeSnapshot`/`dispose` never prune the queue map)
  - Feature: `take_heapsnapshot`, `get_heapsnapshot_*`, `close_heapsnapshot`, long-lived daemon
  - Symptom: server RSS and worker count climb monotonically as distinct snapshot paths are read; `close_heapsnapshot` frees the snapshot but not its queue.
  - Root cause: both maps are keyed by absolute path and only ever appended to; eviction exists only through the explicit `close_heapsnapshot` tool (which still leaves `#fileTaskQueues`). A 100 MB `.heapsnapshot` expands to ≈300–600 MB of JS heap; 5 unclosed snapshots ≈1.5–3 GB.
  - Reproduction: in one daemon session, take snapshots to 10 distinct paths, query each, then watch RSS/worker count.
  - Impact: a leak-hunt session can exhaust container memory; a resource-release tool does not release all resources.
  - Fix shape: add a bounded LRU (or delete the queue in `disposeSnapshot`) in `src/processors/HeapSnapshotManager.ts:133-143,577-598`.

- [BUG-008] [CONSENSUS: 2/4] `emulate` rejects the whole call when `networkConditions` is combined with an allow/blocklist, discarding every other emulation setting
  - [DISPUTED-SEVERITY: P2 (kimi) vs P3 (minimax)]
  - File: `src/McpPage.ts:1468-1474`; contract at `src/tools/emulation.ts:57,62-66`
  - Feature: `emulate` (`networkConditions`) under `--allowedUrlPattern` / `--blockedUrlPattern`
  - Symptom: `emulate` with viewport + user agent + `networkConditions` throws "Network throttling is not supported when network blocking … is configured." and applies none of the other settings.
  - Root cause: the throw happens before any other emulation is applied, and the tool description ("pass all desired settings in a single call") does not state that `networkConditions` is rejected under network blocking.
  - Reproduction: start with `--blockedUrlPattern='*://*.example.com/*'`, then call `emulate` with both `networkConditions` and a viewport.
  - Impact: two documented features are mutually unusable, the agent silently loses the other settings, and the error names only throttling.
  - Fix shape: document the restriction in the `networkConditions` description and/or apply the non-network settings before throwing (`src/McpPage.ts:1468-1474`).

- [BUG-009] [CONSENSUS: 1/4] The daemon is spawned with unredacted secret flags, exposing them in the process table
  - File: `src/daemon/client.ts:142` (`spawn(process.execPath, [DAEMON_SCRIPT_PATH, ...mcpArgs], …)`)
  - Feature: `chrome-devtools start` (daemon mode)
  - Symptom: `ps -ef` / `/proc/<pid>/cmdline` shows `--ws-headers={"Authorization":"Bearer sk-live-…"}` for the detached daemon's whole lifetime.
  - Root cause: the redacted copy produced at `src/daemon/client.ts:141` is only logged; spawn uses the original `mcpArgs`. The daemon also reads the flag back from its own argv (`src/daemon/utils.ts:104`), so there is no alternate transport.
  - Reproduction: start the daemon with `--ws-headers`, then inspect the process table.
  - Impact: any local user can read the live credential; only killing the daemon and rotating recovers.
  - Fix shape: pass credentials to the daemon out-of-band (env/file), not in the spawn argv (`src/daemon/client.ts:142`).

- [BUG-010] [CONSENSUS: 1/4] `list_console_messages` / `list_network_requests` format every retained item before paginating and have no default page size
  - File: `src/McpResponse.ts:766-767` (console `Promise.all`), `src/McpResponse.ts:825-826` (network); slice at `src/McpResponse.ts:1785,1812`; `src/utils/pagination.ts:28-39`; no default at `src/tools/console.ts:55` / `src/tools/network.ts:54`
  - Feature: `list_console_messages`, `list_network_requests`
  - Symptom: on a chatty page the tool builds and returns tens of thousands of formatted lines (several MB) in one message even when `pageSize: 20` is passed.
  - Root cause: formatting runs over the full retained set and pagination only slices the finished array; when no pagination option is supplied `paginate` returns every item. Quantified: 10 000 `MAX_MESSAGES_PER_NAVIGATION` × `maxNavigationSaved` 3–25 ⇒ 10 000–250 000 `ConsoleFormatter.from()` promises and 1 000–25 000 `NetworkFormatter` objects per call.
  - Reproduction: log thousands of messages, call `list_console_messages` with and without `pageSize`.
  - Impact: context-window blowout and CPU/memory spikes; `pageSize` does not bound the work.
  - Fix shape: paginate before formatting in `McpResponse` and give `pageSize` a default.

- [BUG-011] [CONSENSUS: 1/4] Heap-snapshot queries materialize the entire result set and only then paginate down to 50 rows
  - File: `src/processors/HeapSnapshotManager.ts:300` and `:314` (`provider.serializeItemsRange(0, Infinity)`); default page size at `src/McpResponse.ts:1521`
  - Feature: `get_heapsnapshot_class_nodes`, `query_heapsnapshot_objects`, `get_heapsnapshot_edges`
  - Symptom: a leak-hunt query on a real page takes many seconds and raises server RSS by hundreds of MB; on a memory-capped container it can be OOM-killed.
  - Root cause: an "infinite" range is serialized to return 50 rows. Quantified: a 300 MB `.heapsnapshot` with ~2 M live objects ⇒ ≈300–600 MB of live JS heap materialized for 50 rows.
  - Reproduction: query `Object`/`Array` on a real application's snapshot.
  - Impact: invisible stall or session loss on large snapshots.
  - Fix shape: pass the requested range to `serializeItemsRange` in `src/processors/HeapSnapshotManager.ts:300,314`.

- [BUG-012] [CONSENSUS: 1/4] `get_console_message` returns the full JSON value of a console argument with no size cap
  - File: `src/formatters/ConsoleFormatter.ts:195` (`await arg.jsonValue()`), rendered/copied at `src/formatters/ConsoleFormatter.ts:428`
  - Feature: `get_console_message`
  - Symptom: one `console.log(hugeArray)` makes the tool return a multi-MB result with no truncation notice, or crashes the server.
  - Root cause: JSON-like values take the uncapped `jsonValue()` branch; only non-JSON-like values go through the bounded `formatRemoteObject` preview (`src/formatters/ConsoleFormatter.ts:191-193`). Quantified: a 20 MB argument ⇒ ≈80 MB transient across CDP string, parsed object, re-stringified value and `structuredContent`.
  - Reproduction: log a large array in the page, then `get_console_message` on it.
  - Impact: context blowout or OOM.
  - Fix shape: cap/truncate the `jsonValue()` result in `src/formatters/ConsoleFormatter.ts:195`.

- [BUG-013] [CONSENSUS: 1/4] `take_snapshot` renders the accessibility tree three times with O(N·depth) string copying
  - File: `src/formatters/SnapshotFormatter.ts:32-33` (text join) and `:44` (`' '.repeat(depth * 2)` per recursion level); parallel tree at `src/McpResponse.ts:1423-1428`
  - Feature: `take_snapshot`
  - Symptom: large pages yield multi-MB results, seconds of latency and memory spikes well above the returned text size.
  - Root cause: per-node indentation is re-materialized at every depth, so byte-copy cost is Σ(subtree size); the same tree is also built as JSON and pushed again as text. Quantified: 50 000 AX nodes at depth 50 ⇒ ≈5 MB output but ≈250 MB intermediate copying, plus a second ≈20–40 MB object tree.
  - Reproduction: `take_snapshot` (or `verbose: true`) on a page with tens of thousands of AX nodes.
  - Impact: context and memory pressure; no node-count parameter exists to bound it.
  - Fix shape: build indentation once (or stream) in `src/formatters/SnapshotFormatter.ts:44`.

- [BUG-014] [CONSENSUS: 1/4] The console/network collector trims with a head splice, making a log-spamming page cost O(M × cap)
  - File: `src/collectors/PageCollector.ts:149-152` (`this.storage[0].splice(0, this.storage[0].length - maxResourcesPerNavigation)`); cap at `src/collectors/PageCollector.ts:268`
  - Feature: console/network collection for all page tools
  - Symptom: a page that logs in a render loop burns server CPU continuously and slows unrelated tool calls on that page.
  - Root cause: every insert past the cap removes from the head of a 10 000-element array. Quantified: M messages ⇒ (M − 10 000) × 10 000 element moves; M = 100 000 ⇒ ≈9 × 10⁸ moves (≈7 GB memmove), M = 1 000 000 ⇒ ≈9.9 × 10⁹ moves (≈79 GB). The network collector repeats it at a 1 000 cap.
  - Reproduction: log more than 10 000 messages in one navigation and observe CPU.
  - Impact: sustained CPU cost on noisy pages; the cap is not configurable per page.
  - Fix shape: use a ring buffer or trim in batches in `src/collectors/PageCollector.ts:149-152`.

- [BUG-015] [CONSENSUS: 1/4] A broken DevTools frontend is reported as "Nothing is currently selected in the DevTools Network panel"
  - File: `src/McpPage.ts:1414-1417` (swallows every exception, returns `{}`), `src/McpContext.ts:734-737` (500 ms race also swallows), `src/tools/network.ts:193-195` (misleading message)
  - Feature: `get_network_request`, `list_network_requests` (DevTools-panel selection)
  - Symptom: calling `get_network_request` without `reqid` on a browser whose DevTools frontend cannot open (Electron, Chrome < 144, broken bundle) tells the agent to click a DevTools row instead of reporting the real failure.
  - Root cause: the real error is discarded at two layers; only a logger line retains it.
  - Reproduction: call `get_network_request` with no `reqid` on an unsupported browser.
  - Impact: the user is sent to investigate a DevTools window that may not exist; the actual cause is hidden.
  - Fix shape: surface the swallowed DevTools error in `src/tools/network.ts:193-195`.

- [BUG-016] [CONSENSUS: 1/4] A failing `roots/list` silently collapses file access to the temp dir and the denial names workspace roots the user never configured
  - File: `src/index.ts:368-370` (failure only logged), `src/index.ts:345-347` (`#combinedRoots()` returns `undefined`), `src/McpContext.ts:531` (temp-dir fallback), `src/McpContext.ts:573-577` (denial text)
  - Feature: MCP roots enforcement; all file-writing tools
  - Symptom: a roots-capable client whose `roots/list` fails leaves the server restricted to the OS temp dir, and saves to `~/Desktop/shot.png` fail with "not within any of the configured workspace roots" although no root was configured.
  - Root cause: the failure path leaves `#roots === undefined`, so `roots()` returns only the temp dir, and the only warning (`src/index.ts:145-151`) fires only when the client does not advertise roots at all.
  - Reproduction: use a client that advertises roots but fails/times out on `roots/list` (server bounds it at 5 s, `src/index.ts:50`), then save a file outside temp.
  - Impact: deterministic 100% failure for that client with an actively misleading message.
  - Fix shape: distinguish "no roots capability" from "roots lookup failed" and report the latter at `src/index.ts:368-370`.

- [BUG-017] [CONSENSUS: 1/4] `waitForStableDom` leaks the in-page MutationObserver and its JSHandle when the timeout wins the race
  - File: `src/utils/WaitForHelper.ts:58` (race), `:59-83` (observer), `:87-89` (timeout return), `:91-100` (abort listener only on success)
  - Feature: `WaitForHelper` post-action stability wait used by every page tool
  - Symptom: no user-visible error, but a long session accumulates orphaned CDP handles and observers that re-arm a `setTimeout` on every DOM mutation until the next navigation.
  - Root cause: when `Promise.race` resolves via the timeout, the later-resolving `evaluateHandle` promise's value is dropped and its handle never disposed; the cleanup listener is only registered on the success path.
  - Reproduction: open an `alert()` (renderer paused), call `evaluate_script`, let the stability wait time out, dismiss the dialog, then mutate the DOM.
  - Impact: one leaked handle + observer per timed-out action, cumulative in the daemon.
  - Fix shape: keep and dispose the pending `evaluateHandle` promise on timeout in `src/utils/WaitForHelper.ts:54-110`.

- [BUG-018] [CONSENSUS: 1/4] `file:` URL inputs are re-encoded as URLs and then re-validated as relative filesystem paths
  - File: `src/ToolHandler.ts:137-141` (returns `pathToFileURL(...).href`); consumers at `src/McpContext.ts:582-592` (`ensureExtension` → `validatePath` → `path.resolve`); contract at `src/tools/ToolDefinition.ts:72-82`
  - Feature: `take_screenshot`, `performance_*`, `screencast_start`, `save_heap_snapshot`, `lighthouse_audit`, `mock_network` (`body`)
  - Symptom: `take_screenshot {filePath: "file:///tmp/shot.png"}` fails "Access denied" under default roots, or under unrestricted paths writes to `<cwd>/file:/tmp/shot.png`.
  - Root cause: the validator hands the handler a `file:` URL where every downstream filesystem consumer expects a plain path; `tests/ToolHandler.test.ts:1809-1820` even codifies the URL as the handler input, but no consumer converts it back.
  - Reproduction: call `take_screenshot` with a `file:` URL and inspect the error/output path.
  - Impact: a documented-accepted input form is unusable or writes to a surprising location.
  - Fix shape: return the canonical plain path from the `file:` branch in `src/ToolHandler.ts:137-141`.

- [BUG-019] [CONSENSUS: 1/4] `upload_file` accepts whitespace-only paths, drops them, and reports a successful upload of nothing
  - File: `src/ToolHandler.ts:204-222` (blank returns `undefined`, array loop skips it); contract at `src/tools/input.ts:594-599`
  - Feature: `upload_file` (`filePaths`)
  - Symptom: `upload_file {filePaths: ["   "]}` returns `File uploaded from .` after accepting no files.
  - Root cause: Zod `.min(1)` counts entries; `validateAndResolvePathOrUrl` returns `undefined` for blank strings and the array loop silently excludes them, so `params.filePaths` becomes `[]`; the handler runs `fileChooser.accept([])`.
  - Reproduction: call `upload_file` with a whitespace-only path.
  - Impact: a schema-level minimum of 1 is silently converted to 0; the agent believes an upload happened.
  - Fix shape: reject blank entries (throw) instead of returning `undefined` in `src/ToolHandler.ts:147-150`.

- [BUG-020] [CONSENSUS: 1/4] The daemon never answers and never closes the connection when a request frame is not valid JSON
  - File: `src/daemon/daemon.ts:237-241` (`JSON.parse` outside the inner try), `src/daemon/daemon.ts:215-221`, `:325-327` (rejection only logged)
  - Feature: Daemon IPC — `chrome-devtools start` socket protocol
  - Symptom: a malformed frame produces no reply; the client blocks for the full timeout and the accepted socket stays open.
  - Root cause: the async `onmessage` callback rejects at `JSON.parse`, so `transport.send(...)` and `socket.end()` are never reached; the rejection is handled only by the global unhandled-rejection logger.
  - Reproduction: send a non-JSON frame to the daemon socket, then send a valid request.
  - Impact: one leaked file descriptor per malformed frame and a 60-second stall for the legitimate client.
  - Fix shape: wrap `JSON.parse` in the `handleRequest` try/catch in `src/daemon/daemon.ts:237-241`.

- [BUG-021] [CONSENSUS: 1/4] The daemon client clears its timeout before parsing the reply, so a malformed reply hangs the caller forever
  - File: `src/daemon/client.ts:195-205` (`clearTimeout(timer)` then `resolve(JSON.parse(message))`); only remaining rejections at `:207-216`
  - Feature: Daemon IPC — `sendCommand`
  - Symptom: a non-JSON daemon reply leaves `sendCommand` unsettled; the CLI hangs until killed.
  - Root cause: the timer is cleared first; `JSON.parse` then throws inside the transport callback, and nothing else settles the promise.
  - Reproduction: make the daemon write a malformed frame and invoke a CLI command.
  - Impact: indefinite CLI hang with no error.
  - Fix shape: parse before clearing the timer, or reject inside a try/catch in `src/daemon/client.ts:201-205`.

- [BUG-022] [CONSENSUS: 1/4] The daemon keeps executing a tool call after the client has already given up
  - File: `src/daemon/client.ts:195-198` (client timer destroys socket); `src/daemon/daemon.ts:176` (`callTool` still awaited), `:240-245` (late write only logged)
  - Feature: Daemon IPC — `invoke_tool` lifecycle
  - Symptom: a call slower than the client timeout (60 s, or `toolTimeout + 60 s`) performs its side effects after the client was told it failed.
  - Root cause: there is no cancellation path from the client timeout to the in-flight `mcpServer.callTool`; the daemon finishes and writes to a destroyed socket.
  - Reproduction: `navigate_page` with a 120 s timeout against a slow host via the daemon; the client times out while navigation later completes.
  - Impact: client and browser state diverge, and a retry applies side effects twice.
  - Fix shape: propagate cancellation or report abandoned calls at `src/daemon/daemon.ts:176,240-245`.

- [BUG-023] [CONSENSUS: 1/4] `BrowserManager.detectDisplay` blocks the event loop on a fork storm that reads every same-uid process's environment
  - File: `src/BrowserManager.ts:84-92`
  - Feature: browser launch — Linux display detection
  - Symptom: with `DISPLAY` unset on a host with many same-uid processes, the server stalls while `execSync` runs `ps … | xargs -I{} cat /proc/{}/environ …`, and adopts the `DISPLAY` of an unrelated session.
  - Root cause: `xargs -I{}` forks one `cat` per same-uid process, synchronously blocking the Node event loop; the `catch` silently swallows failures.
  - Reproduction: on Linux with `DISPLAY` unset, launch the server in a populated user session and issue any MCP call.
  - Impact: no MCP request or socket is serviced during the probe; Chrome may launch against the wrong display.
  - Fix shape: read `/proc/self/environ`-style local sources or run the probe asynchronously and bounded in `src/BrowserManager.ts:84-92`.

- [BUG-024] [CONSENSUS: 1/4] The CrUX enrichment step can destroy an otherwise successful trace recording
  - File: `src/tools/performance.ts:283-301` (enrichment before store, `finally` only clears the flag), `:323-340` (CrUX network call); default-on at `src/config/mcp-options.ts:188-194`
  - Feature: `performance_start_trace` / `performance_stop_trace` / `performance_analyze_insight`
  - Symptom: recording a trace while `chromeuxreport.googleapis.com` is unreachable (or the trace has no `Meta`) fails the tool AND discards the parsed trace; `performance_analyze_insight` then reports no trace exists.
  - Root cause: `await populateCruxData(result)` runs before `context.storeTraceRecording(result)`; when it rejects, the store is skipped. Unless a `filePath` was passed (raw buffer written at `src/tools/performance.ts:259-280`), the recording is lost.
  - Reproduction: record a trace on an offline machine with CrUX enabled (default).
  - Impact: the whole capture must be redone; the error names a network problem rather than the lost artifact.
  - Fix shape: store the trace before enrichment, or catch CrUX failures, in `src/tools/performance.ts:288-292`.

- [BUG-025] [CONSENSUS: 1/4] `close_page` (and the `new_page` rollback) never disposes the page's task queue or `McpPage` state
  - File: `src/McpContext.ts:618-623` (`closePage` deletes and closes only), `:776-782` (`#disposeMcpPage` is the only disposer), `:943-952` (pruner cannot see the removed page)
  - Feature: page lifecycle — `close_page`, `new_page` failure rollback
  - Symptom: one orphaned `TaskQueue` per closed page for the process lifetime; `McpPage.dispose()` (screen recorder, `NetworkOverrideManager`, collectors, logpoints, DevTools session) never runs.
  - Root cause: `closePage` removes the entry from `#mcpPages` without calling `#disposeMcpPage`; since page ids are monotonic, the entry is never reclaimed. A loop of failing `new_page` calls (`src/tools/pages.ts:188`) grows the map without bound.
  - Reproduction: close non-last pages repeatedly, or fail `new_page` navigations in a loop.
  - Impact: unbounded resource leak in a long-lived server; page teardown skipped.
  - Fix shape: call `#disposeMcpPage(page)` in `src/McpContext.ts:621-622`.

- [BUG-026] [CONSENSUS: 1/4] Heap-snapshot cache returns stale data after re-capturing to the same filePath
  - File: `src/processors/HeapSnapshotManager.ts:167-182` (cache keyed by resolved path); capture at `src/tools/memory.ts:80-82`
  - Feature: `take_heapsnapshot` + `get_heapsnapshot_*` / `compare_heapsnapshots`
  - Symptom: re-taking a snapshot over the same path after mutating the page still returns the first capture's numbers, silently.
  - Root cause: `captureHeapSnapshot` overwrites the file via `McpContext.#writeFile`, but nothing invalidates `#snapshots`; only explicit `close_heapsnapshot` or category disable clears it, and the description does not say so.
  - Reproduction: capture → read → mutate → capture to the same path → read again.
  - Impact: the core capture/compare loop yields wrong analysis with no error.
  - Fix shape: invalidate the cache entry for the target path in `take_heapsnapshot` (`src/tools/memory.ts:80-82`).

- [BUG-027] [CONSENSUS: 1/4] `evaluate_script` emits the bare token `undefined` where it promises JSON, and writes it into `.json` files
  - File: `src/tools/script.ts:264-312` (`JSON.stringify(value)`, `result ?? 'undefined'`); contract at `src/tools/script.ts:28`
  - Feature: `evaluate_script`
  - Symptom: `() => undefined` produces a fenced `json` block containing `undefined`, and with `filePath` writes the bytes `undefined` into a `.json` file that fails `JSON.parse`.
  - Root cause: `JSON.stringify(undefined)` is `undefined`; the code falls back to the literal string `'undefined'` for the file and prints it for the response.
  - Reproduction: `evaluate_script {function:"() => undefined"}` with and without `filePath`.
  - Impact: output that is not valid JSON, contrary to the documented contract.
  - Fix shape: map a non-serializable result to `null` or an explicit error in `src/tools/script.ts:299-312`.

- [BUG-028] [CONSENSUS: 1/4] `reveal_in_devtools` documents `uid`/`reqid` as mutually exclusive, but both are accepted and applied (and neither reports success)
  - File: `src/tools/comments.ts:234-267` (handler resolves both), contract at `src/tools/comments.ts:192`, schema at `:199-215`
  - Feature: DevTools comments — `reveal_in_devtools`
  - Symptom: passing both sends a combined target to `cd4aBridge.reveal`; passing neither still reports "Revealed target in DevTools."
  - Root cause: the schema has no `.refine` and the handler treats `uid` and `reqid` independently.
  - Reproduction: call with a valid `uid` and `reqid`, then with neither.
  - Impact: declared exclusivity is not enforced; a no-op reports success.
  - Fix shape: add a Zod refinement requiring exactly one of `uid`/`reqid` in `src/tools/comments.ts:199-215`.

## P3 — Sharp edges / footguns

- [BUG-029] [CONSENSUS: 2/4] `ensureExtension` silently rewrites the file extension the user asked for
  - File: `src/McpContext.ts:582-593`; reached from `src/tools/screenshot.ts:304-315`, `src/McpResponse.ts:580-586`, `src/tools/script.ts:298-306`, `src/tools/performance.ts:272-276`
  - Feature: `take_screenshot`, `take_snapshot`, `evaluate_script`, `get_network_request`, `performance_stop_trace`
  - Symptom: `take_screenshot {filePath:"/tmp/shot.jpeg"}` writes `/tmp/shot.png`; `/tmp/report.pdf` becomes `/tmp/report.png`.
  - Root cause: the existing extension is replaced (`resolved.slice(0, len - ext.length) + extension`) rather than appended or validated, and descriptions do not state this.
  - Reproduction: pass a `filePath` whose extension differs from the tool's output format.
  - Impact: requested file never exists; downstream steps get ENOENT.
  - Fix shape: derive format from the requested extension or reject the mismatch in `src/McpContext.ts:582-593`.

- [BUG-030] [CONSENSUS: 2/4] `install_extension`/`uninstall_extension` timeouts leave a live 30 s timer and an orphaned background operation
  - File: `src/McpContext.ts:1109-1127`
  - Feature: extensions — `install_extension`, `uninstall_extension`
  - Symptom: a slow install returns "Timeout installing extension" at 30 s, then completes in the background; a retry can double-install. The timer also keeps the event loop alive after a fast success.
  - Root cause: `Promise.race` without clearing/unref'ing the loser; `ToolHandler.#raceWithTimeout` (`src/ToolHandler.ts:348-371`) already has the correct pattern.
  - Reproduction: make installation take 35 s and call the tool.
  - Impact: misleading result, possible duplicate install, delayed CLI shutdown.
  - Fix shape: clear/unref the timer in a `finally` around `src/McpContext.ts:1109-1127`.

- [BUG-031] [CONSENSUS: 1/4] Schema-validation failures are neither logged nor reported to telemetry
  - File: `src/ToolHandler.ts:545-551` (early return) vs `:557-558` (log) and `:620-629` (telemetry inside the later try)
  - Feature: tool dispatch — argument validation and usage telemetry
  - Symptom: a call rejected for a wrong-typed argument leaves no server log line and no Clearcut record; the `success` metric cannot fall below 100%.
  - Root cause: the validation failure returns before the `try` that contains logging and the `finally` telemetry call.
  - Reproduction: `navigate_page` with `timeout: "10s"` and inspect logs/Clearcut.
  - Impact: a client spamming malformed args is indistinguishable from one that never called the tool.
  - Fix shape: move the validation log/telemetry before the early return in `src/ToolHandler.ts:545-551`.

- [BUG-032] [CONSENSUS: 1/4] `mock_network` diagnostics are inaccurate: updating a mock resets its counters, and `navigations` is counted on every mock
  - File: `src/NetworkOverrideManager.ts:237,266` (diagnostics re-zeroed on update); `:404-411` (`navigations++` on every override before matching)
  - Feature: `mock_network` diagnostics
  - Symptom: a mock that has served hundreds of requests reports `hits: 0` after an update, and a mock whose pattern never matched the navigated URL still reports `navigations: 1`.
  - Root cause: `#createOverride` builds `diagnostics: {hits:0, errors:0, navigations:0}` unconditionally and spreads it over `existing`; the navigation loop increments every override before `#findOverride` selects one.
  - Reproduction: update an exercised mock; configure two mocks and navigate.
  - Impact: the inspection output misleads agents about mock coverage.
  - Fix shape: preserve `existing.diagnostics` and increment only the matched mock in `src/NetworkOverrideManager.ts:237,266,404-411`.

- [BUG-033] [CONSENSUS: 1/4] `set_logpoints` leaves stale logpoints on the page when it throws mid-way
  - File: `src/LogpointManager.ts:140-177` (apply loop; removal loop and lazy-source-map reset unreachable on throw)
  - Feature: `set_logpoints`
  - Symptom: after a partial failure, the page keeps the previous call's logpoints plus the newly applied ones; the tool throws a generic error that does not list what was applied.
  - Root cause: exceptions from `updateBreakpoint()`/`setBreakpoint` escape before the removal loop, and the partial-result `LogpointError` path is not used.
  - Reproduction: request two logpoints where the second cannot bind, after a previous different set.
  - Impact: page emits log messages for breakpoints the agent believes were removed.
  - Fix shape: wrap apply in `try/finally` that reconciles the old set in `src/LogpointManager.ts:140-177`.

- [BUG-034] [CONSENSUS: 1/4] `new_page` can leave a tracked but unselected tab when page initialisation fails
  - File: `src/McpContext.ts:608-614` (`init()` throws after registration, before select/snapshot); caller cleanup at `src/tools/pages.ts:157-188` never runs
  - Feature: page lifecycle — `new_page`
  - Symptom: the tab exists and is registered but no rollback closes it; retries accumulate tabs.
  - Root cause: `#createMcpPage` registers the page before `await mcpPage.init(...)`; failure happens before the caller's `try`.
  - Reproduction: force `McpPage.init()` to fail (session attach/target gone) during `new_page`.
  - Impact: orphan tabs in the browser until pruned; the agent is not told they are leftovers.
  - Fix shape: roll back registration/closing on `init()` failure in `src/McpContext.ts:608-614`.

- [BUG-035] [CONSENSUS: 1/4] `PageCollector.getById` hardcodes a network-specific "Request not found for selected page" message for console lookups too
  - File: `src/collectors/PageCollector.ts:242-250`; consumers at `src/McpPage.ts:854-858` (console) and `:873-875` (network)
  - Feature: `get_console_message`, `get_network_request`
  - Symptom: a missing or cap-evicted `msgid` returns "Request not found for selected page", pointing at page selection rather than an invalid/evicted id.
  - Root cause: a single generic error for all item kinds; the sibling `ServiceWorkerCollector` produces id-specific messages.
  - Reproduction: navigate, then call `get_console_message` with an evicted id (console cap 10 000).
  - Impact: agents chase `select_page`/`list_pages` on a wrong trail.
  - Fix shape: parameterize the error kind in `src/collectors/PageCollector.ts:242-250`.

- [BUG-036] [CONSENSUS: 1/4] `upload_file`'s fallback click is not dialog-aware and leaves a modal dialog open
  - File: `src/tools/input.ts:637-647`
  - Feature: `upload_file`
  - Symptom: after 3 s the tool reports no file chooser was triggered, while the click actually opened an `alert()`/`confirm()`; later `blockedByDialog` calls fail with "A dialog is open".
  - Root cause: the fallback click bypasses `waitForEventsAfterAction(..., {handleDialog})`, unlike other input tools.
  - Reproduction: target a page whose upload button shows `alert('denied')` before creating an input.
  - Impact: the page is left blocked and the error misattributes the cause.
  - Fix shape: route the fallback click through `waitForEventsAfterAction` in `src/tools/input.ts:637-647`.

- [BUG-037] [CONSENSUS: 1/4] Element screenshot clip offset is computed from two non-atomic protocol reads
  - File: `src/tools/screenshot.ts:34-41` (`Promise.all([boundingBox(), scrollX/Y])`), summed at `:45-46`
  - Feature: `take_screenshot` with `uid`
  - Symptom: intermittent element screenshots shifted by a scroll delta on pages that scroll programmatically.
  - Root cause: the bounding box and scroll offset are separate CDP round-trips; a scroll between them invalidates the sum (window ≈1–5 ms locally).
  - Reproduction: take repeated element screenshots on a page with rAF-driven smooth scrolling.
  - Impact: wrong image reported as success.
  - Fix shape: read scroll offset in the same in-page evaluation as the box in `src/tools/screenshot.ts:34-46`.

- [BUG-038] [CONSENSUS: 1/4] `list_console_messages` promises "all" messages but silently caps each navigation at 10,000
  - File: `src/collectors/PageCollector.ts:145-153` and `:268-280`; contract at `src/tools/console.ts:49`
  - Feature: `list_console_messages`
  - Symptom: message 1 disappears after 10,001 messages in one navigation, with no truncation note and pagination totals counting only retained items.
  - Root cause: the generic collector splices the oldest entries past `MAX_MESSAGES_PER_NAVIGATION` and nothing surfaces it.
  - Reproduction: emit 10,001 console messages without navigating, call the tool with no filters.
  - Impact: incomplete output with no indication.
  - Fix shape: surface a truncation flag or document the limit in `src/tools/console.ts:49`.

- [BUG-039] [CONSENSUS: 1/4] `list_console_messages.types` is described as filtering "resource types", but the enum is console message types
  - File: `src/tools/console.ts:19-43` (enum) and `:75` (description); filter at `src/McpResponse.ts:752-762`
  - Feature: `list_console_messages`
  - Symptom: `tools/list` advertises `xhr`/`document` semantics while the enum only accepts `log`/`warn`/`issue`; `types: ["xhr"]` fails validation.
  - Root cause: the word "resource types" is reused from the network tool.
  - Reproduction: inspect `tools/list`, then pass `types: ["xhr"]`.
  - Impact: callers are misled into an invalid filter.
  - Fix shape: change the description to "console message types" in `src/tools/console.ts:75`.

- [BUG-040] [CONSENSUS: 1/4] `timeout` silently treats every non-positive value as "use the default", although only 0 is documented
  - File: `src/tools/ToolDefinition.ts:753-758` (transform), contract at `:760-762`
  - Feature: shared `timeout` parameter
  - Symptom: `timeout: -5` is accepted and behaves like 0 instead of failing validation.
  - Root cause: the schema has no `.min(0)` and maps `value <= 0` to `undefined`.
  - Reproduction: `navigate_page {type:"reload", timeout:-5}`.
  - Impact: caller errors look like intentional defaults.
  - Fix shape: add `.min(0)` or document non-positive values in `src/tools/ToolDefinition.ts:753-762`.

- [BUG-041] [CONSENSUS: 1/4] `sanitizeClientName` redacts 32-character names even though the declared maximum is 32
  - File: `src/telemetry/transformation.ts:34-43`; constant at `:32`
  - Feature: telemetry client identification — `ClearcutLogger.setClientName`
  - Symptom: `sanitizeClientName('a'.repeat(32))` returns `<redacted>`; only 1–31 chars pass. `tests/telemetry/transformation.test.ts:307,317` codifies the off-by-one.
  - Root cause: the guard uses `< MAX_CLIENT_NAME_LENGTH` instead of `<=`.
  - Reproduction: call `sanitizeClientName` with a 32-char name.
  - Impact: a valid boundary name is redacted.
  - Fix shape: use `<=` in `src/telemetry/transformation.ts:37`.

- [BUG-042] [CONSENSUS: 1/4] `mock_network`'s advertised input schema drops the "body and upstream are mutually exclusive" refinement
  - File: `src/tools/network.ts:312` (registers `.shape`), refinement at `:282-288`, re-parse at `:317`
  - Feature: `mock_network`
  - Symptom: `tools/list` publishes no mutual-exclusion constraint and `validateToolFiles` validates the `body` path before the handler rejects the pair.
  - Root cause: `.shape` discards the `.refine` from the registered schema.
  - Reproduction: inspect the advertised schema, then call with both a valid `body` file and an `upstream` URL.
  - Impact: the advertised contract disagrees with the handler; side-effectful file validation happens before rejection.
  - Fix shape: register the refined `ZodObject` rather than `.shape` in `src/tools/network.ts:312`.

- [BUG-043] [CONSENSUS: 1/4] `SlimMcpResponse` ignores `--experimentalDataFormat` and returns a `TextContent` object as `structuredContent`
  - File: `src/SlimMcpResponse.ts:11-27`; contract at `src/config/mcp-options.ts:134-142`, wiring at `src/ToolHandler.ts:373-378`
  - Feature: `--slim` with `--experimentalDataFormat`
  - Symptom: with `--slim --experimentalDataFormat=toon`, output stays plain `\n`-joined text, and `structuredContent` is `{type:"text", text:…}` rather than the tool's structured result.
  - Root cause: `handle(_context)` drops the `dataFormat` argument and never encodes TOON/GCF.
  - Reproduction: run with both flags and call `evaluate`.
  - Impact: a documented experimental option silently has no effect.
  - Fix shape: honor `dataFormat` in `src/SlimMcpResponse.ts:11-27`.

- [BUG-044] [CONSENSUS: 1/4] `open_devtools` and `reveal_in_devtools` declare `readOnlyHint: true` while opening a window and mutating DevTools state
  - File: `src/tools/comments.ts:23-27,193-196`; annotation contract at `src/tools/ToolDefinition.ts:130-133`
  - Feature: DevTools comments
  - Symptom: clients that use `readOnlyHint` for auto-approval treat these as side-effect free, although one opens a window and installs a binding and the other navigates/selects in DevTools.
  - Root cause: both are annotated read-only; `resolve_devtools_comment` correctly uses `false`.
  - Reproduction: compare `tools/list` annotations with the observable side effects.
  - Impact: side effects can bypass client approval.
  - Fix shape: set `readOnlyHint: false` in `src/tools/comments.ts:23-27,193-196`.

- [BUG-045] [CONSENSUS: 1/4] `close_page` says it closes "the page by its index" but resolves a stable page ID
  - File: `src/tools/pages.ts:80` (description), handler at `:85-88`, lookup at `src/McpContext.ts:754-760`
  - Feature: `close_page`
  - Symptom: after pages open/close, a caller using list position can close the wrong page or get "No page found".
  - Root cause: the description says index; the parameter is a stable ID.
  - Reproduction: close page 1, open a third page, compare list position with `pageId`.
  - Impact: wrong page closed.
  - Fix shape: replace "by its index" with "by its page ID" in `src/tools/pages.ts:80`.

- [BUG-046] [CONSENSUS: 1/4] `get_console_message` says `pageId` is ignored when `serviceWorkerId` is set, but routing still uses it
  - File: `src/ToolHandler.ts:438-450`; handler at `src/tools/console.ts:149-152`; contract at `src/tools/console.ts:141`
  - Feature: `get_console_message`
  - Symptom: an invalid `pageId` fails "No page found" even when a valid `serviceWorkerId` is supplied, and the named page is initialized/focus-emulated.
  - Root cause: `pageId` is a required injected parameter resolved and queued before the handler bypasses it.
  - Reproduction: call with `{msgid:1, serviceWorkerId:"sw-1", pageId:9999}`.
  - Impact: the claim "ignored" is false and the error is misleading.
  - Fix shape: state that the selected page is still used, as `list_network_requests` does, in `src/tools/console.ts:141`.

- [BUG-047] [CONSENSUS: 1/4] `navigate_page` validates `url` even for `back`/`forward`/`reload`, where it is documented as unused
  - File: `src/tools/pages.ts:259-265` (validation before dispatch), contract at `:219`
  - Feature: `navigate_page`
  - Symptom: `{type:"reload", url:"javascript:alert(1)"}` fails with a URL-scheme error instead of reloading.
  - Root cause: `url` is validated whenever present, before the `type` switch.
  - Reproduction: reload with a disallowed `url` value.
  - Impact: a parameter documented as ignored for three types can still fail the call.
  - Fix shape: validate `url` only in the `type === 'url'` branch in `src/tools/pages.ts:259-265`.

- [BUG-048] [CONSENSUS: 1/4] `DaemonResponse` declares `result` and `error` as always present, but the daemon omits them and adds an undeclared `message`
  - File: `src/daemon/daemon.ts:178-181,190-193,217-220`; type at `src/daemon/types.ts:20-25`
  - Feature: daemon IPC / `chrome-devtools` CLI
  - Symptom: `stop` resolves `{success:true, message:'stopping'}`; success replies omit `error`; failures omit `result`. Current CLI consumers guard on `success`, so the mismatch is latent.
  - Root cause: the runtime reply shapes are not the declared interface.
  - Reproduction: send `{method:'stop'}` over the socket and parse the reply.
  - Impact: any new consumer doing `response.result.length` on a `stop` reply crashes.
  - Fix shape: make `DaemonResponse` a discriminated union in `src/daemon/types.ts:20-25`.

- [BUG-049] [CONSENSUS: 1/4] Every saved temporary file leaks a fresh directory under the OS temp dir
  - File: `src/utils/files.ts:25` (`fs.mkdtemp` per call, never removed); callers at `src/tools/screenshot.ts:317`, `src/tools/lighthouse.ts:292`, `src/tools/slim/tools.ts:50`
  - Feature: `take_screenshot` (≥2 MB), `lighthouse_audit`, slim-mode images
  - Symptom: one `chrome-devtools-mcp-XXXXXX/` directory per temp save; 20 audits ≈ 40 directories and 40–200 MB of unreferenced files outside every configured root.
  - Root cause: no cleanup path or MCP operation to clear them.
  - Reproduction: run repeated large screenshot/lighthouse calls and inspect the temp dir.
  - Impact: unbounded disk growth invisible to the user.
  - Fix shape: track and remove temp dirs (or use a single managed root) in `src/utils/files.ts:25`.

- [BUG-050] [CONSENSUS: 1/4] `lighthouse_audit` discards the paths of reports it already wrote when the second format fails
  - File: `src/tools/lighthouse.ts:299-305` (`Promise.allSettled` then `throw res.reason`); report list at `:310`
  - Feature: `lighthouse_audit`
  - Symptom: when one of two report writes fails, the successful sibling's path is never reported and the `Reports: …` line is skipped.
  - Root cause: the rejection is rethrown before building the artifact list.
  - Reproduction: force one report save to fail while the other succeeds.
  - Impact: a written artifact exists but the agent cannot find it.
  - Fix shape: report partial successes before throwing in `src/tools/lighthouse.ts:299-310`.

- [BUG-051] [CONSENSUS: 1/4] A URL pattern with a colon in its path is rejected with a misleading "named group" error
  - File: `src/utils/url.ts:274-278,206-208`; reached from `src/config/puppeteer-options.ts:77,96`
  - Feature: `--allowedUrlPattern` / `--blockedUrlPattern`
  - Symptom: `--allowedUrlPattern "https://example.com/api:v2/*"` fails startup with advice that cannot express a literal colon.
  - Root cause: any unescaped `:` is treated as a named group.
  - Reproduction: start the server with a pattern containing `:` in the path.
  - Impact: fail-closed (no traffic escapes) but a valid pattern is rejected with a confusing message.
  - Fix shape: distinguish a scheme colon from an in-path colon in `src/utils/url.ts:274-278`.

- [BUG-052] [CONSENSUS: 1/4] Additional secret-bearing sinks bypass the existing redaction helpers
  - File: `src/utils/logger.ts:122-147` (redaction covers only `--ws-headers`/`--ws-endpoint`/`--browser-url`); `src/daemon/client.ts:141` (startup log); `src/config/browser-options.ts:46,53,72,80` (malformed `--browser-url`/`--ws-endpoint` echoed raw via `src/config/ConfigParser.ts:153`); CrUX URL logging at `src/tools/performance.ts:331-333,337`
  - Feature: daemon startup logging, CLI config errors, performance tracing diagnostics
  - Symptom: `--proxy-server http://user:secret@host` is written cleartext to the daemon startup log; a malformed `--browser-url` echoes userinfo/query to stderr; traced page URLs with session tokens/OAuth codes are logged.
  - Root cause: the redaction allowlist does not cover these flags, and the CrUX diagnostic line does not use `redactUrlForLogging`.
  - Reproduction: start the daemon with `--proxy-server` credentials; record a trace on a page whose URL carries a token.
  - Impact: credentials and tokens persist in logs/stderr outside the disclosed egress.
  - Fix shape: widen `redactDaemonArgsForLogging` and redact the CrUX URL log site in `src/utils/logger.ts:122-147` / `src/tools/performance.ts:331-337`.

- [BUG-053] [CONSENSUS: 1/4] `list_console_messages` with `includeStackTraces` fires ~10⁵ concurrent CDP waiters and timers
  - File: `src/McpResponse.ts:775`; `src/formatters/ConsoleFormatter.ts:219`; `src/devtools/DevtoolsUtils.ts:598` (one `AbortSignal.timeout(1_000)` per message) and `:708-717` (one `ParsedScriptSource` waiter per script id)
  - Feature: `list_console_messages` (`includeStackTraces`)
  - Symptom: a busy page makes the call hang or spike CPU/memory; each `Debugger.scriptParsed` event costs O(pending) dispatches.
  - Root cause: stack traces are requested for every message in the same all-messages `Promise.all`. Quantified: 10 000 messages × ~5 scripts ⇒ ≈50 000 live listeners + 10 000 live timers.
  - Reproduction: call with `includeStackTraces: true` on a page with 10 000 messages and many scripts.
  - Impact: resource exhaustion and slow responses.
  - Fix shape: bound stack-trace concurrency in `src/McpResponse.ts:775`.

- [BUG-054] [CONSENSUS: 1/4] The Chrome profile directory is created world-traversable (`0o755`)
  - File: `src/BrowserManager.ts:271-278`
  - Feature: browser launch — profile directory
  - Symptom: `~/.cache/chrome-devtools-mcp/chrome-profile` (holding Cookies, Login Data, Local Storage) is created with default `0755` under the usual umask, unlike the daemon runtime dir which uses `0o700`.
  - Root cause: `fs.promises.mkdir(userDataDir, {recursive: true})` passes no `mode`.
  - Reproduction: launch with a fresh profile dir and inspect its permissions.
  - Impact: other local users can traverse the profile directory; a privacy hardening gap.
  - Fix shape: pass `mode: 0o700` in `src/BrowserManager.ts:278`.

## Disputed entries
- BUG-001 — P1 (kimi) vs P2 (codex, minimax) vs P3 (glm). Kimi grades it P1 because a documented input form is wholly non-functional; codex/minimax keep P2 because a workaround (`function` inline) exists; glm calls it P3. Synthesis keeps the highest supported severity, P1.
- BUG-002 — P1 (minimax) vs P2 (glm). Minimax grades P1 because credentials are persisted to a world-readable file the product tells users to publish; glm grades P2. Synthesis keeps P1.
- BUG-007 — P2 (minimax) vs P3 (codex, glm). Minimax grades P2 for session-killing RSS growth; codex/glm grade P3. Synthesis keeps P2.
- BUG-008 — P2 (kimi) vs P3 (minimax). Kimi grades P2 for a documented parameter becoming unusable; minimax P3. Synthesis keeps P2.

## Coverage table

| Area | Files inspected (representative) | Outcome |
|---|---|---|
| **A1** file-write containment | `src/utils/files.ts:50-96`, `src/McpContext.ts:475-593,1047-1096`, `src/ToolHandler.ts:123-226`, `src/tools/screenshot.ts`, `src/tools/script.ts`, `src/tools/snapshot.ts`, `src/tools/screencast.ts`, `src/tools/lighthouse.ts`, `src/tools/network.ts:139-167`, `src/tools/input.ts:596-654` | 3 findings (BUG-018, BUG-019, BUG-029). Containment itself sound: `resolveCanonicalPath` realpath + symlink rejection, `O_TRUNC|O_NOFOLLOW` + `0o600` in `#writeFile` (`src/McpContext.ts:1047-1074`), separator-terminated roots prefix check (`src/McpContext.ts:555-558`). |
| **A2** network guardrails | `src/utils/url.ts`, `src/utils/network.ts:55-112`, `src/config/puppeteer-options.ts`, `src/McpContext.ts:260-273,1274-1293`, `src/NetworkOverrideManager.ts:201-246,479-520`, `src/BrowserManager.ts:248-300,387-420`, `src/McpPage.ts:1461-1493` | 1 finding (BUG-008). Allow/block enforcement consistent: `fetchWithValidatedRedirects` validates every redirect hop, cross-origin cookies stripped; browser-level enforcement delegated to Puppeteer `allowlist`/`blocklist`; `view-source:` unwrapped before scheme checks. |
| **A3** data leakage | `src/utils/logger.ts`, `src/utils/errorHandling.ts`, `src/ToolHandler.ts:548-630`, `src/McpResponse.ts:954-1010`, `src/telemetry/*`, `src/daemon/daemon.ts:125-245`, `src/daemon/client.ts:120-220`, `src/daemon/utils.ts`, `src/bin/chrome-devtools.ts` | 4 findings (BUG-002, BUG-003, BUG-009, BUG-052). Clearcut payload pipeline clean: string params bucketized to length, arrays to count, page URL reduced to `is_localhost`, flags to `*_present` booleans, server errors to tool name + code. |
| **A4** resource bounds | `src/collectors/PageCollector.ts`, `src/processors/HeapSnapshotManager.ts`, `src/processors/ChunkedTraceParser.ts`, `src/processors/PerformanceTrace.ts`, `src/formatters/*`, `src/McpResponse.ts:745-840,1420-1440,1470-1620`, `src/utils/pagination.ts`, `src/TextSnapshot.ts` | 7 findings (BUG-007, BUG-010, BUG-011, BUG-012, BUG-013, BUG-014, BUG-053). Trace chunk parsing verified bounded; collector caps exist; formatter output caps exist except the cited branches. |
| **A5** process / command handling | `src/BrowserManager.ts`, `src/utils/check-for-updates.ts`, `src/daemon/{daemon,client,utils,types}.ts`, `src/config/{browser-options,ServerConfig,ConfigParser,ConfigLocator}.ts`, `src/bin/*`, `src/utils/{files,url}.ts` | 7 findings (BUG-003, BUG-009, BUG-020, BUG-021, BUG-022, BUG-023, BUG-049). No command-injection defect: Chrome launched via Puppeteer `launch()` argv array (no shell); daemon uses a named pipe with ownership/permission checks and `fs.rmSync(...,{force:true})`; `scripts/**` has no user-controlled `spawn`/`exec*`. |

## Coverage gap report
- Features with zero bugs found (could be well-tested or under-covered — flag for follow-up):
  - Telemetry Clearcut payload construction (`src/telemetry/transformation.ts`, `flagUtils.ts`, `persistence.ts`) — explicitly verified clean except BUG-041's boundary redaction.
  - Trace chunk parsing and model construction (`src/processors/ChunkedTraceParser.ts`, `PerformanceTrace.ts`) — bounded batches, ASCII structural scan, fresh model per parse.
  - PWA tools (`src/tools/pwa.ts`) — URL-validation ordering verified.
  - CSS and logpoint schema/selection surfaces (`src/tools/css.ts`, `src/tools/logpoints.ts`) — schemas verified; only the failure-path state bug BUG-033.
  - Service-worker collection (`src/collectors/ServiceWorkerCollector.ts`) — subscribe/unsubscribe symmetry and id-specific errors verified.
  - `TaskRunner` dispose semantics, `ConfigWatcher` serialized `onChange`, `NetworkFormatter` redirect-chain copy semantics — verified clean.
  - `scripts/**` user-time code — no findings.
- Areas where the panel read only part of a large module (not a clean verdict): `src/McpPage.ts` (dialog/console/network accessors, third-party tool execution focused), `src/McpResponse.ts` (snapshot/network/console/heap assembly focused), `src/McpContext.ts` (lines 1–500, 580–810, 1040–1330). Remaining ranges should be treated as not audited rather than clean.

## Rejected / false positives
- `--allow-unrestricted-paths` ignored when the client negotiates MCP roots (codex P2): rejected as documented behavior. The option help text (`src/config/mcp-options.ts:291-294`) and the `validatePath` comment (`src/McpContext.ts:517-523`) state the flag applies only when the client does not negotiate roots; client roots stay authoritative by design.
- `sanitizeParams` throwing inside the telemetry `finally` (codex P3): rejected as latent/unreachable. No shipped tool parameter uses an unsupported Zod type; the object/record/unknown uses are CLI-argument transforms, not tool params.
- Same-document navigation flag suppressing one history rotation (codex P3): rejected as speculative. It depends on Puppeteer emitting `FrameNavigatedWithinDocument` without a following `FrameNavigated`, which is not verifiable from this repository (Puppeteer is out of scope).
- `loadResource` appearing to check only the blocklist (raised then self-rejected): the allowlist is enforced one call deeper by `validateNetworkUrl` inside `fetchWithValidatedRedirects` (`src/utils/network.ts:60-67`). Not a defect.
- Cookies/`Authorization` headers in `get_network_request` output: documented and opt-out via `--redactNetworkHeaders` (`src/tools/network.ts:127`, `src/config/mcp-options.ts:279-285`). Not a defect.
- Caller-provided `evaluate_script`/`evaluate` execution and extension loading: documented features, out of scope per SECURITY.md.
- Allow/blocklist not enforced by `validateUrl` on navigation: intentional delegation to Puppeteer's browser-level connect option (`src/BrowserManager.ts:405-406`, `src/McpContext.ts:268`); `validateUrl` is the scheme guard, not the host guard. Not a bypass.
- `emulate` leaving a stale `networkConditions` after a config reload: unreachable; `syncNetworkRestrictions()` → `clearNetworkConditions()` clears it (`src/McpPage.ts:1601-1608`).
- `screencast` video written without `0o600`: path is still validated; permission difference is a hardening nit with no reachable escape.
- `upload_file` paths not validated for a remote browser: documented in the parameter description and code comment (`src/tools/input.ts:604-605`).
- Duplicate/no-finding outcomes from partial reads (missing unit tests, refactor opportunities, an `as unknown as` cast at `src/McpResponse.ts:1558`): out of scope for this bug audit.
