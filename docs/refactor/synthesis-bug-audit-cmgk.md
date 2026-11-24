# Bug audit (synthesis) — report only, no fixes

## Summary
- Total unique bugs: 49
- By severity: P0 1 · P1 3 · P2 18 · P3 27
- By consensus: 4/4 0 · 3/4 1 · 2/4 7 · 1/4 41
- Coverage: findings in GitHub Action, install.sh, npm distribution, VS Code extension, IntelliJ plugin, shared webview, config CLI, JSON output contract, `internal/tool`, `internal/agent`, the CI examples, and the marketing-page i18n table. Zero bugs found (and zero coverage) in the Go core: `internal/llmloop`, `internal/pathutil`, `internal/scan`, `internal/viewer`, `internal/session`, `internal/diff`, `internal/mcp`, `internal/gitcmd`, `internal/chatgptauth`, `internal/telemetry`, and `acp/`.
- Coverage note: the four lanes were themed (flow, security, contract, user-impact), not the B1–B5 decomposition. B1/B2/B4/B5 were exercised mainly through `action.yml`, the two IDE extensions, and the example posters; the Go core was only partially read. This is a real gap, recorded at the end.

## P0 — Critical bugs

- [BUG-001] [CONSENSUS: 1/4] Composite action can `require()` a PR-controlled helper from the workspace in container jobs
  - File: `action.yml:414-421` (checkout with no `ref:`), `action.yml:1086-1089` (helper lookup), `action.yml:819-822` (range-step twin)
  - Feature: GitHub Action — review-range resolution and comment posting
  - Precondition: caller uses `on: pull_request` (not `pull_request_target`) and runs the job in a `container:`; a same-repository branch PR (fork PRs get no secrets but still execute code on the runner). Not the documented example config, but a supported caller shape.
  - Symptom: A PR that adds `scripts/github-actions/post-review-comments.js` at the repo root has that file executed by the action's own steps, with the `github` client (token) and `core` already in scope (`action.yml:1090-1094`).
  - Root cause: The checkout step passes no `ref:`, so on `pull_request` actions/checkout defaults to `refs/pull/N/merge` and materialises PR content — contradicting the step's own "Checkout the trusted base" comment. In a container job the host-side `GITHUB_ACTION_PATH` is invisibly mounted, so `roots.map(...).find(fs.existsSync)` (`action.yml:1086-1089`) skips it and selects the workspace copy. The action's own comment at `action.yml:1076-1082` documents exactly that container behaviour.
  - Reproduction: Push a branch adding `scripts/github-actions/post-review-comments.js` exporting a `runPostReviewComments` that exfiltrates `o.github`/`o.core`; call the action with `on: pull_request` in a `container:` job; the post step requires the workspace copy.
  - Impact: Arbitrary JS execution in CI with the step's GitHub token and runner environment; defeats the invariant the checkout comment and `examples/github_actions/ocr-review.yml:57-59` claim ("does not execute any code from the PR").
  - Fix shape: Pin the checkout to the trusted base at `action.yml:414-421`, and stop falling back to `GITHUB_WORKSPACE` for the helper at `action.yml:1086-1089`/`819-822`.
  - Contest: Codex's lens explicitly rejected this candidate (see Disputed entries); retained at P0 because the rejection does not cover the container-invisible `GITHUB_ACTION_PATH` case.

## P1 — Visible breakage

- [BUG-002] [CONSENSUS: 2/4] LLM-authored comment bodies render as trusted Markdown — `command:` links execute on one click
  - File: `extensions/vscode/src/extension/providers/CommentProvider.ts:130-137`
  - Feature: VS Code extension — comment-thread rendering
  - Precondition: reviewed diff content prompt-injects the model into emitting `[x](command:…)` in a finding body; the user clicks the rendered link.
  - Symptom: `renderBody` interpolates `c.content` into a `MarkdownString` and sets `s.isTrusted = true`, which opts into `command:` URI execution.
  - Root cause: Trusted Markdown over untrusted (model/repo-derived) content; the shared webview twin is safe because `CommentCard.tsx:32` renders plain text.
  - Reproduction: A finding body containing `[x](command:workbench.action.terminal.sendSequence…)`; clicking it runs a registered command with extension-host authority.
  - Impact: Arbitrary VS Code command execution (terminal writes, tasks, file operations) one click from untrusted review output.
  - Fix shape: Drop `isTrusted` (or allow-list command ids) at `CommentProvider.ts:137`.
  - Severity dispute: P1 (codex) vs P2 (glm) — see Disputed entries.

- [BUG-003] [CONSENSUS: 2/4] Finding paths are used as raw filesystem paths — Apply writes outside the workspace root
  - File: `extensions/vscode/src/extension/services/GitService.ts:441`, `extensions/vscode/src/extension/providers/commentAnchor.ts:140,144`, `extensions/vscode/src/extension/providers/CommentProvider.ts:150-171`
  - Feature: VS Code extension — comment anchoring and Apply
  - Precondition: a finding whose `path` contains `..` (model output under PR-controlled prompt injection); the user clicks Apply.
  - Symptom: `readFile(`${root}/${relPath}`)` and `vscode.Uri.file(`${root}/${comment.path}`)` feed open/edit/save; the OS resolves `..`, so the target is outside the repository.
  - Root cause: No containment check on `comment.path` anywhere downstream. The IDEA twin does it correctly (`CommentService.kt:734-738` requires `target.startsWith(base)`), proving contract drift rather than design.
  - Reproduction: A comment with `"path": "../../.zshenv"` mounts on that file; Apply replaces or deletes the reported line range and saves.
  - Impact: Arbitrary file read (line-structure leak via mounting) and arbitrary file modification/deletion with the user's permissions.
  - Fix shape: Realpath-containment check (resolve, verify `repoRoot` prefix) before the URI construction at `GitService.ts:441` / `CommentProvider.ts:150`.

- [BUG-004] [CONSENSUS: 1/4] On Windows, npm-installed CLI cannot be spawned by either IDE — health check still reports "ocr ✓"
  - File: `extensions/vscode/src/extension/services/CliService.ts:121-127`; `extensions/idea/src/main/kotlin/com/alibaba/opencodereview/idea/services/CliService.kt:175`
  - Feature: both IDE extensions — every CLI invocation (review, config save, connection test)
  - Precondition: CLI installed through npm on Windows (the documented primary path, `pages/.../installation.md:14`).
  - Symptom: `runRaw` spawns `ocr` with no `shell: true`, while `probeCommand` (`CliService.ts:45`) and `install` (`:87`) do pass `shell: process.platform === 'win32'`. npm's global `ocr` is a `.cmd` shim, which Node/Java cannot execute without a shell.
  - Root cause: Shell mode applied on the probe/install paths but omitted on the real execution path; the IDEA plugin's own comment at `CliService.kt:71` states the `.cmd` constraint.
  - Reproduction: Install via the panel's npm button on Windows, then start a review → `spawn ocr ENOENT`/`EINVAL`; Test Connection and config save fail the same way while the probe says installed.
  - Impact: The entire plugin is dead for every Windows user whose CLI came from npm, and the status line contradicts the error.
  - Fix shape: Pass `shell: process.platform === 'win32'` in `runRaw` at `CliService.ts:121` and `CliService.kt:175`.

## P2 — Degraded behaviour

- [BUG-005] [CONSENSUS: 3/4] A degenerate review range is reviewed as an empty diff, exits 0, and posts a clean summary
  - File: `action.yml:436` (swallowed base fetch), `action.yml:437` (merge-base falls back to `HEAD_SHA`), `action.yml:989` (`--from`/`--to`), `action.yml:1043-1045` (post gated only on exit 0), `scripts/github-actions/post-review-comments.js:334-346`
  - Feature: GitHub Action — compute merge-base → review → post
  - Precondition: base ref unresolvable at run time (base branch renamed/deleted after the PR opened), or (`workflow_run` variant) the caller supplies no `base_ref`/`head_sha` so both resolve empty.
  - Symptom: `git fetch origin "$BASE_REF" 2>/dev/null || true` hides the failure; `git merge-base … || echo "$HEAD_SHA"` sets `MERGE_BASE = HEAD_SHA`; `ARGS=(--from X --to X)` (or `--from "" --to ""`) reviews zero files and exits 0; the post step rewrites the sticky summary with "No comments generated. Looks good to me."
  - Root cause: No fail-fast on an unresolved base/head, unlike the PR-number guard at `action.yml:399-406`; the merge-base fallback turns an error into a legitimate-looking empty range.
  - Reproduction: Delete the base branch after opening a PR; re-run the action → green run, clean summary, no files reviewed.
  - Impact: The PR's previous real summary is replaced by an all-clear from a review that covered nothing; inline history survives but the summary lies.
  - Fix shape: Fail the job when the base ref or merge-base cannot be resolved (`action.yml:437`), mirroring `action.yml:399-406`.

- [BUG-006] [CONSENSUS: 2/4] Fixed `/tmp` result/stderr paths allow symlink clobber and cross-posting on shared runners
  - File: `action.yml:1016-1017` (redirections), `action.yml:1030-1033` (artifact upload), `action.yml:1096-1097` (`resultPath`/`stderrPath` consumed)
  - Feature: GitHub Action — run review, upload artifacts, post comments
  - Precondition: self-hosted or otherwise shared runner where `/tmp` persists across jobs and co-tenants can create files (GitHub-hosted fresh VMs are unaffected).
  - Symptom: `ocr review … > /tmp/ocr-result.json 2>/tmp/ocr-stderr.log` follows any pre-existing symlink planted at those names; two concurrent jobs truncate and rewrite each other's files, so job A's post step reads job B's JSON.
  - Root cause: Fixed, predictable names in a world-writable directory instead of `${RUNNER_TEMP}`/`mktemp` with exclusive-create semantics.
  - Reproduction: `ln -s ~/.bashrc /tmp/ocr-result.json` on the host; the next review truncates and rewrites `~/.bashrc` with review JSON. Or run two jobs concurrently and observe job B's findings posted on job A's PR.
  - Impact: File clobber anywhere the runner user can write (startup-file persistence), cross-repository comment/artifact leakage, and exposure of another run's stderr (which can contain endpoint details).
  - Fix shape: Use `${RUNNER_TEMP}`/`mktemp` with exclusive create in `action.yml:1007-1033`.

- [BUG-007] [CONSENSUS: 2/4] One shared `current` process slot — Cancel kills the wrong process or nothing, and the orphan review keeps spending
  - File: `extensions/vscode/src/extension/services/CliService.ts:128,140-142,183-206`; `extensions/vscode/src/extension/services/ReviewSession.ts:40-43`
  - Feature: VS Code extension — sidebar review ↔ config panel / test commands sharing one `CliService`
  - Precondition: a review is running and any other CLI call starts (`ocr config set`, `ocr llm test`, or a second review).
  - Symptom: `runRaw` assigns every spawned process to `this.current`; the concurrent call overwrites it and clears it on exit. Cancel (`:183`) then either signals the short-lived config/test process or finds `null`, so the detached review process group is never signalled.
  - Root cause: Cancellation is process-global, not session-scoped; the IDEA twin keeps the handle per session (`ReviewSession.kt:86-89`).
  - Reproduction: Point `llm.url` at a slow endpoint, start a review, click Test, then Cancel → the test dies, the review continues; the UI discards the eventual result.
  - Impact: Silent cancel failure, an orphan process that bills the LLM to completion, and dropped findings.
  - Fix shape: Key the handle by session/purpose and pass the owned handle to `cancel()` at `CliService.ts:128,183`.

- [BUG-008] [CONSENSUS: 2/4] A slow or failed login-shell probe is cached for the life of the window — install cannot repair it
  - File: `extensions/vscode/src/extension/services/shellEnv.ts:30,37-45,56-74`; `extensions/vscode/src/extension/services/CliService.ts:107,121`; `extensions/vscode/src/extension/providers/ConfigPanelProvider.ts:139-142`
  - Feature: VS Code extension — shell environment resolution → CLI spawn / environment check / install
  - Precondition: the first `getShellEnv()` runs while zshrc/bashrc takes >5 s (nvm, network init), or the CLI is installed in-panel on macOS where the GUI PATH lacks `ocr`.
  - Symptom: `spawnSync(..., {timeout: 5000})` is killed, `parseEnvBlock` yields `{}`, and `cached` is permanently set to the minimal GUI `process.env`; the silent `catch` logs nothing. `resolveBin` also caches its failed lookup. A successful in-panel npm install calls only `invalidateEnvironmentCache()` (`CliService.ts:107`), leaving `shellEnv`'s `cached`/`binCache` untouched.
  - Root cause: Double cache (shell env and bin path) never invalidated, and a partial/timed-out parse treated as success.
  - Reproduction: Open VS Code on macOS, install via the panel, then start a review → ENOENT and the panel keeps showing "install"; restart the window fixes it.
  - Impact: No review, config set, or `llm test` works until VS Code restarts; install loop.
  - Fix shape: Invalidate `cached`/`binCache` after a successful install (`CliService.ts:107`), and retry rather than cache a partial parse (`shellEnv.ts:41-45`).
  - Severity dispute: P2 (codex) vs P3 (glm) — see Disputed entries.

- [BUG-009] [CONSENSUS: 2/4] GitLab and GitFlic example API clients call `urlopen` with no timeout — the pipeline hangs forever
  - File: `examples/gitlab_ci/post_review.py:688`, `examples/gitflic_ci/post_review.py:345`
  - Feature: CI example posters (GitLab, GitFlic)
  - Precondition: the API endpoint accepts the connection but never responds (firewall DROP, stalled proxy, black hole).
  - Symptom: `urllib.request.urlopen(req)` has no `timeout=`, so it blocks indefinitely; the `socket.timeout` handler at `gitlab_ci:717` can never fire and the retry wrapper is bypassed.
  - Root cause: Missing timeout; the sibling codeup (`examples/codeup_ci/post_review.py:205`) and gerrit (`examples/gerrit_ci/post_review.py:245`) examples pass one.
  - Reproduction: Run the GitLab example against an unroutable API host; the first call never returns and the job is killed at the platform timeout.
  - Impact: CI stage wedged, runner minutes burned, no diagnostics.
  - Fix shape: Pass `timeout=` to both `urlopen` calls.
  - Severity dispute: P3 (codex) vs P2 (glm) — see Disputed entries.

- [BUG-010] [CONSENSUS: 1/4] The zero-comment summary ignores the manifest's terminal state — a partial/budget-limited run posts a clean "LGTM"
  - File: `scripts/github-actions/post-review-comments.js:334-346`, `:224`
  - Feature: GitHub Action — post helper, empty-result branch
  - Precondition: `max_tokens_budget` exhausted (or any `terminal_state !== "complete"`) with zero findings.
  - Symptom: `comments.length === 0` chooses the ✅ icon from `warnings` alone; the same branch then hands `result.manifest` to `appendCheckpoint`, which rejects `terminal_state !== "complete"` (`:224`). The body is nevertheless posted as an all-clear.
  - Root cause: The all-clear decision and the checkpoint decision read different inputs; coverage gaps are not required to populate `warnings`.
  - Reproduction: Set a small `max_tokens_budget`, let it exhaust before any file reaches a comment, observe a green ✅ summary while the manifest records `failed(budget)`.
  - Impact: The PR shows a clean pass while files were skipped; the visible summary carries no coverage note.
  - Fix shape: Gate the all-clear body on `result.manifest.terminal_state === "complete"` at `post-review-comments.js:334`.

- [BUG-011] [CONSENSUS: 1/4] `startReview` has no running gate; a webview reload or second start overwrites the running review
  - File: `extensions/frontend/src/webview/App.tsx:21-25`, `extensions/frontend/src/webview/store.ts:53-61`, `extensions/vscode/src/extension/providers/SidebarProvider.ts:63-68,91,95-106`, `CommentProvider.ts:54`
  - Feature: VS Code extension — webview state machine ↔ ReviewSession ↔ CommentProvider
  - Precondition: the webview reloads/recreates while a review runs, or the user starts a second review before the first finishes.
  - Symptom: `init` carries no running-session field, so the remounted webview resets to `idle` and re-enables Start; a new `ReviewSession` replaces the old one without cancelling its process; whichever run finishes last calls `comments.show` and clears the other's threads.
  - Root cause: No running-session state in `init` and no start gate in the host.
  - Reproduction: Reload the window mid-review and click Start again → interleaved logs, the older run's threads silently replaced.
  - Impact: Lost/incoherent findings and a second billed run.
  - Fix shape: Include the running session in `init` and reject a second `startReview` at `SidebarProvider.ts:91`.

- [BUG-012] [CONSENSUS: 1/4] A git failure while building the file-status map reclassifies every branch/commit finding as "file missing"
  - File: `extensions/vscode/src/extension/services/GitService.ts:218-226,412-421`, `extensions/vscode/src/extension/providers/commentAnchor.ts:131-135`; IDEA twin `GitService.kt:174-176,276-286`
  - Feature: both plugins — review completion → file-status map → comment anchoring
  - Precondition: base ref force-pushed/deleted, or any git invocation fails (lock contention, timeout) during `prepareReviewFileStatus`.
  - Symptom: `getBranchDiff` catches every failure and returns `[]`; the status map is then filled from the empty list, so every `fileStatus` is null and all findings render sidebar-only with "file missing" for files that exist.
  - Root cause: An empty diff and a failed diff are represented identically, and the status map trusts the empty list.
  - Reproduction: Force-push the base branch during a branch-mode review, then inspect the results: every finding is non-jumpable.
  - Impact: A successful review looks broken; the user cannot jump to any finding.
  - Fix shape: Propagate the git error to `prepareReviewFileStatus` instead of returning `[]` at `GitService.ts:218-226`.

- [BUG-013] [CONSENSUS: 1/4] A malformed webview message orphans the IDEA review process and can flip a finished review to FAILED
  - File: `extensions/idea/src/main/kotlin/com/alibaba/opencodereview/idea/review/SidebarRouter.kt:109-116`, `:75-88`, `:160-165`, `:182-191`
  - Feature: IntelliJ plugin — message parsing ↔ ReviewSession lifecycle
  - Precondition: any message that parses to `WebviewToHost.Malformed` (e.g. `openFileDiff` without `path`, a malformed `startReview.options`, a newer frontend bundle); it also fires after a review finished because the session reference is never cleared.
  - Symptom: The Malformed branch does `session.getAndSet(null)` without `cancel()` and posts FAILED; the next CancelReview and StartReview both see `null`, so the orphaned `ocr review` is never killed, and its eventual result is dropped by the identity check.
  - Root cause: Session retirement on the error path skips cancellation.
  - Reproduction: Send an `openFileDiff` message without `path` after a review completes → FAILED is posted over DONE and the CLI keeps spending.
  - Impact: Misreported state, unbounded LLM spend, discarded findings.
  - Fix shape: Call `cancel()` before clearing the session in `SidebarRouter.kt:109-116`.

- [BUG-014] [CONSENSUS: 1/4] IDEA Apply falls back to review-time line numbers after an external reload and rewrites the wrong lines
  - File: `extensions/idea/src/main/kotlin/com/alibaba/opencodereview/idea/providers/CommentService.kt:251,258,330,274-286,297`
  - Feature: IntelliJ plugin — apply suggestion on a stale mount
  - Precondition: the file changes externally and the document reloads (branch switch, `git checkout`, revert), then the user clicks Apply.
  - Symptom: `lineRangeIn(index, document)` fails to match the reloaded document and falls through to `clampLineRange(target.startLine, target.endLine, …)` — the pre-reload CLI coordinates — which are then replaced/deleted and saved, and the comment is marked `APPLIED`.
  - Root cause: The stale-document guard only fires when the range is null, not on document mismatch.
  - Reproduction: Review, `git checkout` another branch, click Apply → whatever now occupies those line numbers is overwritten.
  - Impact: Silent code corruption, marked handled so the user will not revisit.
  - Fix shape: Compare the document's content/version against the mounted snapshot before `clampLineRange` at `CommentService.kt:258-330`.

- [BUG-015] [CONSENSUS: 1/4] VS Code contributes `ocr.review.start` / `ocr.review.cancel` but never registers them
  - File: `extensions/vscode/src/extension/commands.ts:14-31`; contract at `extensions/vscode/package.json:45-51`, `extensions/frontend/src/shared/constants.ts:8-9`
  - Feature: VS Code command palette / keybinding surface
  - Precondition: any palette invocation or keybinding bound to the declared review commands.
  - Symptom: `registerCommands` registers `configOpen`, `commentApply`, `commentDiscard`, `commentFalsePositive` only; neither review id appears anywhere in `extensions/vscode/src` outside the constants file.
  - Root cause: Declared contributions were never wired to handlers.
  - Reproduction: Run "Start Review" from the palette → "command 'ocr.review.start' not found".
  - Impact: Two advertised commands are dead; the webview path remains the only way to drive a review.
  - Fix shape: Register both ids in `commands.ts` delegating to `SidebarProvider.startReview`/`cancelReview`, or remove the contributions.

- [BUG-016] [CONSENSUS: 1/4] `ocr config set providers.<MixedCase>.<field>` persists a key the resolver and extensions never read
  - File: `cmd/opencodereview/config_cmd.go:1002-1018`; reader at `internal/llm/resolver.go:411`; contract `extensions/idea/.../ConfigDraft.kt:132-135`, `internal/llm/providers.go:518-524`
  - Feature: config CLI — preset-provider configuration
  - Precondition: a non-canonical spelling of a preset name, e.g. `providers.OpenAI.api_key` while `provider` is `openai`.
  - Symptom: `setProviderValue` calls case-insensitive `LookupProvider(name)`, then writes `cfg.Providers[name] = entry` with the raw spelling (`config_cmd.go:1018`); the resolver does an exact `cfg.Providers[cfg.Provider]` lookup (`resolver.go:411`) and the extensions read an exact map key.
  - Root cause: Lookup is case-insensitive but storage is not canonicalised; the Kotlin draft explicitly lowercases (`ConfigDraft.kt:132-135`).
  - Reproduction: `ocr config set provider openai` then `ocr config set providers.OpenAI.api_key sk-…` → `config get` prints the value, but `ocr review` cannot resolve the key/model.
  - Impact: Silent misconfiguration; the IDE panel shows the provider as unconfigured.
  - Fix shape: Store under `preset.Name` after a successful lookup at `config_cmd.go:1018`, or reject non-canonical spelling.

- [BUG-017] [CONSENSUS: 1/4] A documented `llm.auth_token_cmd` is treated as "not configured" and disables Start Review
  - File: `extensions/frontend/src/shared/configUtils.ts:71,98`, `extensions/vscode/src/extension/services/configParse.ts:39-45`; contract `internal/llm/resolver.go:337,649`
  - Feature: shared frontend readiness gate / sidebar Start Review
  - Precondition: the legacy `llm` block uses `auth_token_cmd` with no stored `auth_token` — the docs' recommended secret-manager setup.
  - Symptom: `OcrConfig.llm` has no `authTokenCmd`; `parseConfig` drops `auth_token_cmd`; `isConfigReady` requires `config.llm.authToken`, so `configured === false` and the primary button renders "Configure".
  - Root cause: The frontend config model is missing a field the Go resolver supports.
  - Reproduction: Configure `llm.url`, `llm.model`, `llm.auth_token_cmd` → the terminal `ocr review` works, the sidebar refuses to start.
  - Impact: The IDE integrations cannot start a review for a valid configuration.
  - Fix shape: Carry `authTokenCmd` through both parsers and accept `(authToken || authTokenCmd)` in `isConfigReady`.

- [BUG-018] [CONSENSUS: 1/4] The npm launcher silently replaces a pinned CLI version, contradicting the documented "prints an upgrade hint"
  - File: `scripts/update.js:154-165`, `bin/ocr.js:103-108`; documented at `pages/src/content/docs/en/installation.md:243`
  - Feature: npm distribution — background update check
  - Precondition: a newer version on the npm registry and a writable global prefix (nvm/Homebrew-style), re-armed about every 18 minutes.
  - Symptom: The detached updater runs `npm i -g @alibaba-group/open-code-review@<latest>` and prints nothing on success; the hint is written only when the install fails. A pinned `@1.2.3` is silently undone.
  - Root cause: Auto-upgrade implemented in place of the documented hint, with `stdio: "ignore"` and a swallowed catch.
  - Reproduction: Pin a version, run any `ocr` command with a newer release published → the binary changes without a message; a failed mid-install leaves `ocr` broken with no log.
  - Impact: Silent version change; a broken global install is invisible.
  - Fix shape: Print the hint instead of reinstalling, or make the upgrade opt-in, at `scripts/update.js:154`.

- [BUG-019] [CONSENSUS: 1/4] A custom-provider save that fails mid-sequence leaves a half-written provider; the error toast lasts 5 s
  - File: `extensions/vscode/src/extension/services/ConfigService.ts:93-97`, `extensions/frontend/src/webview/views/ConfigView.tsx:294,518-520`, `extensions/frontend/src/webview/ConfigPanelApp.tsx:41`
  - Feature: config panel (both IDEs) — custom provider create form
  - Precondition: one `config set` in the batch is rejected (e.g. a URL without a scheme, which the form does not validate), then Save.
  - Symptom: `setMany` applies entries one-by-one and aborts at the first failure with no rollback; the panel navigates away on click (`ConfigView.tsx:294`) before success; the toast auto-dismisses after 5 s.
  - Root cause: Non-transactional multi-key save plus navigate-on-click.
  - Reproduction: Add a provider with URL `my-gateway.internal/v1` → the list shows a provider with protocol/URL but no model/key, and the API-key field reports "Saved (leave blank to keep)" though none was written.
  - Impact: An unusable provider that looks configured; the user must retype the key.
  - Fix shape: Apply the batch atomically (or roll back on failure) in `ConfigService.ts:93-97`, and navigate only on success.

- [BUG-020] [CONSENSUS: 1/4] Clearing the model list or auth header in the custom-provider form silently does nothing
  - File: `extensions/frontend/src/shared/configUtils.ts:159-168` (also `:133-139`)
  - Feature: config panel (both IDEs) — custom provider edit form
  - Precondition: a provider with `models` or `auth_header` set; clear the field and Save.
  - Symptom: `buildCustomUpdateSaveEntries` emits a `models` entry only `if (models)` and an `auth_header` entry only `if (params.authHeader.trim())`; no unset entry is produced, so the old values persist and reappear after refresh.
  - Root cause: The builder can only write non-empty values; it never calls the CLI's `unset`.
  - Reproduction: Custom tab → edit → clear `models` → Save → the old list is back, with success reported.
  - Impact: These two fields cannot be cleared from either IDE.
  - Fix shape: Emit an unset for an emptied optional field at `configUtils.ts:159-168`.

- [BUG-021] [CONSENSUS: 1/4] Retry after a failed Branch or Commit review silently restarts a Workspace review
  - File: `extensions/frontend/src/webview/App.tsx:63`; mode already tracked in `extensions/frontend/src/webview/store.ts`
  - Feature: sidebar (both IDEs, shared webview) — FailedView retry
  - Precondition: a branch/commit review fails (provider timeout, auth error) and the user clicks Retry.
  - Symptom: `onRetry={() => start({ mode: ReviewMode.Workspace })}` hardcodes the mode and drops `from`/`to`/`commit`.
  - Root cause: Retry ignores the reducer's recorded `reviewMode` and refs.
  - Reproduction: Start a branch review → let it fail → Retry → a workspace review of uncommitted changes runs, presented as the same review.
  - Impact: The user draws conclusions from a different diff than the one that failed.
  - Fix shape: Replay the recorded mode and refs at `App.tsx:63`.

- [BUG-022] [CONSENSUS: 1/4] Comment threads mount at unverified line numbers whenever the reported lines are merely in-range
  - File: `extensions/vscode/src/extension/providers/commentAnchor.ts:111-118,86-97,195-200`
  - Feature: comment anchoring (both IDEs; IDEA port shares the logic)
  - Precondition: in-range-but-drifted line numbers (old-side/new-side coordinate drift), or a non-unique `existing_code` snippet combined with out-of-range numbers.
  - Symptom: `resolveLinesInContent` returns `startLine`/`endLine` verbatim whenever they are within the file and never consults `existingCode`; the `locateNote` warning is emitted only on the relocation path. Relocation itself is first-match-wins.
  - Root cause: In-range coordinates are treated as verified, though range validity says nothing about content correctness.
  - Reproduction: A finding whose lines shifted but remain inside the file → the thread pins to unrelated code with no warning; click it and review the wrong lines.
  - Impact: Trust loss; findings closed as addressed against the wrong code.
  - Fix shape: Verify the in-range window against `existingCode` (or at least surface `formatLocateNote`) at `commentAnchor.ts:111-118`.

## P3 — Sharp edges / footguns

- [BUG-023] [CONSENSUS: 2/4] `install.sh` has no overall deadline on the release API call or the binary download
  - File: `install.sh:38-39` (latest-release API), `install.sh:61` (asset download), `install.sh:64-70` (checksum, bounded)
  - Feature: `curl | sh` installer
  - Precondition: a stalled or black-holed GitHub API/asset host, or a mirror serving at just over the 1024 B/s floor.
  - Symptom: The API call has neither `--connect-timeout` nor `--max-time`; the download has `--connect-timeout 5 --speed-limit 1024 --speed-time 30` but no `--max-time`, so a host at ~1025 B/s is never cut off.
  - Root cause: Timeout flags applied inconsistently across the three curls.
  - Reproduction: Block api.github.com with a DROP rule and run `curl -fsSL … | sh` → the installer hangs; worst case download time is asset_size / 1024 B/s (~17 h for a 60 MB binary).
  - Impact: Hung installs with no error message.
  - Fix shape: Add `--connect-timeout 5 --max-time 15` at `install.sh:38` and `--max-time` at `install.sh:61`.

- [BUG-024] [CONSENSUS: 1/4] A PR-controlled `.opencodereview/rule.json` in the checked-out tree selects the review rules
  - File: `action.yml:414-421` (checkout), `cmd/opencodereview/shared.go:123-134,153`, `internal/config/rules/system_rules.go:299-326,410-441`
  - Feature: GitHub Action (`pull_request` caller) → `ocr review` rule resolution
  - Precondition: caller uses `on: pull_request`; a same-repository branch PR adds or edits `.opencodereview/rule.json`.
  - Symptom: The workspace is the merge commit, and `loadProjectRule(repoDir)` reads `<repoDir>/.opencodereview/rule.json` straight off disk with `os.ReadFile` rather than from the `--to` git object; the action only hashes the file into the checkpoint fingerprint (`action.yml:833-845`).
  - Root cause: Project rules are resolved from the working tree, which contains PR content on `pull_request`.
  - Reproduction: A PR edits the rule file to exclude its changed paths; the review posts clean.
  - Impact: The reviewed PR author selects the rules applied to their own diff and can suppress findings.
  - Fix shape: Load project rules from the reviewed git object, or pin the checkout to base, at `system_rules.go:410`.

- [BUG-025] [CONSENSUS: 1/4] `mktemp -u` picks the stderr FIFO name without creating it — a co-tenant can wedge the review step
  - File: `action.yml:1007-1008`
  - Feature: GitHub Action — `stream_progress=true` run step
  - Precondition: a shared self-hosted host where another process can create a file at the printed name between `mktemp -u` and `mkfifo`.
  - Symptom: `OCR_STDERR_FIFO="$(mktemp -u)"` creates nothing; `mkfifo` then fails with EEXIST and `|| exit 1` aborts before the review runs. When it succeeds, the predictable path lets a co-tenant attach a reader to the review's stderr.
  - Root cause: `-u` is inherently racy for a path that must not pre-exist.
  - Reproduction: A co-tenant creates the printed name; the review step exits on the bare `mkfifo` error.
  - Impact: Denial of the run, or a local read of provider errors/endpoint details on stderr.
  - Fix shape: Create the FIFO in a private `mktemp -d` and use that path at `action.yml:1007`.

- [BUG-026] [CONSENSUS: 1/4] The VS Code environment probe has no timeout — a hung `--version` freezes the check forever
  - File: `extensions/vscode/src/extension/services/CliService.ts:42-58`
  - Feature: VS Code extension — `checkEnvironment`
  - Precondition: a probe process that starts but never exits (broken npm shim, launcher waiting on stdin, wedged node).
  - Symptom: `spawn(resolveBin(bin), ['--version'])` has only `error`/`close` handlers and resolves only in `close`; a never-exiting process means the promise never settles, and every env-gated UI path hangs.
  - Root cause: No probe timeout or kill timer, unlike the IDEA twin's 10 s tree-kill.
  - Reproduction: Point `ocr` at a launcher that reads stdin; open the panel → "checking environment" forever.
  - Impact: The extension UI is stuck with no error and no recovery except a window reload.
  - Fix shape: Add a kill timer around the probe at `CliService.ts:42-58`.

- [BUG-027] [CONSENSUS: 1/4] Exit-1 reviews with any status other than `partial` discard findings that were successfully parsed
  - File: `extensions/vscode/src/extension/services/CliService.ts:149-162`; supported statuses at `ReviewSession.ts:15-21`
  - Feature: both extensions — CLI result recovery
  - Precondition: the CLI exits 1 while emitting a parsed result with `completed_with_errors` (or another incomplete status) and comments.
  - Symptom: Only `code === 1` is recoverable, and `if (result.status !== 'partial') throw` discards the parsed, comment-bearing result; the user sees a generic failure.
  - Root cause: The recovery allow-list is narrower than `INCOMPLETE_STATUSES`.
  - Reproduction: Force a review to exit 1 with `completed_with_errors` and findings → the UI shows failure with zero findings.
  - Impact: Successfully produced findings are lost.
  - Fix shape: Accept every incomplete status at `CliService.ts:149-162`.

- [BUG-028] [CONSENSUS: 1/4] Webview CSP nonce is a millisecond timestamp
  - File: `extensions/vscode/src/extension/providers/SidebarProvider.ts:133`, `extensions/vscode/src/extension/providers/ConfigPanelProvider.ts:153`
  - Feature: VS Code webview panels
  - Precondition: none; defense-in-depth.
  - Symptom: `const nonce = String(Date.now())` — guessable, and both panels created in the same millisecond share the identical nonce.
  - Root cause: A non-CSPRNG value used as a CSP nonce.
  - Reproduction: Open both panels quickly and compare the generated nonce.
  - Impact: Downgrades the CSP from an unpredictable allow-list to a guessable one; no injection sink found today.
  - Fix shape: Use `crypto.randomUUID()`/`randomBytes` in both providers.

- [BUG-029] [CONSENSUS: 1/4] IDEA JCEF webview ships without a Content-Security-Policy
  - File: `extensions/idea/src/main/kotlin/com/alibaba/opencodereview/idea/jcef/WebviewHtml.kt:43-92`
  - Feature: IntelliJ plugin — JCEF webview
  - Precondition: none; defense-in-depth.
  - Symptom: `buildPage` assembles HTML with inline bridge scripts and no CSP meta tag, unlike the nonce'd VS Code webviews.
  - Root cause: The page relies solely on JSON-encoding discipline in the bridge.
  - Reproduction: Static inspection — the generated HTML has no `<meta http-equiv="Content-Security-Policy">`.
  - Impact: One future injection sink becomes script execution inside the IDE webview with bridge access.
  - Fix shape: Add a restrictive CSP meta tag in `WebviewHtml.buildPage`.

- [BUG-030] [CONSENSUS: 1/4] VS Code `CliService` accumulates CLI output in memory without a bound
  - File: `extensions/vscode/src/extension/services/CliService.ts:131-134`
  - Feature: VS Code extension — CLI execution
  - Precondition: a very large review JSON or a model that floods stderr.
  - Symptom: `stdout += d.toString()` / `stderr += text` on every chunk with no cap.
  - Root cause: No equivalent of `GitService.runGit`'s `maxBuffer: 10MB` (`GitService.ts:505`).
  - Bound: O(n) retained bytes in n = total CLI output, plus O(n²) copying from repeated `+=` on the accumulated string.
  - Reproduction: Review a repository whose result JSON reaches hundreds of MB; watch extension-host memory.
  - Impact: Extension-host degradation or crash (taking down all extensions).
  - Fix shape: Cap accumulation or stream to a temp file at `CliService.ts:131-134`.

- [BUG-031] [CONSENSUS: 1/4] CI examples forward auth tokens on cross-host redirects
  - File: `examples/gitflic_ci/post_review.py:342`, `examples/codeup_ci/post_review.py:200`, `examples/gitlab_ci/post_review.py:684`; contrast `examples/gerrit_ci/post_review.py:237-238`
  - Feature: CI examples (GitFlic, CodeUp, GitLab)
  - Precondition: the API endpoint replies with a 302 to another host (server misconfiguration or an http:// MITM).
  - Symptom: urllib re-sends headers supplied via `add_header`/the Request constructor on a cross-host redirect; only `add_unredirected_header` headers are stripped.
  - Root cause: The three examples use the redirect-forwarding form; gerrit uses the correct form with a comment.
  - Reproduction: Point the API base URL at a host that redirects to an attacker domain; observe the token resent.
  - Impact: LLM/platform token disclosure to the redirect target.
  - Fix shape: Switch the three auth headers to `add_unredirected_header` as in `gerrit_ci:237-238`.

- [BUG-032] [CONSENSUS: 1/4] The GitFlic poster has no catch around the fallback/summary posts — one transient 5xx loses every folded finding
  - File: `examples/gitflic_ci/post_review.py:312,320` (fallback note and summary), `:300-305` (per-comment catch), `:476`
  - Feature: GitFlic example — publish loop
  - Precondition: a transient error on the fallback note or the final summary after inline posting.
  - Symptom: Inline failures are folded into `failed`, but the subsequent `post({"message": note})` and final summary `post(...)` are uncaught; the exception propagates to `main` as a traceback.
  - Root cause: Only the inline loop is error-handled.
  - Reproduction: Return a 5xx for the fallback/summary after inline posts → red pipeline with a stack trace and no summary.
  - Impact: Partially posted review, folded findings lost.
  - Fix shape: Wrap the fallback/summary posts at `examples/gitflic_ci/post_review.py:312-320`.

- [BUG-033] [CONSENSUS: 1/4] GitFlic example's git subprocesses have no timeout
  - File: `examples/gitflic_ci/post_review.py:381-385`
  - Feature: GitFlic example
  - Precondition: a hung `git` invocation (interactive auth prompt, stalled filesystem).
  - Symptom: `subprocess.run(["git", *args], …)` passes no `timeout=`.
  - Root cause: Missing subprocess timeout.
  - Reproduction: Make the origin remote require interactive SSH auth with no askpass; any `_git` call blocks forever.
  - Impact: Stuck CI job consuming runner time.
  - Fix shape: Pass `timeout=` in `_git()` at `post_review.py:381-385`.

- [BUG-034] [CONSENSUS: 1/4] The CodeUp example runs the paid review before validating that it can post
  - File: `examples/codeup_ci/post_review.py:269-287`
  - Feature: CodeUp example — `main` ordering
  - Precondition: pipeline misconfiguration — a missing `CODEUP_DOMAIN`/`_ORG_ID`/`_REPO_ID`/`_MR_LOCAL_ID`/`_TOKEN`.
  - Symptom: `run_ocr_review` (subprocess timeout 1800 s) runs first; `_require_env` discovers the missing variable only afterwards and returns 1.
  - Root cause: Validation is ordered after the expensive step.
  - Reproduction: Omit `CODEUP_TOKEN` and push → a full LLM spend with findings only in the job log.
  - Impact: Wasted review cost per push, nothing reaches the MR.
  - Fix shape: Call `_require_env` before `run_ocr_review` at `examples/codeup_ci/post_review.py:269`.

- [BUG-035] [CONSENSUS: 1/4] `issue_comment` on a plain issue fails the job on a bogus PR fetch
  - File: `action.yml:384,429-431`
  - Feature: GitHub Action — review-range resolution
  - Precondition: the trigger phrase is commented on a standalone issue (not a PR) in a workflow wired to `issue_comment`.
  - Symptom: `EVENT_PR_NUMBER` falls back to `github.event.issue.number` without checking `github.event.issue.pull_request`; the fetch step is the only unguarded git call and errors out.
  - Root cause: Missing `issue.pull_request` gate; the example workflow has it (`examples/github_actions/ocr-review.yml:38`) but the action does not.
  - Reproduction: Comment `/open-code-review` on a plain issue → red job.
  - Impact: Red CI and user confusion; no data impact.
  - Fix shape: Gate the fetch step on `github.event.issue.pull_request` at `action.yml:424-431`.

- [BUG-036] [CONSENSUS: 1/4] `llm_extra_body` JSON is validated only when `llm_reasoning_effort` is set
  - File: `action.yml:681-703`
  - Feature: GitHub Action — LLM configuration inputs
  - Precondition: invalid JSON in `llm_extra_body` with an empty `llm_reasoning_effort`.
  - Symptom: The `node -e` step that validates and merges runs only `if [ -n "${LLM_REASONING_EFFORT:-}" ]`, so extra-body-only configuration skips validation and fails later at request time.
  - Root cause: Validation coupled to an unrelated condition.
  - Reproduction: Set `llm_extra_body: '{"bad json'` and leave reasoning effort empty → the action passes, `ocr` fails at the first model request.
  - Impact: Delayed, low-signal failure.
  - Fix shape: Validate `llm_extra_body` unconditionally at `action.yml:681-703`.

- [BUG-037] [CONSENSUS: 1/4] Example workflow pins the action to a mutable branch ref
  - File: `examples/github_actions/ocr-review.yml:114` (also `:98`)
  - Feature: CI examples (GitHub Actions)
  - Precondition: a user copies the example workflow.
  - Symptom: The workflow uses `alibaba/open-code-review@main` and `actions/github-script@v7`, so future upstream pushes change the code executing with the copier's `secrets.OCR_LLM_AUTH_TOKEN`; the action's own `action.yml:415` pins checkout by full SHA.
  - Root cause: Branch/tag refs instead of commit SHAs.
  - Reproduction: Any later commit to upstream main runs in the copier's repo with their secrets.
  - Impact: Supply-chain exposure of the copier's repo token and LLM secrets.
  - Fix shape: Pin to a release commit SHA at `ocr-review.yml:114` and `:98`.

- [BUG-038] [CONSENSUS: 1/4] VS Code validates comment shape only for `partial`, then trusts every other status
  - File: `extensions/vscode/src/extension/services/cliParse.ts:43-54,59`; contract `extensions/idea/.../CliParse.kt:147`
  - Feature: parsing the CLI JSON review result
  - Precondition: a malformed envelope with a non-partial status (`success`, `complete`, `completed_with_warnings`, `completed_with_errors`, `skipped`).
  - Symptom: The per-comment validation is inside `if (json.status === 'partial')`; every other status goes straight to `json.comments.map(toComment)`, which copies `path`/`content` unchecked.
  - Root cause: Validation scoped to one status instead of to the presence of a `comments` array; the Kotlin parser validates every accepted result.
  - Reproduction: `parseCliResult('{"status":"completed_with_warnings","comments":[{"content":"x"}]}')` yields a comment with `path === undefined`.
  - Impact: Comments that silently fail to mount or throw during rendering.
  - Fix shape: Apply the per-comment check whenever `comments` is an array at `cliParse.ts:43`.

- [BUG-039] [CONSENSUS: 1/4] `comments` is `null`, not `[]`, when every subtask fails
  - File: `internal/agent/agent.go:858`, `cmd/opencodereview/output.go:364,730`; contract `pages/src/content/docs/en/cli-reference.md:360`
  - Feature: `ocr review --format json` output contract
  - Precondition: every dispatched subtask hard-fails (invalid endpoint/API key), so the collector produces no comments.
  - Symptom: `dispatchSubtasks` returns `nil, err`; `jsonOutput.Comments` has no `omitempty`, so a nil slice encodes as `"comments": null`, contradicting "Always present, possibly empty".
  - Root cause: The all-failed path never initialises the slice; only the normal collector returns `[]`.
  - Reproduction: Run with an invalid endpoint so all subtasks fail → stdout has `"comments": null`.
  - Impact: JSON consumers that iterate `comments` (the post script, extensions) hit null on exactly the failure path most likely to be inspected.
  - Fix shape: Initialise `comments := []model.LlmComment{}` where the result is assembled (`output.go:364`).

- [BUG-040] [CONSENSUS: 1/4] The `code_comment` parser ignores the registry's required fields and silently defaults severity to `low`
  - File: `internal/tool/code_comment.go:161-163,142-144,182-196`; contract `internal/config/toolsconfig/tools.json:80-86`, `pages/src/content/docs/en/tools.md:179-184`
  - Feature: model-facing `code_comment` tool schema and parser
  - Precondition: a model that follows `tools.md` (or a custom prompt) and omits `category`/`severity`.
  - Symptom: `parseCommentsInner` accepts an item with only `content`; missing/unknown `category`/`severity` are coerced to `other`/`low`; only `path` and `content` gate inclusion, though the registry marks five fields required.
  - Root cause: The enforced contract matches neither the schema nor the docs.
  - Reproduction: `ParseComments({"comments":[{"content":"x","path":"a.go"}]})` succeeds with `Severity:"low"`, `Category:"other"`.
  - Impact: Routing changes — `route_severity_below=medium` sends all such findings to the summary, and `route_categories` sees only `other`.
  - Fix shape: Enforce the registry's `required` list with the model-facing error, or relax the schema and docs to the implemented set, at `code_comment.go:161-196`.

- [BUG-041] [CONSENSUS: 1/4] `code_search` truncates globally at 100, while the docs promise a per-file cap and a different note
  - File: `internal/tool/code_search.go:94,232-248`; contract `pages/src/content/docs/en/tools.md:438-443`
  - Feature: `code_search` result limits advertised to the model
  - Precondition: a search with >100 matches spread over several files.
  - Symptom: The grep uses `--max-count 101` per file, but rendering stops once `matchCount >= 100` total, so later files are omitted; the emitted prefix is "Showing the first 100 matches across N matching files…", not the documented sentence.
  - Root cause: A global cap implemented behind a per-file documentation claim.
  - Reproduction: Two files with 60 matching lines each → at most 100 lines total, with the undocumented prefix.
  - Impact: Silent loss of matches; prompt guidance and any test asserting the documented text are wrong.
  - Fix shape: Reconcile the cap and the note between `code_search.go:232-248` and `tools.md:438-443`.

- [BUG-042] [CONSENSUS: 1/4] Extensions parse an unset review `language` as `Chinese`, while the CLI and docs default to English
  - File: `extensions/vscode/src/extension/services/configParse.ts:46`, `extensions/idea/.../ConfigParse.kt:71`; contract `pages/.../configuration.md:480-481`, `internal/config/template/template.go:279-283`
  - Feature: `language` field of the config object passed to the webview
  - Precondition: a config with no `language` (the default).
  - Symptom: Both parsers substitute `"Chinese"` when the field is absent; the CLI's `resolveLang("")` returns `"English"`.
  - Root cause: A hard-coded wrong default in the frontend parsers.
  - Reproduction: Log the config payload for a default config → `config.language === "Chinese"` while reviews are written in English.
  - Impact: Latent contract drift; any future language selector or round-trip save would flip review output to Chinese.
  - Fix shape: Default to `''` or `"English"` in both parsers.

- [BUG-043] [CONSENSUS: 1/4] Analysis runs can emit `status: failed` with no manifest, outside the documented status set
  - File: `cmd/opencodereview/output.go:459-462`; contract `pages/src/content/docs/en/cli-reference.md:356`
  - Feature: top-level `status` of the JSON envelope
  - Precondition: `ocr review --commit <sha> --analysis --format json` against a failing LLM configuration.
  - Symptom: When `manifest == nil` and `combined.ReviewStatus == "failed"`, the analysis branch sets `payload.Status = "failed"`, a value the docs assign only to manifest-bearing outputs.
  - Root cause: The analysis branch copies `ReviewStatus` into a field with a different documented enum.
  - Reproduction: Run the command → `.status == "failed"` with `.manifest` absent.
  - Impact: Consumers switching on the documented set hit an unhandled branch; `review_status` already carries the failure.
  - Fix shape: Keep top-level `status` within the documented set at `output.go:459-462`.

- [BUG-044] [CONSENSUS: 1/4] `npm install -g` hangs forever when the postinstall binary download stalls
  - File: `scripts/install.js:102-115`
  - Feature: npm distribution — postinstall binary download
  - Precondition: a network where the GitHub release endpoint connects but stalls (firewall, captive portal, dead proxy).
  - Symptom: `https.get` in `download()` passes no `timeout` and no overall budget; `install.sh:61` at least has connect/speed limits.
  - Root cause: No timeout on the Node downloader.
  - Reproduction: Run `npm install -g @alibaba-group/open-code-review` behind a stalled proxy → "Downloading …" forever.
  - Impact: Hung install with no error; the user must kill npm.
  - Fix shape: Add a socket/overall timeout to `https.get` at `scripts/install.js:102`.

- [BUG-045] [CONSENSUS: 1/4] Comment cards show "L0" for findings that have no line anchor
  - File: `extensions/vscode/src/extension/services/cliParse.ts:33`, `extensions/frontend/src/webview/components/CommentCard.tsx:30`
  - Feature: extension results list (both IDEs) — DoneView cards
  - Precondition: findings without a resolvable location (cross-file observations, no `existing_code`, files outside the diff).
  - Symptom: `startLine: raw.start_line ?? 0` uses 0 as the missing sentinel and the card renders it verbatim (`pkg/util.go L0`), though jumping already treats 0 as unresolved (`CommentProvider.ts:236-239`).
  - Root cause: The missing-sentinel is not suppressed in the card header.
  - Reproduction: Finish a review with cross-file findings and inspect the results list.
  - Impact: A nonsense line label in the primary results list.
  - Fix shape: Omit the line label when `startLine === 0` at `CommentCard.tsx:30`.

- [BUG-046] [CONSENSUS: 1/4] The "false positive" feedback action is fully wired but unreachable from any UI
  - File: `extensions/vscode/package.json:70-81`, `extensions/frontend/src/webview/components/CommentCard.tsx:31-35`; wired at `commands.ts:30`, `SidebarProvider.ts:123-127`
  - Feature: extension comment actions — false-positive feedback
  - Precondition: a user wants to flag a comment as a false positive.
  - Symptom: The command and message path exist, but the comment-thread title menu contributes only Apply and Discard, and the results card renders only two buttons.
  - Root cause: A missing menu/button contribution.
  - Reproduction: Try to mark any finding as a false positive from either IDE → no affordance exists.
  - Impact: A distinct feedback signal cannot be given from the extensions.
  - Fix shape: Add `ocr.comment.falsePositive` to the thread title menu at `package.json:70-81` and a card button.

- [BUG-047] [CONSENSUS: 1/4] The official-provider form saves a provider with no API key and no ambient env var, then reports it configured
  - File: `extensions/frontend/src/webview/views/ConfigView.tsx:356`, `extensions/frontend/src/shared/configUtils.ts:94,111-113`
  - Feature: config panel — official provider form
  - Precondition: blank API key, no ambient env var, Save without Test.
  - Symptom: `canSave` requires only a model; the blank key is not written; `isConfigReady` returns true for any preset with a model. The banner shows the provider active and Start is enabled; the first review fails auth.
  - Root cause: Readiness ignores the credential for preset providers, assuming ambient env.
  - Reproduction: Onboard `openai` with a model and no key → panel says configured, review fails with an auth error.
  - Impact: A confidently wrong "configured" state.
  - Fix shape: Require a key or verify the env var at save time in `ConfigView.tsx:356`/`configUtils.ts:94`.

- [BUG-048] [CONSENSUS: 1/4] Deleting the active custom provider clears the whole config behind a generic confirm dialog
  - File: `extensions/vscode/src/extension/services/ConfigService.ts:48-50`, `extensions/vscode/src/extension/providers/ConfigPanelProvider.ts:99-104`
  - Feature: config panel — custom provider deletion
  - Precondition: delete the provider that is currently active.
  - Symptom: `deleteCustomProvider` removes the entry and also deletes `provider` and `model`; the confirm text names only the provider, so the setup flips to "not configured" when the dialog closes.
  - Root cause: Deactivation is an undocumented side effect of deletion.
  - Reproduction: Delete the active provider → the next review fails with "no valid LLM endpoint configured".
  - Impact: The action did more than the dialog promised; the deleted API key existed only in config.
  - Fix shape: Warn that the provider is active (or require re-selection) in `ConfigPanelProvider.ts:99-104`.

- [BUG-049] [CONSENSUS: 1/4] `WhySection` references eight i18n keys that exist in no locale
  - File: `pages/src/components/WhySection.tsx:13-14,20-21,27-28,44-45`; contract `pages/src/i18n/types.ts`, `pages/src/i18n/en.ts:4`
  - Feature: marketing-page i18n
  - Precondition: mount the component (currently not imported anywhere).
  - Symptom: `t('why.title')`, `t('why.sectionLabel')`, and `why.case{1..3}{Title,Desc}` resolve to nothing in en/zh/ja/ko/ru/tr; `t` returns the key (`context.tsx:58`) because it takes `key: string`.
  - Root cause: Eight keys added to the component but never to the locale maps.
  - Reproduction: Mount `<WhySection />` → headings read "why.title", "why.case1Desc".
  - Impact: Latent; no user-visible page renders it today.
  - Fix shape: Add the eight `why.*` keys to all six locale maps, or delete the unused component.

## Disputed entries

- [BUG-001] **[CONTESTED: P0 (glm) vs rejected (codex)]** — Codex's report lists "Posting helper resolved from the PR workspace" under rejected candidates: *"for a published remote action `GITHUB_ACTION_PATH` always exists and wins, so the fallback grants no new capability."* GLM's report claims: *"On a `pull_request` event run inside a `container:` job, a PR that adds `scripts/github-actions/post-review-comments.js` … gets that file executed … Arbitrary code execution in CI."* Adjudication: codex's reason assumes `fs.existsSync(GITHUB_ACTION_PATH)` is true, but in a container job that value is a host path invisible inside the container — the action's own comment at `action.yml:1076-1082` states exactly this. Combined with the checkout step passing no `ref:` (so `pull_request` checks out the merge commit, not the base its comment claims), GLM's chain holds for that caller shape. Retained at P0, narrowed to the `pull_request` + container precondition. Codex's rejection stands for non-container callers.

- [BUG-002] **[DISPUTED-SEVERITY: P1 (codex) vs P2 (glm)]** — codex: *"a single click runs an arbitrary registered command … originating from untrusted review content"* → P1. glm: *"Arbitrary VS Code command execution in the user's session, one click away"* → P2. Synthesis keeps P1: one click to a privileged sink from untrusted input matches the plan's P1 definition.

- [BUG-008] **[DISPUTED-SEVERITY: P2 (codex) vs P3 (glm)]** — codex treats it as a flow break (*"install loop; no review … works until VS Code restarts"*) → P2; glm scopes it to *"the very next `ocr` invocation still fails with ENOENT until the window is reloaded"* → P3. Synthesis keeps P2 because the broader codex chain (any slow first probe, permanently cached) also disables the environment check itself.

- [BUG-009] **[DISPUTED-SEVERITY: P3 (codex) vs P2 (glm)]** — codex grades the GitLab hang P3; glm grades the GitLab+GitFlic pair P2 and notes the retry wrapper is bypassed. Synthesis keeps P2: a black-holed endpoint wedges the CI job for the platform's full timeout, which is visible breakage rather than a footgun.

## Coverage table

| Kickoff area | Files read (lane) | Outcome |
|---|---|---|
| B1 Review-pipeline trust boundary | `action.yml:382-1100` (codex, glm); `extensions/vscode` `cliParse.ts`, `CommentProvider.ts`, `commentAnchor.ts`, `GitService.ts` (codex, glm, minimax); `extensions/idea` `SidebarRouter.kt`, `CommentService.kt` (codex); `cmd/opencodereview/shared.go`, `review_cmd.go` (codex); `internal/config/rules/system_rules.go` (codex); `internal/tool/code_comment.go`, `code_search.go` (kimi); `internal/agent/agent.go` (kimi) | 8 findings: BUG-001, 002, 003, 024, 038, 039, 040, 041. `internal/llmloop` **not read** — prompt assembly / tool-call handoff unverified. |
| B2 Filesystem and path handling | `extensions/vscode` `GitService.ts:441`, `commentAnchor.ts`, `CommentProvider.ts` (codex, glm); `action.yml` temp paths (codex, glm); `install.sh`, `scripts/` (minimax) | 4 findings: BUG-003, 006, 023, 025. `internal/pathutil`, `internal/scan`, `internal/viewer`, `internal/session` **not read** — Go-side traversal/symlink/temp handling unverified. |
| B3 Secrets and credentials | `internal/llm/resolver.go`, `providers.go` (kimi); example auth-header code (glm); `action.yml` stderr file (glm) | 2 findings: BUG-006, 031. `internal/chatgptauth` and `internal/telemetry` **not read** — token file modes and telemetry payloads unaudited. |
| B4 Network and subprocess | `examples/*/post_review.py` (codex, glm); `install.sh` (codex, glm); `scripts/install.js`, `scripts/update.js` (minimax); `action.yml` helper (codex, glm) | 7 findings: BUG-009, 018, 023, 031, 032, 033, 044. `internal/mcp`, `internal/gitcmd`, and the `internal/llm` HTTP transport **not read** — redirect/TLS/cancellation plumbing unverified. |
| B5 Resource bounds and concurrency | `extensions/vscode` `CliService.ts` (glm); `action.yml` `/tmp` + FIFO (codex, glm); npm launcher (minimax); example retry loops (codex, glm) | 4 findings: BUG-006, 007, 008, 030. `internal/diff` **not read** — no complexity bound produced for the diff path. |

- Features with at least one bug found: GitHub Action (`action.yml`), `install.sh`, npm distribution, VS Code extension, IntelliJ plugin, shared webview, config CLI, JSON output contract, `internal/tool`, `internal/agent`, GitLab/GitFlic/CodeUp/GitHub examples, marketing-page i18n.
- Features where zero bugs were found (limited evidence): shared webview comment rendering (plain text, no Markdown sink — inspected), npm platform manifests (static only), the example gerrit poster (its `add_unredirected_header` and `timeout=` are the correct pattern the others diverge from).

## Coverage gap report

The following kickoff areas produced **no coverage at all** — not "clean", simply unread by any lane. This is the biggest limitation of the audit and should drive a follow-up.

- `internal/llmloop/` — prompt construction, tool-result injection, warnings aggregation. No lane read it. B1's central question (does repo/diff content reach a tool argument, shell word, or URL) is therefore only answered for the `action.yml`/extension sinks, not for the Go agent loop.
- `internal/pathutil/`, `internal/scan/`, `internal/viewer/`, `internal/session/` — B2's named targets. Zero traversal/symlink/temp audits.
- `internal/chatgptauth/`, `internal/telemetry/` — B3's named targets. Token storage permissions, token-in-log redaction, and telemetry payload contents are unaudited.
- `internal/mcp/`, `internal/gitcmd/`, and the `internal/llm` HTTP transport — B4's named targets. argv-vs-shell-string construction, redirect/TLS handling, and timeout plumbing are unaudited.
- `internal/diff/` — B5's named target. No quadratic-work or allocation bound was produced for the diff path.
- `acp/` — wholly unread.
- `internal/config/` beyond `rules/system_rules.go` and `template.go` — no audit of config-file parsing, secret handling, or write permissions.

Within the areas that were read, two sub-features got only a partial pass and are flagged for follow-up: the IDEA plugin's config-write atomicity (`CommentService.kt` write path was not re-read) and the GitHub Action's checkpoint mechanics (only the fingerprint and terminal-state reads were traced).

## Rejected candidates

Things that looked like bugs and were not, with the reason.

- **Posting helper resolved from the PR workspace (non-container callers).** For a published remote action outside a container, `GITHUB_ACTION_PATH` exists and sorts first in the `roots` list, so the workspace fallback is never reached. Rejected for that caller shape — retained as BUG-001 only for container jobs.
- **Empty PR number → `/issues//comments` 404.** Guarded before the review at `action.yml:399-406` with an explicit `::error` and `exit 1`.
- **`stream_progress` FIFO truncated before the post step reads it.** `wait "$TEE_PID"` at `action.yml:1013` guarantees `tee` has flushed before `action.yml:1024` reads the file.
- **VS Code `runRaw` pipe deadlock.** Both stdout and stderr are drained immediately (`CliService.ts:131-139`); there is no `spawnSync`-style buffer stall. (The absence of a size bound is a separate, retained finding: BUG-030.)
- **Commit SHA treated as a git option.** `getCommitFiles` passes `--end-of-options` before the SHA (`GitService.ts:244-251`).
- **Mirror-supplied checksum is self-consistent.** `install.sh:51-56` warns explicitly that an unofficial mirror's checksum integrity is not guaranteed; the default path fetches both from GitHub over TLS.
- **In-panel npm install hangs forever.** `CliService.install` has no timeout, but it is user-initiated and streams npm output; the higher-impact variant (the stale shell env that makes the post-install probe fail) is retained as BUG-008.
- **Action re-run duplicates inline comments.** `RUN_TAG` includes `runAttempt` (`post-review-comments.js:1007-1015`), so a new attempt re-posts; however, `incremental` exists for history-aware posting and the action documents that inline comments are appended unless it is enabled — non-incremental posting is the documented default.
- **GitLab job retry re-posts inline comments.** Tags are job-scoped (`examples/gitlab_ci/post_review.py:1468-1469`), so a retry duplicates them; `OCR_INCREMENTAL` is the documented remedy and non-incremental posting is the intended default.
- **Webview log list is unbounded.** `extensions/frontend/src/webview/store.ts:79-80`, rendered whole at `LogViewer.tsx:23-25`: O(emitted lines) DOM nodes, reset at the next run. A resource bound, not a flow break; recorded rather than graded.
- **IDEA config-write atomicity.** Not re-read in this pass, so neither claimed nor rejected; listed as a coverage gap above.
