## Evidence Read

Commands run and files/materialized paths read for this lens:

- `git -C $REF ls-tree --name-only HEAD extensions/` (extensions are tree-only, materialized via `git show HEAD:<path>`).
- `git -C $REF ls-tree --name-only HEAD skills/` (50 skills enumerated).
- `git -C $REF ls-tree --name-only HEAD packages/` (28 packages enumerated).
- `git -C $REF ls-tree --name-only HEAD docs/channels/` (50+ channel docs enumerated).
- `git -C $REF ls-tree --name-only HEAD docs/concepts/ docs/nodes/ docs/platforms/ docs/plugins/`.
- Read: `README.md:1-75, 75-118`, `VISION.md` (full).
- Read: `docs/concepts/multi-agent.md` (full), `docs/concepts/parallel-specialist-lanes.md` (full), `docs/concepts/models.md` (1-100), `docs/concepts/model-failover.md` (1-80), `docs/concepts/memory.md` (1-80), `docs/concepts/dreaming.md` (1-80), `docs/concepts/queue-steering.md` (1-60), `docs/concepts/memory-builtin.md` (1-100), `docs/concepts/agent-runtimes.md` (1-120), `docs/concepts/architecture.md` (1-80), `docs/channels/pairing.md` (full), `docs/automation/hooks.md` (1-80), `docs/platforms/index.md` (full).
- `git -C ... show HEAD:extensions/codex/README.md` (codex harness).
- `git -C ... show HEAD:extensions/cua-computer/package.json` (computer-use agent).
- Target tree (read-only, $MO_TARGET_CWD = `$MO_TARGET_CWD`):
  - `mini_ork/__init__.py`, `mini_ork/lane_router.py` (1-50), `mini_ork/dispatch/` listing, `mini_ork/orchestration/` listing, `mini_ork/mcp_context/server.py` (1-50), `mini_ork/integrations/` listing (discord/slack/telegram), `mini_ork/ide_pages/` listing (35 files), `mini_ork/ide_pages/kanban.py` (1-40), `mini_ork/ide_pages/inbox.py` (1-30), `mini_ork/gates/` listing (30 files), `mini_ork/memory/` listing, `mini_ork/learning/` listing.
  - `docs/architecture/verification-stack.md` (1-60), `docs/architecture/artifact-graph.md` (1-60), `docs/PYTHON-SDK.md` (1-60).
  - `recipes/` (50 recipe dirs enumerated), `recipes/star-growth-panel/` for the panel under analysis.

### Cross-Candidate Observations

Two structural facts about OpenClaw drive most of the "wait, it can do that?" moments I will propose:

1. **Composition is the headline, not the parts.** OpenClaw's stars (392k) are not earned by any single capability — no individual extension is novel — but by *what they add up to*: a personal-assistant surface that is simultaneously on 50 messaging channels, on macOS/iOS/Android/Windows/Linux, in a CLI, a TUI, a Web UI, an ACP harness adapter, and a Codex harness bridge. The first-minute demo is rarely "look, a Slack channel"; it is "look, I just told my Mac in iMessage to fix the test, my Discord picked up the failure, my iOS node is showing me the trace, and my laptop is steering the running tool." mini-ork already has heterogeneous dispatch and an IDE; the missing piece is the *visible composition* — the operator watching lanes and devices coordinate, not just reading a log.

2. **Trust and capability are co-shipped.** Pairing codes, sandboxing, owner allowlists, command approval, DM policy, and the "internal hooks are trusted code, not sandboxed scripts" warning are presented *as* the capability, not as a separate compliance section. The "wait, it can do that?" moment is often paired with "and I know it can't do the unsafe thing." mini-ork has none of the operator-facing trust UX; that absence is the single largest gap between mini-ork's actual capability surface and what the demo conveys.

The eight candidates below translate OpenClaw's mechanisms into mini-ork's domain, ranked by star impact per unit of effort.

## Candidate Features

### K1 — "Spawn a research team in one command"

- evidence: `docs/concepts/multi-agent.md:117-183` ("Team preset") — `openclaw agents team create --non-interactive` ships a chief-of-staff + researcher + writer + reviewer preset, each with its own workspace, AGENTS.md contract, and handoff rule. The preset wires `agents.entries.<id>.subagents.allowAgents` and `delegationMode: "prefer"` (lines 142-168). `docs/concepts/parallel-specialist-lanes.md:34-100` describes the rollout: lane contracts + chat budget + handoff + tool posture, in that order. README line 18 ("One Gateway runs it as a personal assistant on a laptop or as a shared team deployment") carries the same message to the homepage.
- what_openclaw_does: A single command materialises an org-chart of agents with workspaces, identities, and a written delegation contract. Specialists return artifacts to a coordinator without re-delegating. Lane contracts (`AGENTS.md`) cap the tool surface each role uses. The visual demo in the Control UI ("Agents at `/agents`, current work status, recent chat previews") lets the operator *see* the team run.
- why_it_drives_stars: The single most cargo-cult-worthy line in OpenClaw is "I made a team in one command and they wrote me a launch plan while I watched." mini-ork's existing recipe DAGs are technically equivalent (researcher + implementer + verifier + auditor are real node kinds today) but the *visible product* is a long sequence of node completion rows in a log. The demo retells itself because "team preset" + "watch the specialist run while you read its handoff" is concrete and screenshot-able.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: Theatre if the team preset ships as a config file with no visible state. The demo fails the moment the operator opens the IDE and sees one run card, not four. The first step must be an IDE panel showing live "team" state (coordinator + specialists + last handoff), not a CLI flag.
- first_step: Add `recipes/team-presets/` with `research-team.yaml`/`launch-team.yaml`/`audit-team.yaml` that wire existing node kinds (researcher → researcher-implementer → reviewer → implementer with handoff contracts in `AGENTS.md`-equivalent kickoff blocks). Surface the team in `mini_ork/ide_pages/kanban.py` as a single "team" card that fans out into live per-specialist cards in the runs pane — a path mini-ork already partial-renders today.

### K2 — "It remembers and consolidates while you sleep"

- evidence: `docs/concepts/memory.md:21-62` (USER.md / MEMORY.md / `memory/YYYY-MM-DD.md` / DREAMS.md layers, with provenance classification); `docs/concepts/dreaming.md:11-80` (background "Dreaming" sweeps with light → REM → deep phases, write `MEMORY.md` only in the deep phase, with a `DREAMS.md` diary); `docs/concepts/memory-builtin.md` (SQLite FTS5 + sqlite-vec hybrid search, separate read-only worker so a slow query cannot block the Gateway).
- what_openclaw_does: The agent writes daily notes; a background "Dreaming" sweep promotes durable candidates into a curated `MEMORY.md` and a human-reviewable `DREAMS.md`. The user sees a long-lived persona that picks up yesterday's preferences on a fresh session, and the operator can read the diary.
- why_it_drives_stars: "Tell it once, it remembers, and it tells you why it remembered" is the canonical personal-assistant promise. mini-ork already has *verified-outcome memory* (`mini_ork/memory/`) — a much stronger epistemic primitive — but nothing that surfaces a long-lived persona across sessions. The demo fails today: the second run on the same project starts cold. The capability that "already exists but is invisible" here is mini-ork's biggest lift opportunity.
- mini_ork_fit: high
- star_impact: high
- effort: M
- risk: Mining runs for "preferences" sounds like a personal-assistant feature, which is out of scope per the synthesis guardrail. Constrain it to **project-scoped** notes (this repo, this team, this recipe pattern) and **operator-curated** promotion (the operator decides what survives the deep sweep). That preserves the "long-lived persona" feel without drifting toward personal-assistant territory.
- first_step: Add `mini_ork/memory/diary/` — a background recipe (cron or recipe-loop) that reads the run history of a project, writes `<project>/.mini-ork/diary/YYYY-MM-DD.md`, and lets the operator approve promotion into `<project>/.mini-ork/MEMORY.md`. Wire the next-session bootstrap to read `MEMORY.md` for project-scoped context the way it reads `MEMORY.md` today. Land it behind a `MO_DIARY=1` env gate first so the simple tier keeps zero behaviour change.

### K3 — "It's everywhere you already are"

- evidence: `README.md:18` ("Discord, iMessage, Slack, Teams, Telegram, WhatsApp, and 20+ more"); `docs/channels/` enumerates 50+ channel guides (discord, telegram, slack, whatsapp, signal, matrix, msteams, imessage, line, mattermost, googlechat, nextcloud-talk, nostr, irc, sms, twitch, qqbot, synology-chat, feishu, wecom, x, yuanbao, zalo, zaloclawbot, zalouser, bluebubbles); `docs/concepts/multi-agent.md:354-358` (multi-account per channel).
- what_openclaw_does: The same Gateway reaches the operator wherever they already are. Inbox-style "pairing codes" prevent unsolicited inbound; multi-account lets one bot on each surface.
- why_it_drives_stars: "It's in Slack, Discord, Telegram, WhatsApp, Signal, iMessage, and the matrix room with my team, and the same handoff works across all of them" is the omnipresence reaction. mini-ork ships three (`mini_ork/integrations/{discord,slack,telegram}.py`); the *visibility* of that surface is low. The demo retells because "I told Slack, Discord replied with the trace" is a single screenshot.
- mini_ork_fit: medium
- star_impact: high
- effort: M
- risk: Each channel adapter is real engineering; trying to reach 50 channels in one cycle produces shallow adapters that break. The star impact comes from "the list of channels the operator can pick from," not from any single one. Prioritise: ship a thin `ChannelAdapter` protocol (`mini_ork/integrations/_base.py`), add GitHub Issues + Linear + Jira adapters (where mini-ork already has PM mirrors), then revisit chat channels.
- first_step: Add GitHub Issues + Linear + Jira chat-like adapters under `mini_ork/integrations/` so a recipe's `awaiting input` prompt can be answered from the operator's ticket tracker. Document them in `docs/architecture/integrations.md` next to the existing three. This is the cheapest path to the omnipresence demo without a chat-surface rebuild.

### K4 — "Swap the brain mid-flight"

- evidence: `docs/concepts/agent-runtimes.md:11-44` (provider vs model vs runtime vs channel — "the `claude-cli` runtime is not an embedded harness id"); `docs/concepts/model-failover.md:11-50` (auth-profile rotation within provider, then model fallback, then bounded same-model recovery; `Retry-After` honored); `docs/concepts/models.md:11-44` (selection order: primary → fallbacks → auth failover).
- what_openclaw_does: The operator picks the model in one config knob; the runtime harness is a separate knob (`openclaw`, `codex`, `copilot`, `muse`, `claude-cli`); fallbacks are declared, not coded; the runtime recovers the partial transcript on rate-limit rather than replaying.
- why_it_drives_stars: "It just kept going when the API rate-limited, and I can switch from Codex to Claude-CLI without changing the recipe" is the model-swapping demo. mini-ork already has heterogeneous lanes (`mini_ork/dispatch/llm_dispatch.py`, `MO_ROUTING_POLICY`) and cost governance (`mini_ork/dispatch/cost_pause.py`), but the swap is *operator-invisible* — it lives in env vars and lane metrics.
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: Theatre if the swap is a config edit; the demo must show it happening *during a run*. Also: OpenClaw's five runtimes are partly out of scope (Codex harness, Copilot plugin, Muse, Claude-CLI). Translate the *mechanism* — runtime + auth rotation + transcript preservation — without copying the runtimes. mini-ork can declare its own runtimes (`mini_ork`, `claude-code`, `codex-cli`, `gemini-cli`) and use the same fallback model.
- first_step: Add a "live lane pivot" verb to `mini_ork/ide_pages/run.py` that, for the running node, swaps `dispatch_model` lanes mid-flight using the partial-transcript preservation rule from `model-failover.md:11-26`. Surface the current lane + two fallbacks in the run header. Cost governance already exists; this exposes it.

### K5 — "You can interrupt it"

- evidence: `docs/concepts/queue-steering.md:11-50` ("Steering does not interrupt a tool call that is already running. An assistant message's tool calls are a committed plan until one of them starts executing"); the boundary is at the *runtime boundary*, after each tool settles; the runtime emits paired "Skipped" tool results so the model knows what was deferred.
- what_openclaw_does: An inbound message during an active run enters a "steer" queue and is delivered at the next safe tool boundary; the unstarted tail is skipped with synthetic "Skipped to process an incoming message." results. The operator sees the live agent pivot.
- why_it_drives_stars: "I yelled at it mid-thought and it changed course" is the canonical interactive-agent moment. mini-ork has a run, but no operator-facing queue and no "live steer" surface in the IDE. The recipe DAG waits for completion; there is no mid-run verb the operator can issue.
- mini_ork_fit: high
- star_impact: medium
- effort: M
- risk: Steering in OpenClaw is a runtime property, not a node property. mini-ork's recipe DAG treats each node as atomic; mid-node steering breaks the artifact-graph contract (`docs/architecture/artifact-graph.md:23-27` — "an edge is also a completion gate"). Limit steering to *between nodes*, not *within a node*: the operator types a directive, the current node finishes, the next node's kickoff is rewritten. That is a one-cycle win and survives the artifact contract.
- first_step: Add a "Steer" thread action to `mini_ork/ide_pages/thread.py` that appends a directive to the run, and modify `mini_ork/orchestration/runner_orchestrator.py` to inject the directive into the *next* node's kickoff rather than the current one. Document the limit explicitly: steering is between nodes, not within them.

### K6 — "Trust is built in: pairing + sandboxing as a capability"

- evidence: `docs/channels/pairing.md` (full) — 8-char pairing codes, 1-hour expiry, "DM pairing applies to channels that implement OpenClaw's pairing API", owner bootstrapping for first-time setups, "Approve from the Control UI" queue; `docs/automation/hooks.md:33-40` ("Internal hooks are trusted code, not sandboxed scripts. They run with the Gateway process's filesystem, network, and environment access"); `VISION.md:67-72` ("Security in OpenClaw is a deliberate tradeoff: strong defaults without killing capability").
- what_openclaw_does: Every channel has a pairing step. Unknown senders get a code and their message is **not processed** until approved. Hooks are explicitly trusted, not sandboxed — and that is itself called out as a feature.
- why_it_drives_stars: "It asks before it does the destructive thing, and the code has an expiry, and there is a queue in the UI" is the safety-as-a-product moment. mini-ork has *gates* (`mini_ork/gates/`: 30 files including hackability, suite-adequacy, intervention-gate, merge-approval, scope-overlap), but the gates are invisible to the operator unless they `tail -F` a log. The capability that exists but is invisible.
- mini_ork_fit: high
- star_impact: high
- effort: S
- risk: Pairing codes only make sense when there is an external party to pair (a chat user, a device node). mini-ork's audience is solo developers; "pair" must translate to "the recipe wants to do X; here's a one-time code in the IDE, type to approve." That is *approval UX for gates*, not DM pairing — and that is exactly what the existing gates can carry if they are surfaced.
- first_step: Add a "Pending approvals" inbox card to `mini_ork/ide_pages/inbox.py` that lists every gate decision (`mini_ork/gates/intervention_gate.py`, `merge_approval.py`) currently awaiting operator input, with a one-click approve/decline and a code-style numeric prompt for CLI use. Land it behind `MO_GATE_APPROVAL_UX=1` for the simple tier.

### K7 — "Skills you can actually see and pick"

- evidence: `git -C ... show HEAD:extensions/...` (50 skills enumerated: 1password, apple-notes, apple-reminders, bear-notes, blogwatcher, bluedesk, camsnap, clawhub, coding-agent, control-ui, diagram-maker, eightctl, gemini, gh-issues, gifgrep, github, gog, goplaces, healthcheck, himalaya, mcporter, meme-maker, model-usage, nano-pdf, node-connect, node-inspect-debugger, notion, obsidian, openai-whisper, openhue, oracle, ordercli, peekaboo, python-debugpy, sag, sherpa-onnx-tts, skill-creator, songsee, sonoscli, spike, spotify-player, summarize, things-mac, tmux, trello, visualize, weather, xurl); `VISION.md:120-128` ("We still ship some bundled skills for baseline UX. New skills should be published through ClawHub first, not added to core by default"); ClawHub at `clawhub.ai` (named in README line 89/114).
- what_openclaw_does: The operator opens a "skills" pane, browses 50+ named capabilities, enables one, and it shows up in the next prompt as a usable verb. New skills are published to a marketplace (ClawHub) rather than added to core.
- why_it_drives_stars: "It already has 50 things it can do, and there's a marketplace" is the breadth demo. mini-ork has *recipes* (50+ in `recipes/`), which are conceptually the same primitive — but framed as "task classes" rather than "capabilities I can pick from." Repackaging them is essentially zero engineering; surfacing them is the win.
- mini_ork_fit: high
- star_impact: medium
- effort: S
- risk: Repackaging recipes as "skills" risks diluting the recipe DAG's atomicity if a skill tries to be a recipe. Keep the division: a recipe is a multi-node DAG with a kickoff; a skill is a single-verb capability bound to a node type. Map existing recipes one-to-one to a "skill manifest" that names the node types it consumes, then surface that manifest.
- first_step: Add `mini_ork/skills/manifest.py` that walks `recipes/<name>/` and emits a `<name>.skill.yaml` summarising inputs/outputs/required node types. Surface the union in a new IDE page `mini_ork/ide_pages/skills.py` that mirrors OpenClaw's skills pane (grid view, one card per skill, click to see kickoff + verdict history). No new code; only re-shaping.

### K8 — "Drive my computer" (computer-use node)

- evidence: `git -C ... show HEAD:extensions/cua-computer/package.json` (CUA driver across darwin-universal, linux-{arm64,x64}, win32-{arm64,x64}; `@trycua/cua-driver` + `rastermill`; `cuaDriverArtifacts` per platform); `docs/concepts/computer-use.md` (47.4K — full computer-use docs) lists screenshot/click/type/file-transfer primitives; `docs/nodes/` enumerates node-host, session-hosting, talk, camera, presence, voicewake, media-playback.
- what_openclaw_does: A node type can take screenshots, click buttons, type text, and drive native apps across macOS/Windows/Linux. The user watches a screencast or a side-by-side preview while the agent works.
- why_it_drives_stars: "It can use my computer" is the canonical 2025/2026 "wait, it can do that?" reaction. The screencast-as-demo is its own viral loop.
- mini_ork_fit: low
- star_impact: high
- effort: L
- risk: Computer use is heavy infra (per-platform binaries, screenshot pipelines, X11/Cocoa/Win32 differences), heavy trust surface (a runaway agent clicks the wrong button), and off-strand for mini-ork's "task operating system" framing — mini-ork's audience is the *engineer who wants their agent to write code*, not the *operator who wants their Mac to be driven*. Mark this as the high-effort, off-mission option; only adopt if K1-K7 land and the next cycle is starved.
- first_step: Park it. Add a one-paragraph `docs/architecture/future-computer-use.md` that scopes the surface (would need a separate `mini_ork/computer/` module, per-platform node-host binaries, and a screencast relay) and notes it as deferred behind K1-K7.

## Ranked Shortlist

1. K1 — team preset + visible team panel — the headline "an org of agents" demo
2. K4 — live lane pivot — smallest effort, exposes capability mini-ork already has
3. K6 — operator-facing gate approval UX — capability exists but is invisible
4. K2 — project-scoped memory + diary — long-lived persona, project-not-personal framing
5. K3 — wider integration surface (GitHub/Linear/Jira before chat channels) — omnipresence without a chat-surface rebuild
6. K5 — between-node steering — mid-run verb without breaking the artifact contract
7. K7 — skills pane — repackage existing recipes; cheapest surface-area expansion
8. K8 — computer-use node — high-effort, off-mission; deferred

## mini-ork Gap Analysis

For each candidate, what mini-ork has today (cited) and what is missing.

**K1 — Team preset**
- **Today**: `mini_ork/orchestration/` has the conductor, runner, item-fanout, and topology modules that can run multi-node DAGs; `recipes/` has 50+ recipes including researcher → implementer → reviewer chains (`recipes/recursive-self-improve/`, `recipes/audit-judge-panel-3l/`). `docs/architecture/artifact-graph.md:23-27` defines the edge-as-completion-gate contract that already supports this. The `AGENTS.md`-equivalent contract lives in each recipe's kickoff.
- **Missing**: No "team preset" abstraction above the recipe. No IDE panel that shows "coordinator + 3 specialists" as a single entity. No command that spawns a whole team in one shot. The capability is *atomic* but not *visible*.

**K4 — Live lane pivot**
- **Today**: `mini_ork/dispatch/llm_dispatch.py` (49.9K) is the heterogeneous-lane core; `mini_ork/dispatch/routing.py` (16.9K) is the policy registry; `mini_ork/lane_router.py` does the GRPO advantage + UCB routing; `mini_ork/dispatch/cost_pause.py` is the cost gate; `mini_ork/dispatch/throttle_guard.py` is the rate-limit gate. `docs/architecture/verification-stack.md` proves the verdict is execution-anchored.
- **Missing**: No IDE verb to swap lanes mid-run. No transcript-preservation rule for partial outputs (OpenClaw's `model-failover.md:21-26` says "the runner attempts bounded same-model recovery … preserving partial output and completed work" — mini-ork has nothing equivalent). The dispatch is real; the surface is one CLI flag deep.

**K6 — Operator-facing gate approval UX**
- **Today**: 30 files in `mini_ork/gates/` including `intervention_gate.py`, `merge_approval.py`, `promotion_gate.py`, `scope_overlap.py`, `gate_bootstrap.py`. Gates already block destructive ops; they emit typed results.
- **Missing**: No inbox card for pending gate decisions. The operator has to read a log. `mini_ork/ide_pages/inbox.py` lists run rows but does not list gate decisions. The "wait, it asked before it did the destructive thing" UX is absent.

**K2 — Project-scoped memory + diary**
- **Today**: `mini_ork/memory/` (preferences.py, retirement.py, semantic.py, store.py) plus `docs/PYTHON-SDK.md:25-29` exposes `memory` as a primitive with verified-outcome semantics — a much stronger epistemic primitive than OpenClaw's plaintext `MEMORY.md`. No background consolidation loop; no diary files; no cross-session bootstrap for project preferences.
- **Missing**: No continuous sweep; no diary; no bootstrap from `MEMORY.md` on a fresh session. The capability (verified memory) exists and is invisible; the consolidation loop is absent.

**K3 — Wider integration surface**
- **Today**: `mini_ork/integrations/` has discord.py (47K), slack.py (33K), telegram.py (20K), plus `_base.py` and `_ws.py`. `mini_ork/pm/` exposes the PM mirror that reads Linear/Jira/GitHub.
- **Missing**: No chat-like inbound from the PM sources. The PM mirror is read-only; the operator cannot answer an `awaiting input` prompt from a Linear ticket. The omnipresence surface is three chat channels plus a read-only PM mirror.

**K5 — Between-node steering**
- **Today**: `mini_ork/orchestration/` has the runner and conductor that own the node sequence; `mini_ork/ide_pages/thread.py` is the thread action surface; `docs/architecture/artifact-graph.md:23-27` makes the edge a completion gate.
- **Missing**: No between-node directive injection. The IDE has no "Steer" thread action; the runner has no between-node rewrite hook for the *next* kickoff. Steering as a concept does not exist in the operator UX.

**K7 — Skills pane**
- **Today**: `recipes/` has 50+ DAGs that already carry manifests (`task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`). `mini_ork/extensions.py` exposes the extension registry and recipe builder.
- **Missing**: No skills pane; no per-skill manifest format; no "browse 50 capabilities and pick one" surface. The recipes are loaded by classification, not by browsing.

**K8 — Computer-use node**
- **Today**: Nothing. `mini_ork/` has no platform-bridging module; `recipes/` has no computer-use recipe; `docs/architecture/` has no node-host design.
- **Missing**: Everything — per-platform binaries, screenshot pipeline, screencast relay, click/type primitives, sandbox, audit trail. Capability is fully absent.

## Open Questions

1. **Is mini-ork's audience the engineer-who-runs-recipes or the operator-who-watches-agents?** OpenClaw's headline is the second; mini-ork's current posture is the first. K1, K4, K5, K6 require the second; if the answer is "first," K2 (project-scoped memory) and K7 (skills pane) carry more weight than K1.
2. **Will the IDE surface be a primary differentiator or a secondary artifact?** K1, K4, K5, K6 all assume the IDE is a primary surface (mini-ork already has 35 IDE page files, suggesting yes). If yes, K4's "live lane pivot" is the cheapest demo per line of code; if no, K7 (skills pane on the CLI) is the cheapest entry.
3. **Does mini-ork want a marketplace?** OpenClaw's ClawHub is the marketplace for skills/plugins. K7 is much higher-impact if mini-ork commits to a "skills marketplace" surface; much lower if recipes stay classification-driven. The decision must precede K7's first step.
4. **What is the trust posture for destructive ops?** K6's approval UX assumes gates already block destructive work. mini-ork's `intervention_gate.py` is the candidate; the open question is whether the gate list is the right surface, or whether mini-ork needs a separate "dangerous verb" classifier.
5. **How is "long-lived persona" framed without crossing into personal-assistant territory?** K2's risk paragraph depends on this. If the answer is "project-only, operator-curated," K2 ships; if the answer is "persona-wide, model-decided," K2 fails the synthesis guardrail and should be re-scoped or dropped.