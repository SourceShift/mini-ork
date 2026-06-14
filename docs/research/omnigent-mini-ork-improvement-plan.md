---
title: "Omnigent → mini-ork: comprehensive improvement plan"
source: Databricks engineering blog 2026-06-13 — *Introducing Omnigent: A Meta-Harness to Combine, Control and Share Your Agents* (Zaharia + Uhlenhuth)
upstream_repo: https://github.com/omnigent-ai/omnigent
status: design-note
last_updated: 2026-06-14
audience: agent+human
canonical_path: docs/research/omnigent-mini-ork-improvement-plan.md
tags: [omnigent, meta-harness, composition, collaboration, sandbox, policies, mini-ork-roadmap]
---

# Omnigent → mini-ork: comprehensive improvement plan

Databricks announced Omnigent on 2026-06-13 as the "meta-harness" layer
above coding agents — Claude Code, Codex, Pi, and custom YAML agents
— unified by a common interface that adds composition, control,
collaboration, and multi-surface access. Mini-ork is already a
meta-harness shaped product, but Omnigent's release exposes seven
specific capability gaps where mini-ork is materially behind a fresh
Apache-2.0 competitor. This doc reads the Omnigent announcement
through mini-ork's lens, names where we already win, names where they
already win, and ships a six-phase plan with concrete primitives in
shipping order.

---

## 1. The Omnigent thesis, in one paragraph

Each LLM coding agent (Claude Code, Codex, Pi, Cursor, etc.) lives in
its own silo: its own state, its own controls, its own context.
Whenever your work spans multiple agents — and it does, increasingly,
because "Harvey-style frontier-advisor + open worker model" wins —
you end up copy-pasting prose between them. Omnigent's answer is to
lift composition, control, and collaboration **out** of each agent's
silo and into a layer above. Their seven shipped capabilities:

| # | Capability | What it does |
|---|---|---|
| O-1 | **Common interface above harnesses** | Wrap any agent (Claude Code, Codex, Pi, Agents SDK, custom YAML) behind one API: messages + files in, text streams + tool calls out. |
| O-2 | **Real-time collaboration** | Share a live session URL; teammates can view, comment on files, or send commands while the agent runs. |
| O-3 | **Multi-interface access** | Same session reachable from terminal, web, macOS native app, mobile, or HTTP API. |
| O-4 | **Cloud execution** | Run any agent on your local box OR on hosted sandbox providers (Modal, Daytona, Fly.io). Hermetic environments for safe collaboration. |
| O-5 | **Contextual security policies** | Stateful guardrails that track session state, not just `allow X / deny Y`. E.g. "after `npm install`, require human approval for `git push`." |
| O-6 | **Cost policies** | Dynamic per-session cost tracking with `pause + ask-to-continue` checkpoints (e.g. "pause every $100"). |
| O-7 | **Strong OS sandbox** | Flexible OS lockdown + network egress proxy. Don't let the agent see your GitHub token — inject it at the egress proxy only on approved requests. |

The roadmap items they tease: GEPA-style automatic optimization at the
meta-harness level, MemEx/RLM-style code-based introspection, an
Omnigent Server MCP, and more harness adapters.

---

## 2. Where mini-ork already wins

Five capabilities that mini-ork ships today and Omnigent either lacks
or does not differentiate on:

### M-1. Distinct-family heterogeneity as a **load-bearing contract**

Omnigent treats harness/model choice as a "swap with one-line change."
Mini-ork treats it as a **mechanically enforced** precondition for
honest review (Rajan 2025 submodularity, Nasser 2026 α=0.042
cross-family). The `coalition_gate.sh` (commit `f7890a7`) refuses to
publish synthesis from same-family panels. Omnigent gives users the
*option* to mix; mini-ork makes mixing *required* for any synthesis-
class output. This is not a feature comparison — it's a quality
guarantee.

### M-2. Deterministic verifier gates

Mini-ork's recipes ship with `verifiers/*.sh` that decide pass/fail by
exit code. Omnigent's policies are state-tracking but still
prompt-shaped on the "did the work succeed" question — they enforce
guardrails (cost, permissions) but not artifact correctness. The
mini-ork side ships eight oracle gates (`coalition`, `cw_por`,
`krippendorff_alpha`, `citation_verifier_mechanical`,
`refute_or_promote`, `honest_ci`, `anchor_corpus`,
`langfuse_score_mapper`) that map verdicts to numeric quality scores.

### M-3. Token-level cost ledger + circuit breaker

`MO_DAILY_BUDGET_USD` + `llm_calls` table + cache-aware cost migration
(0024) + the new `pricing_strategy.sh` (commit `13ea509`) give per-
provider, per-model, per-token-kind rates and a hard daily cap. The
behavioral circuit breaker (`circuit_breaker.sh`) detects cost-burn-
without-write and three other stagnation signals. Omnigent's cost
policies are session-level pause-and-resume — we have that pattern
*per gate type* with finer instrumentation.

### M-4. Pluggable recipe model

24 recipes ship today, each composable from the same eight node types
+ six edge types + seven gates. Omnigent has YAML agents but no
articulated "recipe portfolio" with the same composition discipline.
Their `omnigent.ai/docs/use/custom-agents` is roughly our
`recipes/<name>/workflow.yaml` shape, but lacks the cross-recipe
benchmarking, gradient extraction, and self-improve infrastructure we
built around the recipe abstraction.

### M-5. State-DB substrate

`state.db` (sqlite with WAL + 25 migrations) records every run, every
LLM call, every gradient, every promotion decision. Omnigent's
collaboration is real-time; mini-ork's substrate is *durable* — runs
from six months ago are still queryable, comparable, and feedable
into learning records. This is the bigger half of "compounding agent
work, not one-off prompting."

---

## 3. Where Omnigent already wins (the seven gaps)

This is the meat. Each gap names a specific capability Omnigent ships
that mini-ork does not have today.

### G-1. Multi-interface access (web / desktop / mobile / terminal / HTTP)

**Omnigent:** same session reachable from any of five surfaces.

**Mini-ork:** CLI only. The dashboard repo exists as a separate
read-only React+FastAPI project (`mini-ork-dashboard`), but it is
read-only and a separate deploy. There is no native app, no mobile
client, no HTTP API for *sending commands* to a running dispatch.

**Why this matters:** today, to check on a long-running dispatch,
operators ssh in. To pause one mid-stream, operators kill -SIGSTOP
the process. To answer a profile-gate question from elsewhere,
operators have to either be on the box or pre-arm
`MO_AUTO_ANSWER_PROFILE`.

### G-2. Real-time collaboration

**Omnigent:** invite teammates to view a session, comment on files in
its workspace, or send commands while the agent runs.

**Mini-ork:** session traces are private to the box and post-hoc-
shareable via copy/paste. Two operators on different machines cannot
collaborate on a single dispatch.

**Why this matters:** complex dispatches (recursive-validate-impl,
epic-runner) take 30+ min. Today the operator who started it owns it.
Without sharing, pairing or review workflows require synchronous Zoom
+ screen-share, which doesn't scale beyond two people.

### G-3. Cloud-sandbox execution

**Omnigent:** launch any agent on your local box OR on hosted sandbox
providers (Modal, Daytona, Fly.io). Hermetic environments for safe
collaboration.

**Mini-ork:** local execution only. The framework-edit recipe creates
throwaway worktrees locally; spawned children also run locally
(commits 62cd3f5 + f3ce341 closed the worst race conditions but did
not address the sandboxing direction).

**Why this matters:** running mini-ork against a high-stakes repo
mutates the operator's working tree. A hermetic remote sandbox
isolates the blast radius — and unlocks "spin up 8 cross-family
lenses on 8 different cloud sandboxes in parallel."

### G-4. Strong OS sandbox + network egress proxy

**Omnigent:** lock down OS access, intercept and transform network
requests. *Never expose the GitHub token to the agent — inject it
only at the egress proxy on approved requests.*

**Mini-ork:** secrets are in `.mini-ork/config/secrets.local.sh`
sourced into each dispatch's environment, so any agent that runs
`env` or `echo $GITHUB_TOKEN` sees them. The .gitignore prevents
committing secrets; nothing prevents the dispatched agent from
exfiltrating them in its trace.

**Why this matters:** this is the gap with the worst worst-case.
A misaligned or compromised provider response could include a tool
call that prints all env vars to stdout, and our trace store would
faithfully record + persist the secret. The Omnigent answer (proxy
injection on approved requests) is the correct design and we do not
have it.

### G-5. Stateful / contextual security policies

**Omnigent:** policies that track dynamic state per session. After
`npm install` of a new package, require human approval for `git push`.
After writing a file the agent didn't create, require operator
confirmation for the next mutation.

**Mini-ork:** `scope_gate.sh` is static path-based allow/deny. There
is no notion of session state in the gate decision; the same prompt
gets the same answer at minute 0 and minute 30.

**Why this matters:** real high-stakes policies *are* stateful. "OK
to run `pnpm install` once; not OK to run it three times in five
minutes." "OK to push if the verifier passed; not OK if the
implementer wrote outside scope." These are inexpressible in our
current gate vocabulary.

### G-6. Cost-pause + ask-to-continue

**Omnigent:** pause an agent and ask to continue every $100 spent.

**Mini-ork:** hard cap via `MO_DAILY_BUDGET_USD`. When the cap hits,
the dispatch refuses to start a new node — there is no
ask-to-continue path. Operators either raise the cap and re-dispatch
or live with the abort.

**Why this matters:** the daily cap is the wrong granularity for
expensive single dispatches. A 4-tier recursive-validate-impl loop
spending $25/iteration needs operator-in-the-loop *per iteration*,
not per day.

### G-7. Multi-harness wrappers (Claude Code itself as a node)

**Omnigent:** wrap an entire harness (Claude Code, Codex CLI, Pi) as
a unit you can call into another workflow.

**Mini-ork:** lane providers (`lib/providers/cl_codex.sh`,
`cl_glm.sh`, etc.) wrap LLM *clients*, not full coding harnesses.
The codex CLI lane invokes `codex exec`; the claude lane (when
wired) would call the API. Neither composes a full multi-turn
agentic harness as a sub-step.

**Why this matters:** the "Harvey pattern" Databricks names —
open-source worker model + frontier-advisor caller — assumes the
worker is a full harness with tools and a workspace, not just a
single-call LLM. Mini-ork can't natively represent that today.

---

## 4. Six-phase improvement plan

Each phase is independently shippable. Earlier phases unblock later
phases; numbering is sequencing, not strict dependency.

### Phase Ω-A — Cost-pause + ask-to-continue (G-6)

**Why first:** smallest scope, biggest immediate operator value, no
infra dependencies. Pairs with shipped `MO_DAILY_BUDGET_USD` +
`pricing_strategy.sh` (13ea509).

Files (3, multi-file via mini-ork):

1. `lib/cost_pause.sh` (NEW)
   - `mo_cost_pause_check <run_id> <delta_usd>` — when cumulative
     cost crosses a configured `MO_PAUSE_EVERY_USD` threshold,
     write a `.cost-pause` sentinel to the run dir and return rc=2.
   - Dispatcher (`bin/mini-ork-execute`) checks the sentinel
     before each LLM dispatch.
2. `bin/mini-ork-resume` (NEW)
   - Operator-facing entrypoint: removes the sentinel + emits an
     approval audit row.
3. `bin/mini-ork-execute` (MODIFY)
   - Source `cost_pause.sh`; call the check inside the existing cost
     hot loop; honor sentinels.

### Phase Ω-B — Stateful contextual policies (G-5)

**Why next:** unblocks Phase Ω-D (sandbox would otherwise be a
gate-bypass surface).

Files (3, via mini-ork):

1. `lib/policy_engine.sh` (NEW)
   - DSL: `policy <name> on <event> when <state-predicate> do <action>`
     where actions include `allow`, `deny`, `require_human_approval`,
     `log_only`.
   - State predicates query `state.db` for prior session events.
2. `db/migrations/0026_policy_state.sql` (NEW)
   - `policy_state` table: per-session counters/flags that policies
     read + mutate.
   - `policy_decisions` table: every policy evaluation audited.
3. `lib/scope_gate.sh` (MODIFY)
   - Delegate decisions to `policy_engine.sh` when a policy matches.
     Fall back to current static rules when no policy applies.

### Phase Ω-C — HTTP API surface for running dispatches (G-1 substrate)

**Why now:** prerequisite for both multi-interface (G-1) and
real-time collaboration (G-2).

Files (4, via mini-ork; biggest planned dispatch):

1. `mini_ork/web/api.py` (MODIFY — exists as read-only)
   - Add POST endpoints: `/runs/{id}/pause`, `/runs/{id}/resume`,
     `/runs/{id}/answer-profile-question`, `/runs/{id}/abort`.
   - Add SSE endpoint: `/runs/{id}/stream`.
2. `mini_ork/web/auth.py` (NEW)
   - Token-based per-operator auth for the new write endpoints.
3. `bin/mini-ork-serve` (MODIFY)
   - Boot the auth middleware; expose new routes.
4. `tests/test_web_smoke.py` (MODIFY)
   - Smoke tests for the four new endpoints.

### Phase Ω-D — Cloud-sandbox execution (G-3)

**Why before G-7:** sandboxing is the right substrate for "wrap a
full harness as a node."

Files (5, via mini-ork; substantial dispatch):

1. `lib/sandbox/local.sh` (NEW) — current local-dispatch path,
   refactored to a clean adapter.
2. `lib/sandbox/modal.sh` (NEW) — Modal.com adapter via their CLI.
3. `lib/sandbox/daytona.sh` (NEW) — Daytona adapter.
4. `bin/mini-ork-spawn` (MODIFY) — accept `--sandbox <local|modal|daytona>`
   flag; default `local`. Sandbox adapter handles workspace
   provisioning + artifact retrieval.
5. `recipes/*/task_class.yaml` — add `default_sandbox` field per
   recipe; epic-runner defaults to `modal` for parallel children.

### Phase Ω-E — Strong OS sandbox + network egress proxy (G-4)

**Why this late:** highest engineering effort + highest security
stakes; needs Phase Ω-D's sandbox abstraction in place.

Files (4, via mini-ork):

1. `lib/egress_proxy.sh` (NEW) — wraps the agent's network calls
   through a localhost proxy that strips outbound auth headers and
   reinjects per-policy.
2. `lib/secret_vault.sh` (NEW) — operator-facing API to store secrets
   that the proxy can inject. NOTHING in the agent's env or filesystem.
3. `.mini-ork/config/egress-policies.yaml` (NEW) — declares which
   destinations can receive which secrets.
4. `lib/providers/cl_*.sh` (MODIFY x6) — route LLM API calls through
   `egress_proxy.sh` instead of direct `curl`/`requests`.

### Phase Ω-F — Real-time session collaboration (G-2) + harness wrappers (G-7)

**Why last:** both depend on the HTTP API (Ω-C) + sandbox
abstraction (Ω-D).

Files (6, via mini-ork; biggest single dispatch):

1. `mini_ork/web/collab.py` (NEW) — SSE channel multiplexing for
   multiple subscribers per run.
2. `mini_ork/web/comments.py` (NEW) — file-anchor comments persisted
   to `state.db`.
3. `db/migrations/0027_session_collab.sql` (NEW) — `session_viewers`
   + `session_comments` tables.
4. `lib/harness_wrapper.sh` (NEW) — wraps a full coding-agent harness
   (e.g., a `claude-code exec` subprocess) as a workflow node that
   produces a diff + verdict.
5. `recipes/harness-bridge/` (NEW recipe) — demonstrates wrapping
   Claude Code as a node inside a mini-ork pipeline.
6. `docs/use/multi-harness-authoring.md` (NEW) — operator guide that
   parallels Omnigent's `docs/use/custom-agents`.

---

## 5. Recipe + integration changes downstream

Once the six phases ship, three existing recipes get materially better:

### `recursive-validate-impl`
- Pause + ask-to-continue between iterations (Ω-A) replaces today's
  pure budget cap.
- Tier-4 panel runs across **4 distinct cloud sandboxes in parallel**
  (Ω-D) instead of 4 serial local subprocesses.
- Reflector summary becomes shareable via collab URL (Ω-F).

### `epic-runner`
- Child framework-edit runs each get their own sandbox (Ω-D),
  closing the file-reversion race more cleanly than the cleaner.sh
  fix (commit `f3ce341`).
- Wave aggregator publishes a live URL operators can monitor (Ω-C).

### `refactor-audit`
- Egress proxy (Ω-E) means findings panels can include real
  GitHub API calls *without* the panel seeing the token.
- Stateful policy (Ω-B): if the audit's findings include any
  high-severity, require operator approval before publisher
  commits the synthesis.

---

## 6. Honest trade-offs + open questions

Three places where this plan is **not strictly an upgrade** to mini-ork:

1. **HTTP API + auth + collab move us away from local-first.** Today
   mini-ork runs entirely on the operator's box with no inbound
   network surface. Phase Ω-C opens an authenticated HTTP server;
   Phase Ω-F opens SSE + comment writes. The threat model shifts
   from "trust the operator" to "trust the auth layer + the
   operator." Operators who prefer the local-first stance can opt
   out by never running `mini-ork serve` — but the recipes won't
   gain the multi-interface affordances.

2. **Cloud sandboxes (Ω-D) introduce a vendor relationship.** Modal
   and Daytona are paid services. The current daily cap measures
   LLM spend only; with cloud sandboxes there's a second
   compute spend to track. We need `pricing.yaml` extended with
   a `sandbox:` section + the cost ledger to record both.

3. **The egress proxy (Ω-E) breaks transparency for the wrapped
   agent.** When the agent calls GitHub and the proxy injects a
   token, the agent's transcript shows the call without the token —
   useful for not leaking secrets, but it means the agent's
   reasoning trace is no longer a complete reproduction record.
   Operators replaying a trace need separate audit of the proxy
   injections. We need to ship the proxy with a structured audit
   log that mirrors the LLM trace store.

---

## 7. What we are explicitly NOT doing (yet)

- **Building our own provider** — mini-ork stays provider-neutral.
  Omnigent describes "many LLM providers" as deploy targets; we
  treat each provider as a lane.
- **Building a hosted SaaS** — mini-ork stays local-first runtime,
  with optional self-hosted server. No cloud control plane managed
  by SourceShift.
- **Replicating Omnigent's "starfish" UI** — the mini-ork dashboard
  stays a separate repo; we don't rewrite our React layer to match
  theirs.
- **Adopting their YAML agent format directly** — our recipes are
  shaped by mini-ork's eight-node grammar. Their YAML can be ingested
  via Phase Ω-F's harness-wrapper, but it does not become our native
  format.

---

## 8. Citations + references

- **Omnigent blog post** — Zaharia + Uhlenhuth, *Introducing
  Omnigent: A Meta-Harness to Combine, Control and Share Your
  Agents.* Databricks engineering, 2026-06-13.
- **Omnigent repo** — https://github.com/omnigent-ai/omnigent
  (Apache 2.0)
- **Omnigent docs** — https://omnigent.ai
- **Related work cited by Omnigent**:
  - Harvey + Fireworks: open-source-agents-frontier-advisors
  - Anthropic multi-agent research system
  - Databricks Genie data agents
  - GEPA optimization (https://gepa-ai.github.io/gepa/)
  - MemEx programmable scratchpad
  - RLM (Alex Zhang, 2025)

### Already shipped mini-ork primitives this plan composes with

- `coalition_gate.sh` (commit `f7890a7`) — heterogeneity precondition
- `pricing_strategy.sh` (commit `13ea509`) — config-driven rate table
- `circuit_breaker.sh` (commit `fa93340`) — behavioral cost-burn detection
- `MO_AUTO_ANSWER_PROFILE` (commit `c391d8f`) — autonomous-dispatch unblocker
- `lib/version_registry.sh` + `bin/mini-ork-rollback` (commit `89f7951`)
- Oracle-gate constellation: `krippendorff_alpha_gate.sh`,
  `citation_verifier_mechanical.sh`, `refute_or_promote_gate.sh`,
  `honest_ci_gate.sh`, `anchor_corpus.sh`, `langfuse_score_mapper.sh`
  (commits `3d1e815`, `31f7808`, `ad48ef3`, `91eba3d`, `f1a9032`,
  `c0e6ad8`)

### Earlier related design notes

- `docs/research/code2lora-mini-ork-application.md` — Code2LoRA
  paper explainer + 5 closed-weights shipping units (commit
  `e37e175`).
