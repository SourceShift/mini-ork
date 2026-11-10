# Task-status audit — F-ide-zed (133 tasks)

## Summary

All 133 claimed tasks from the zed-mini-ork group were checked against the fork at HEAD (`5bf984b59c`, 2026-10-29), its pinned `mini_ork_ui` submodule (`38a1f7a`), the mini-ork engine at `main` (`ef55e7d8`) and the live machine state. **80 are DONE**, **28 are OPEN**, **10 are STALE** and **15 are UNKNOWN** _(2026-11-10: the closures since the audit are listed under "Closed since the audit")_. The IDE half of this group has genuinely shipped: the crate pins 9 node tabs including Harness, the shared zoom cluster, the breadcrumb, full-width pages, the title-bar project switcher, the `PWD` fix and the registry helpers all exist in code, the app bundle was built and the :7090 server answered 200 on audit day (a live pid in a report is stale by the next reboot; treat these as audit-day observations, not current state). What is still genuinely open splits into four families. (1) *Directory picking*: `pick_files` still hardcodes `directories: false` (store.rs:707), so the whole pick-files-directories epic (spec written, card built) is stuck on one flag. (2) *Verification, not code*: roughly a dozen rows are awaiting a human to look at a screen (gui-verify, gui-replay, zoom-gui-smoke, launch-verify, tcc-grant, user-verify) — the features exist but no one recorded the observation. (3) *Engine decisions never taken*: the abstain contract, the ide_pages-vs-Rust-FE boundary, the parity decision, the v2 context switch, the `Open in web UI` button and the ACP trio all remain unrecorded. (4) *Small real gaps*: the rust-analyzer scan exclusions, the Board's filter/totals controls, board card Retry, the `issues` IDE page, and the 200-journey corpus. Three rows that were in this list are closed — `build_parser()` in `mini_ork/threads/cli.py`, the hardcoded `python3` in `_synth_verify` (`c2f807a1`, both kickoff emitters now use `sys.executable`) and the gitleaks gate (`b70b4690` + `c089a1ee`).


Verdict counts: DONE 80 · OPEN 28 · STALE 10 · UNKNOWN 15 _(2026-11-10; recounted from the table after closing `threads-builder`, `reflect-timeout-shape`, `p1-2-abstention-contracts` and `abstention-fold`)_


## Table

| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| threads-builder | 2026-10-29 | pending | DONE | `mini_ork/threads/cli.py` now exposes `build_parser()` (subparsers for all six verbs) and `main()` dispatches through it; `VERBS`/`_HANDLERS`/`_USAGE` are cross-checked by `tests/unit/test_threads_cli_parser.py` |
| orca-inventory | 2026-10-29 | in_progress | DONE | 9d63cfb20a "docs(orca-parity): inventory Orca and grade mini-ork against it"; docs/orca-parity/orca-cli.md + README.md |
| orca-parity-matrix | 2026-10-29 | pending | DONE | 9d63cfb20a + f04ce2c8cd; docs/orca-parity/matrix.md |
| journey-scale | 2026-10-29 | pending | OPEN | 15 journeys in docs/user-journeys/journeys/*.yaml; docs/user-journeys/README.md:131 still names the 200-journey floor |
| user-relaunch-verify | 2026-10-28 | in_progress | OPEN | capability shipped (submodule c5577e7 IDE starts its own server; :7090 live GET / -> 200) but the lanes->cost user check is not recorded |
| revive-epic-1792151796 | 2026-10-19 | pending | UNKNOWN | searched docs/epics, docs/todos and the 1136-commit log for 1792151796 / pick-files-directories epic; no revive record |
| gui-verify | 2026-10-19 | pending | OPEN | orch page ships (mini_ork/ide_pages/orch.py, 14f6bd962e streams it) but no user replay of the Orchestrator tab is recorded |
| rp-c3-triage-truth | 2026-10-18 | failed | DONE | 18ffcae7 "feat(run-page): one truthful status - triage names the node that failed (C3)"; tests/unit/test_ide_pages_run_triage_truth.py at HEAD |
| gui-replay | 2026-10-18 | pending | OPEN | no replay record; :7090 server live (pid 24420) and the app is running (pid 388) |
| acp-trio-decision | 2026-10-18 | pending | OPEN | tests/unit/test_acp_orchestrator.py:128-156 still holds the three lane assertions; no decision doc |
| build-launch-gui | 2026-10-17 | pending | DONE | target/mini-ork IDE.app/Contents/MacOS/zed (451.8 MB, 2026-10-29) and the app is running (pid 388) |
| verify-boundary | 2026-10-17 | in_progress | OPEN | no boundary decision recorded; pages live in mini_ork/ide_pages AND in crates/mini_ork_ui |
| rp-c3-salvage | 2026-10-17 | pending | STALE | salvage 6087a708 is not an ancestor of HEAD, but its content landed as 18ffcae7; no restart needed |
| node-crumb-visual | 2026-10-17 | in_progress | DONE | submodule 1320825 "Turn the node inspector's run/node text into a breadcrumb", pinned by fork 630318bba3 |
| design-parity-decision | 2026-10-17 | pending | OPEN | no parity decision recorded; SESSION-GOALS.md:132 still lists it pending |
| abstention-fold | 2026-10-17 | pending | DONE | one mapping now covers both halves: `failure_kind: toolchain` and `status: unverified|abstain` both resolve to ABSTAIN_RC in execute._run_verifier_ref, documented in docs/architecture/verification-stack.md |
| server-relaunch-7090 | 2026-10-17 | pending | DONE | `mini-ork serve --port 7090 --home .../researcher/.mini-ork` live (pid 24420); GET / -> 200 |
| sh-orphan | 2026-10-17 | in_progress | DONE | c7a84dc5 "point the drivers' prose at the Python driver, not the retiring .sh" + b1c8e475 "delete the retired shell driver" |
| r5-unprobeable-gate | 2026-10-16 | pending | DONE | mini_ork/specdir/lint.py:152 flags an all-unprobeable AC at lint time |
| classify-spawn-refine | 2026-10-16 | pending | OPEN | mini_ork/verify/pm.py:182 classify_spawn still carries the unreviewed pattern list; no refinement record |
| p1-2-abstention-contracts | 2026-10-16 | pending | DONE | the envelope decides, not the rc: `status: unverified|abstain` → ABSTAIN_RC whatever the exit code, and identically whether the evidence parsed strictly or tolerantly (execute._run_verifier_ref + docs/architecture/verification-stack.md, "The canonical abstain contract") |
| fix-mistagged-preconditions | 2026-10-16 | pending | OPEN | tests/unit/test_classify.py absent; docs/audits/2026-10-16-session-gaps/cb737d8d-sdd-run-revive.md:33 "NOT STARTED" |
| part-1-promote-why | 2026-10-16 | pending | DONE | why_section at mini_ork/ide_pages/run.py:3745, rendered at :2074 as "Why? - evidence" |
| part-3-empties | 2026-10-16 | pending | STALE | counts landed (7d0ac72a "tab badges (C9+C12)"), but empty tabs are deliberately kept with count 0 - run.py:4062 says so |
| part-2-diagnostics | 2026-10-16 | pending | DONE | diagnostics_sections (mini_ork/ide_pages/run.py:3837) merged into one section at :4753 |
| zoom-gui-smoke | 2026-10-16 | in_progress | UNKNOWN | no per-surface GUI smoke record found; app is running (pid 388) |
| crate-build-blocked | 2026-10-16 | pending | STALE | crate blocked no longer: 22 modules in crates/mini_ork_ui/src, fork pins 38a1f7a and built the app on 2026-10-29 |
| expand-pointer-answers | 2026-10-16 | pending | UNKNOWN | no doc expands B1/C1/C9/D6/0.1 from 02-05/10; the later 2026-10-17 FE brief does not carry those ids |
| verify-build | 2026-10-16 | pending | STALE | superseded: ui-check/clippy/build succeeded repeatedly afterwards; bundle built 2026-10-29 |
| zoom-unit-tests | 2026-10-16 | pending | DONE | graph.rs:3493-3565 tests clamp_zoom, step_zoom, box_fit_zoom and anchored_offset |
| gui-smoke | 2026-10-16 | pending | UNKNOWN | no HID-driven smoke record found |
| part-4-flow-map | 2026-10-16 | pending | DONE | flow_zoom (page.rs:186) + flow_full (:190); zoom_controls and fit at flow.rs:1449-1503 |
| list-zoom | 2026-10-16 | pending | DONE | graph.rs render_list: `let k = self.zoom` scales every literal (row, gaps, fonts, widths) |
| zoom-cluster | 2026-10-16 | pending | DONE | one shared zoom_controls (graph.rs:636) called from graph.rs:1299, graph.rs:3192 (tab_mode), flow.rs:1484, page.rs:2349 |
| verify-zoom | 2026-10-16 | pending | STALE | superseded: the crate shipped and the app was built (target/mini-ork IDE.app) |
| zoom-core | 2026-10-16 | pending | DONE | graph.rs:405-536 ZOOM_MIN/MAX/STEP/WHEEL + clamp_zoom/step_zoom/box_fit_zoom/anchored_offset |
| ws-1-stale-orch-tests | 2026-10-16 | pending | DONE | ae1307d9 "Refresh the orchestrator page tests to the shapes the redesign shipped" |
| live-replay-gui | 2026-10-16 | pending | UNKNOWN | no GUI half of the live replay recorded |
| sdd-crate-run | 2026-10-16 | in_progress | OPEN | spec exists (zed docs/specs/2026-10-16-run-initiation/crate/pick-files-directories.md) but store.rs:707 still passes directories: false |
| linear-jira-ui | 2026-10-16 | pending | DONE | c83767b8 "Add a Sources pane and secret-only writes for Linear and Jira" |
| gui-loadmore-smoke | 2026-10-16 | pending | OPEN | Load more exists (sidebar.rs:1003-1075) but the open/click/withdraw smoke is not recorded |
| f1-dir-picker | 2026-10-16 | pending | OPEN | crates/mini_ork_ui/src/store.rs:707 prompt_for_paths hardcodes directories: false; no `directories` flag read |
| b6-codefix-match | 2026-10-16 | pending | DONE | recipes/code-fix/task_class.yaml path_globs "**/*" + typed classify in mini_ork/dispatch/failure_class.py:161 |
| orch-gui-verify | 2026-10-16 | in_progress | OPEN | no confirmation of a held-open turn rendering session/update; the tab streams ACP (14f6bd962e) |
| gui-task-queue | 2026-10-15 | pending | OPEN | screenshot pass not recorded; task page itself ships (66fb07bb) |
| cn-ui-seen-before | 2026-10-15 | in_progress | DONE | 8123e49e/bcffd048 ContextNest tiers + cached IDE page; cn_session page registered in ide_pages PAGES |
| cn-ui-board-badges | 2026-10-15 | in_progress | DONE | f7b45c84 "badge Board cards with the asks and overlaps that name them" (kanban.py:103-134) |
| cn-issues | 2026-10-15 | pending | UNKNOWN | no issue-tracker artifacts (six prerequisite issues) found in either repo |
| p0-tests | 2026-10-15 | pending | DONE | tests/unit/test_ide_pages_orch_attention.py + test_ide_pages_orch_sections.py exist with P0 assertions |
| p4-attention | 2026-10-15 | pending | DONE | 5bf2ea5b + 93d9c696; board_cmd.py:1636 calls orch.attention prepended to every tab; test_ide_pages_orch_attention.py |
| cn-ui-needs-you | 2026-10-15 | in_progress | DONE | inbox.py:800 "inbox-needs-you" section and :1122 needs-you chip; roadmap id cn-ui-needs-you |
| intake-run-id | 2026-10-15 | pending | DONE | mini_ork/threads/store.py INTAKE_COLUMNS adds run_id (+mandate_json) and _ensure_intake_columns applies it |
| board-answer-verb | 2026-10-15 | pending | DONE | mini_ork/cli/board_cmd.py answer verb at :1528 (and :1599) |
| board-start-verb | 2026-10-15 | pending | DONE | mini_ork/cli/board_cmd.py start verb at :1538 (and :1619) |
| sync-ui-gitlink | 2026-10-15 | pending | DONE | fork 1ced93d437 "pin mini_ork_ui at 46dda68 (MINI_ORK_IDE_OPEN carries page args)" |
| load-more-gui-smoke | 2026-10-15 | failed | OPEN | control implemented (sidebar.rs:309-1075; submodule 2717205/70651b8) but the live smoke failed and was not re-run |
| mini-ork-build | 2026-10-15 | in_progress | DONE | target/mini-ork IDE.app/Contents/MacOS/zed built 2026-10-29, 451.8 MB |
| cn-ui-tiers-b | 2026-10-15 | in_progress | DONE | _cn.py:57/135 secure-tier wrapper fails closed naming only the key; 8123e49e tiers |
| orch-spine-mockups | 2026-10-15 | pending | DONE | docs/ux-briefs/2026-10-15-orchestrator-intake.md + 2026-10-15-orchestrator-intake-redesign.md carry the ASCII/mermaid spines |
| orch-screenshots | 2026-10-15 | pending | OPEN | only assets/mini-ork-ide.png hero exists; no populated/zero-state orch-page screenshots in either repo |
| orch-ux-brief | 2026-10-15 | in_progress | DONE | docs/ux-briefs/2026-10-15-orchestrator-intake.md (370+ lines) |
| rebuild | 2026-10-15 | in_progress | DONE | bundle rebuilt 2026-10-29 and the app is running (pid 388) |
| miniork-s7-probe-fixture-guard | 2026-10-15 | pending | DONE | frozen-path refusal at land_patch (mini_ork/cli/apply.py:740-797) + tests/unit/test_frozen_paths_enforced.py |
| triage-doc-s5-correction | 2026-10-15 | pending | UNKNOWN | searched docs/audits and docs/todos for a Section 5 severity correction; not located |
| ide-rebuild | 2026-10-15 | in_progress | DONE | bundle present; palette commit 919178b pinned by fee632c57d |
| python-repo-ui-pin | 2026-10-14 | pending | DONE | fork 0a60d84dc5 "pin mini_ork_ui to 7e90bbd (activity rail right of the board)" |
| board-task-page | 2026-10-14 | pending | DONE | 66fb07bb "Give every Board card its own task page" |
| threads-visible | 2026-10-13 | in_progress | DONE | submodule 5187eab "List the project's Zed agent threads on the board", pinned by 2ffb47a0 |
| board-card-retry | 2026-10-13 | pending | OPEN | no per-card Retry action on Needs-you cards in kanban.py (only a retry-schema reference at :1129) |
| synth-verify-python3 | 2026-10-13 | pending | DONE `c2f807a1` | Fixed at the cause, not the line: the same pair was duplicated as a literal in `observability/bug_report.py:633` (the promoted-kickoff path), which is why only the file-hint path ever learned `sys.executable`. Both emitters now read `mini_ork/verify_commands.fallback_commands()`, built at call time so it names the interpreter that can import the package. The frozen-kernel golden interpolates the pair instead of pinning a host-specific literal. Pinned by `test_scheduler_py::test_no_hint_fallback_uses_the_running_interpreter` and the promoted-kickoff assertions in `test_bug_report_py` |
| reviewer-gate | 2026-10-13 | pending | UNKNOWN | no decision record for the superseded replay found in docs or the log |
| fix-researcher-eslint-project | 2026-10-13 | pending | STALE | researcher tsconfig include "**/*.ts" already covers server/** (out-of-repo check) |
| decide-61320 | 2026-10-13 | pending | STALE | the deliverable was re-implemented separately: bb258b91 Telegram, 991a8f59 Slack, 7d09f02c Discord |
| p12-ac2 | 2026-10-13 | in_progress | OPEN | relations/differential now run in code-fix shadow (`certify/codefix_terms.py`), not `certify`-only; the open half is the flip to `MO_CODEFIX_CERTIFY=gate`, blocked on 0 `certify_terms.json` reports |
| 51986-revise | 2026-10-12 | in_progress | UNKNOWN | no run dir matching 51986 in .mini-ork/runs; no completion receipt |
| published-chip | 2026-10-12 | pending | DONE `d62b8b81` | `ide_pages.run._delivery_chip` maps the delivery kind to a header chip — `committed <sha>` (green), `proposed diff` (blue), `worktree to merge` (yellow) — and the v2 + level-1 header both draw it, so every tab carries the fact the delivery bar only showed on its own tab. The kind comes from one function, `run_flow.delivery()`, which `_merge` now also reads (`_delivery`), so chip and bar cannot disagree. Asked only for a `published` run: finding the evidence may fall back to `git log` in the target repo, and a run that delivered nothing grows no chip. Pinned by `tests/unit/test_ide_pages_run_delivery_chip.py`; the UX plan's chip inventory names the three labels |
| sidebar-thread-list | 2026-10-12 | pending | DONE | submodule 5187eab thread list in the rail board (sidebar.rs), pinned 2ffb47a0 |
| intake-delegation-monitor | 2026-10-12 | in_progress | UNKNOWN | monitoring task; no end state recorded |
| epic-publisher-abstain | 2026-10-12 | pending | DONE | recipes/epic-runner/lib/epic_dispatcher.py:238/289/387 gates publish on MINI_ORK_EPIC_PUBLISH and appends --smoke-shape when off |
| wit-3-scope-allow | 2026-10-12 | pending | STALE | scope parsing rewritten (LF-R8, mini_ork/kickoff_scope.py) and the transports shipped in separate runs |
| wit-3-integrations | 2026-10-12 | in_progress | DONE | bb258b91 Telegram + 991a8f59 Slack + 7d09f02c Discord over the thread seam |
| wit-2-documents | 2026-10-12 | pending | DONE | 4598b66b read-only extractors; mini_ork/documents/{pdf,ooxml,tabular}.py + thread.py preview |
| wit-4-linear | 2026-10-12 | pending | DONE | 4379ff03 Linear adapter + 1be093e8 gated write-back |
| upgrade-publish-defect | 2026-10-11 | pending | UNKNOWN | no scripts/mini-ork-upgrade in the repo; searched the tree |
| attribution-epic-board | 2026-10-11 | in_progress | UNKNOWN | no attribution artifact for epic/board consolidation + app import found |
| step8-decision | 2026-10-11 | pending | DONE | secure-tier chat webhooks shipped (2d1852b2 POST /api/v1/chat/{telegram,slack,discord}) + config/chat.example.yaml secure keys |
| outcome-web-ui-btn | 2026-10-11 | pending | OPEN | the button is still present at mini_ork/ide_pages/outcome.py:819; no decision recorded |
| ide-verify | 2026-10-11 | pending | OPEN | build+run verified (app live) but the card fix / Questions-tab check is not recorded (Questions folded into DAG) |
| marker-removal | 2026-10-11 | pending | DONE | README rewritten (04a2bd4, 38a1f7a); grep finds no marker lines in crates/mini_ork_ui/README.md |
| R5-remote-nodes | 2026-10-11 | pending | DONE | docs/todos/20261011-1830-ui-submodule-build-requirements.md: R5 SATISFIED (tree_sync._materialise_submodules + tests) |
| harness-tab-rust | 2026-10-11 | pending | DONE | crates/mini_ork_ui/src/graph.rs:63 TABS is 9 entries incl. ("harness", "Harness") |
| harness-tests | 2026-10-11 | pending | DONE | tests/unit/test_ide_pages_node_harness.py holds 13 tests |
| ui-submodule-r5-remote-nodes | 2026-10-11 | pending | DONE | same receipt as R5: tree_sync materialises submodules; docs record SATISFIED |
| keep-or-delete-fork | 2026-10-11 | pending | STALE | the fork is alive and shipping (commits to 2026-10-29 inclusive) |
| board-row-separator | 2026-10-11 | in_progress | DONE | crates/mini_ork_ui/src/sidebar.rs:1348 bottom-border row separator |
| inbox-tests | 2026-10-11 | pending | DONE | e204cc7d "tests: catch the ide_pages ... tests up to the committed behaviour" (+ d7450c87) |
| stage-0-thread-spine | 2026-10-11 | pending | DONE | 76051a32 "feat(threads): add the thread spine - migration, model and store"; mini_ork/threads/ |
| stage-3-comms | 2026-10-11 | pending | DONE | transports shipped (bb258b91/991a8f59/7d09f02c) + config/chat.example.yaml + threads serve |
| stage-2-documents | 2026-10-11 | pending | DONE | mini_ork/documents + attachment previews in threads (f4d5bb75, 012d3fe9) |
| wu-a41f8e07 | 2026-10-11 | in_progress | DONE | docs/epics/roadmap-work-intake-delegation.md + roadmap-contextnest-ide.md (9ec0b70c, 16-epic roadmap) |
| stage-2-files | 2026-10-11 | pending | DONE | mini_ork/documents package + f4d5bb75 threads keep and preview attachments |
| enable-v2-injection | 2026-10-11 | pending | OPEN | MO_CONTEXT_V2 defaults to `shadow` (mini_ork/context_v2.py:27/91); no config sets it on |
| restart-7090 | 2026-10-11 | pending | DONE | :7090 server live (pid 24420 `mini-ork serve --port 7090`), GET / -> 200 |
| pm-stage-1 | 2026-10-11 | pending | OPEN | `mini-ork pm` CLI ships (mini_ork/cli/pm.py build_parser) but ide_pages PAGES has no `issues` page |
| design-copy-on-build | 2026-10-11 | in_progress | STALE | superseded by submodule vendoring (e21d015d15 / 31171e9741); no copy-in step needed |
| write-plan | 2026-10-11 | pending | UNKNOWN | no identifiable plan artifact for this id |
| board-features | 2026-10-11 | pending | OPEN | kanban.py has _CARD_CAP/_fair_share but no filter box, column totals or "N more" control |
| inbox-cheap-fixes | 2026-10-11 | pending | DONE | per-row landed/repairing pass at inbox.py:377-390; every list section carries full=True |
| reflect-timeout-shape | 2026-10-11 | pending | DONE | the window is already capped by *work* (`_capped_until`, one batch of traces), and a pass that hits `deadline_s` now halves that cap (state key `batch`, doubled back on success) instead of retrying the same oversized window forever |
| ra-scan-exclusions | 2026-10-11 | pending | OPEN | crates/.../.zed/settings.json file_scan_exclusions still lists only crates/agent fixtures; no ui/ or target/ exclusion |
| reflect-truncation-confirm | 2026-10-11 | pending | UNKNOWN | no side-channel references in mini_ork/learning; the researcher-home observation is not recorded |
| confirm-live-kill | 2026-10-11 | pending | OPEN | user-side ram-sentinel observation not recorded |
| projects-registry-model | 2026-10-11 | pending | DONE | crates/mini_ork_ui/src/projects.rs roots_of/write_registry/registry_remove/forget/open_or_init |
| board-pwd-fix | 2026-10-11 | pending | DONE | crates/mini_ork_ui/src/board.rs:204 and :391 `.env("PWD", project)` |
| store-drop-implicit-add | 2026-10-11 | pending | DONE | store.rs has no remember_home; the only registry write is projects.rs:257 switch_to, documented as the single gesture |
| title-bar-menu | 2026-10-11 | pending | DONE | fork 8f79c5d949 "manage mini-ork projects from the title bar menu" (title_bar.rs) |
| sidebar-parity | 2026-10-11 | pending | DONE | projects.rs ProjectEntry is documented as the one row both the title-bar menu and the sidebar render |
| user-verify | 2026-10-11 | pending | OPEN | no user confirmation of all three changes recorded |
| build-bundle | 2026-10-11 | in_progress | DONE | signed bundle: target/mini-ork IDE.app with Contents/_CodeSignature and Contents/MacOS/zed |
| full-width-pages | 2026-10-11 | in_progress | DONE | every section is w_full with no centring clamp (page.rs:946/984); bbb00e1 full-width sections carry an id |
| fix-fixture-leak-tests | 2026-10-10 | pending | UNKNOWN | tests/unit/test_tier2_scoped_unit_runner.py has root_config fixtures; current pass state not checked (read-only audit) |
| add-gitleaks-gate | 2026-10-10 | pending | DONE | `b70b4690` adds the pinned-gitleaks `secret-scan` job to ci.yml (a `needs:` dep of `ci result` AND in its `expected` set); `c089a1ee` closes the allowlist half — the fixture exemption was path-scoped, and gitleaks allows a finding when ANY condition matches, so `paths = ['^tests/.*']` exempted every credential under tests/ |
| stop-openbao-container | 2026-10-10 | pending | DONE | `docker ps -a` shows no openbao/vault container |
| openbao-secure-tier | 2026-10-10 | in_progress | DONE | mini_ork/dispatch/vault.py openbao backend (:319-363) + vault_write (:54-90); config/vault.example.yaml |
| zed-bridge | 2026-10-10 | pending | DONE | P1-P4 all present: mcp launch/proxy (mcp_cmd.py), zed setup (zed_cmd.py), mcp registry with vault expansion |
| p3-mcp-proxy | 2026-10-10 | pending | DONE | mini_ork/cli/mcp_cmd.py:283 proxy_server |
| p1-mcp-launch | 2026-10-10 | pending | DONE | mini_ork/cli/mcp_cmd.py:312 launch_server |
| p2-zed-setup | 2026-10-10 | pending | DONE | mini_ork/cli/zed_cmd.py:185 _build_mcp_context_entry + :201 _stdio_mcp_names |
| mcp-registry-auth | 2026-10-10 | pending | DONE | mcp_cmd.py load_registry + vault_environment expansion (:79-88, :244-277) |
| speed-up-rebuild | 2026-10-10 | in_progress | DONE | script/mini-ork-build bypasses sccache, sets QoS, jobs cap and ld64.lld |
| tcc-grant | 2026-10-10 | in_progress | OPEN | the signing identity that preserves TCC is implemented (script/mini-ork-app:69) but the grant itself is a System Settings action, unrecorded |
| launch-verify | 2026-10-10 | pending | OPEN | user-side open-time comparison vs stock Zed not recorded |
| verify-full-zed-link | 2026-10-10 | pending | DONE | script/mini-ork-build:51-53 forces ld64.lld; a full 451.8 MB zed binary is linked in the bundle |

## Still genuinely open

- **journey-scale** — Scale corpus to 200+ journeys  
  next: 15 journeys in docs/user-journeys/journeys/*.yaml; docs/user-journeys/README.md:131 still names the 200-journey floor
- **user-relaunch-verify** — User opens lanes -> cost; confirm IDE-spawned serve  
  next: capability shipped (submodule c5577e7 IDE starts its own server; :7090 live GET / -> 200) but the lanes->cost user check is not recorded
- **gui-verify** — User replays the Orchestrator tab in the rebuilt GUI  
  next: orch page ships (mini_ork/ide_pages/orch.py, 14f6bd962e streams it) but no user replay of the Orchestrator tab is recorded
- **gui-replay** — GUI half of the live replay on the orch tabs  
  next: no replay record; :7090 server live (pid 24420) and the app is running (pid 388)
- **acp-trio-decision** — Decide whether to refresh the three ACP assertions  
  next: tests/unit/test_acp_orchestrator.py:128-156 still holds the three lane assertions; no decision doc
- **verify-boundary** — Choose core-data vs ide_pages vs Rust FE boundary  
  next: no boundary decision recorded; pages live in mini_ork/ide_pages AND in crates/mini_ork_ui
- **design-parity-decision** — Decide target level and which safety surfaces must survive exact parity  
  next: no parity decision recorded; SESSION-GOALS.md:132 still lists it pending
- **classify-spawn-refine** — User refines classify_spawn's pattern list: which rc/pattern means the tool never started  
  next: mini_ork/verify/pm.py:182 classify_spawn still carries the unreviewed pattern list; no refinement record
- **fix-mistagged-preconditions** — Fix the two precondition ACs citing the nonexistent tests/unit/test_classify.py and dedupe codefix AC1/AC8  
  next: tests/unit/test_classify.py absent; docs/audits/2026-10-16-session-gaps/cb737d8d-sdd-run-revive.md:33 "NOT STARTED"
- **sdd-crate-run** — Deliver pick-files-directories via SDD  
  next: spec exists (zed docs/specs/2026-10-16-run-initiation/crate/pick-files-directories.md) but store.rs:707 still passes directories: false
- **gui-loadmore-smoke** — GUI live smoke of the Load more control (open /tmp/mo-smoke50, click last child, watch spinner then withdrawal)  
  next: Load more exists (sidebar.rs:1003-1075) but the open/click/withdraw smoke is not recorded
- **f1-dir-picker** — S.pick_files directories flag → store.rs prompt_for_paths  
  next: crates/mini_ork_ui/src/store.rs:707 prompt_for_paths hardcodes directories: false; no `directories` flag read
- **orch-gui-verify** — Launch the IDE and confirm a held-open turn renders session/update  
  next: no confirmation of a held-open turn rendering session/update; the tab streams ACP (14f6bd962e)
- **gui-task-queue** — Screenshot pass of new pages and the New task -> /task -> Queue check  
  next: screenshot pass not recorded; task page itself ships (66fb07bb)
- **load-more-gui-smoke** — Live-smoke the Load more control's render, spinner and withdrawal in the IDE  
  next: control implemented (sidebar.rs:309-1075; submodule 2717205/70651b8) but the live smoke failed and was not re-run
- **orch-screenshots** — Capture populated + zero-state screenshots of the orch page  
  next: only assets/mini-ork-ide.png hero exists; no populated/zero-state orch-page screenshots in either repo
- **board-card-retry** — Phase 2: per-card Retry on Needs-you cards (board verb + crate change)  
  next: no per-card Retry action on Needs-you cards in kanban.py (only a retry-schema reference at :1129)
- ~~**synth-verify-python3**~~ — **DONE `c2f807a1`**: both kickoff emitters read one
  `fallback_commands()` definition, so the no-hint epic path and the bug-promoted path
  can no longer disagree about which interpreter to name.
- **p12-ac2** — Finish P1.2 full-suite AC2 differential  
  next: the premise moved — relations/differential are no longer `certify`-only: `mini_ork/certify/codefix_terms.py` runs both veto terms on every green code-fix replay (shadow by default, `MO_CODEFIX_CERTIFY=gate` for the level downgrade). What remains is the flip to `gate`, and it is blocked on evidence: 0 `certify_terms.json` reports have been written on this machine, so no run has yet shown the terms measuring anything
- ~~**published-chip**~~ — **DONE `d62b8b81`**: `committed <sha>` / `proposed diff` /
  `worktree to merge` in the header of a published run, read from the same
  `run_flow.delivery()` the delivery bar uses.
- **outcome-web-ui-btn** — Decide whether the outcome menu keeps its 'Open in web UI' button (outcome.py:350)  
  next: the button is still present at mini_ork/ide_pages/outcome.py:819; no decision recorded
- **ide-verify** — build zed-mini-ork and verify the card fix + Questions tab in the IDE  
  next: build+run verified (app live) but the card fix / Questions-tab check is not recorded (Questions folded into DAG)
- **enable-v2-injection** — Decide whether to turn MO_CONTEXT_V2 on so verified themes inject  
  next: MO_CONTEXT_V2 defaults to `shadow` (mini_ork/context_v2.py:27/91); no config sets it on
- **pm-stage-1** — issues IDE page + mini-ork pm CLI command  
  next: `mini-ork pm` CLI ships (mini_ork/cli/pm.py build_parser) but ide_pages PAGES has no `issues` page
- **board-features** — Filter box, card quick actions, column totals + 'N more', fit-the-pane live refresh  
  next: kanban.py has _CARD_CAP/_fair_share but no filter box, column totals or "N more" control
- **ra-scan-exclusions** — Exclude ui/ and target/ from rust-analyzer to stop the 6.5 GB scans  
  next: crates/.../.zed/settings.json file_scan_exclusions still lists only crates/agent fixtures; no ui/ or target/ exclusion
- **confirm-live-kill** — User relaunches app from terminal with stderr tee'd and watches ram-sentinel log while clicking a changed file  
  next: user-side ram-sentinel observation not recorded
- **user-verify** — User relaunches and checks all three changes  
  next: no user confirmation of all three changes recorded
- ~~**add-gitleaks-gate**~~ — DONE 2026-11-08: `b70b4690` (job + two-tier docs) and
  `c089a1ee` (value-scoped allowlists; the path-scoped fixture exemption was the hole).
- **tcc-grant** — grant dev.mini-ork.ide Removable Volumes access  
  next: the signing identity that preserves TCC is implemented (script/mini-ork-app:69) but the grant itself is a System Settings action, unrecorded
- **launch-verify** — user relaunches and compares open time vs stock Zed  
  next: user-side open-time comparison vs stock Zed not recorded

## Closed since the audit

- **reflect-timeout-shape** — **DONE**: the bound never needed to be derived from home size, because the window is capped by *work*: `_capped_until` narrows `until` to one batch of traces (`MO_REFLECTION_BATCH`, default 500), so a large home drains one batch per tick instead of one whole window. What was missing is the other half — a batch too slow to finish inside `deadline_s` was retried from the same `since` on every tick, re-paying for the same traces with the watermark frozen. `run_once` now halves the cap on the deadline's 124 and doubles it back toward the ceiling on a clean finish (persisted as the state file's `batch` key, written only while it differs from the ceiling), so the schedule converges on the largest batch that fits its deadline. Both ends of the cap agree: `_capped_until` takes it, and `MO_REFLECTION_BATCH` is set for the pass so the pipeline selects it. Floor is one trace per pass — a single trace slower than the deadline is a config problem (`deadline_s`), now documented as such in `docs/operator/reflect-schedule.md`. Covered by six cases in `tests/unit/test_reflect_schedule.py`.
- **threads-builder** — **DONE**: `mini_ork/threads/cli.py` has `build_parser()`, and `main()` parses through it rather than hand-dispatching, so the tree *is* the command's definition instead of a second copy of it. Six subparsers (`list`, `show`, `send`, `continue`, `attach`, `serve`), the `VERBS` tuple and the `_HANDLERS` table must agree, and every verb accepts the `--home` the IDE appends. The point beyond tidiness: `tests/unit/test_ide_pages_actions.py` resolves a page action's parser by subcommand module, and `threads` was the one subcommand it could not see — so the composer's `send` and the picker's `attach` argv were unchecked. `tests/unit/test_threads_cli_parser.py` now parses those exact argv.
- **p1-2-abstention-contracts** — **DONE**: the executor now reads the canonical abstain contract off the ENVELOPE, not the exit code. `status: unverified|abstain` → `ABSTAIN_RC` (3) whether the verifier exited 0 or 1, and identically whether the evidence parsed strictly or through the tolerant path — the split that made the same verdict green a node on one run and fail it on the next. `status: skipped` is deliberately excluded (no subject ≠ no verdict). Contract recorded in `docs/architecture/verification-stack.md`; covered by `tests/unit/test_verifier_envelope_honoured.py`.
- **abstention-fold** — **DONE** with the row above: one mapping in `execute._run_verifier_ref` covers `failure_kind: toolchain` and `status: unverified`, so P0 4d and P1.2 no longer have two competing abstain stories.
