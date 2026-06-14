---
title: "Omnigent vs mini-ork: side-by-side comparison"
source_repo_omnigent: https://github.com/omnigent-ai/omnigent (Apache 2.0, alpha as of 2026-06-13)
source_repo_mini_ork: https://github.com/SourceShift/mini-ork (Apache 2.0, v0.3-rc1)
status: comparison-note
last_updated: 2026-06-14
audience: agent+human
canonical_path: docs/research/omnigent-vs-mini-ork-comparison.md
tags: [omnigent, comparison, meta-harness, pros-cons, competitive-analysis]
---

# Omnigent vs mini-ork: side-by-side comparison

Cloned `omnigent-ai/omnigent` (Apache 2.0, Python 3.12+, alpha) at
`/tmp/omnigent` on 2026-06-14 and compared against the current mini-ork
HEAD (`c6eec81`). This doc names the structural differences honestly,
identifies what each side does better, and flags where the projects
have meaningfully different goals (so neither side is "wrong").

---

## 1. By the numbers

| Metric | Omnigent | mini-ork |
|---|---:|---:|
| **Languages** | Python 3.12 core, TypeScript web (ap-web), small JS/sh | Bash (lib/bin), Python (web + tests), TypeScript (ui), SQL migrations |
| **Core LOC** | ~223,637 Python | ~16,885 bash + python (lib + bin) |
| **Test LOC** | ~334,240 Python | ~15,290 (bash + python) |
| **UI LOC** | ~116,026 (ap-web TSX/TS) + macOS native app | ~431,795 (ui/ — separate React+FastAPI dashboard) |
| **Repo size on disk** | 62 MB | ~50 MB (with web/static cache) |
| **Domain-specific units** | YAML agent specs + harness adapters | 24 recipes + 25 DB migrations + 50+ lib primitives |
| **License** | Apache 2.0 | Apache 2.0 |
| **Status** | Alpha (announced 2026-06-13) | v0.3-rc1 (early preview) |

Honest read of the numbers: **Omnigent is 13x larger by Python LOC and
22x larger by test LOC**. Mini-ork is denser per primitive (bash scripts
are compact), but Omnigent has an order of magnitude more engineering
muscle behind it (this is a Databricks-internal-tool open-sourced;
mini-ork is a one-author OSS project).

---

## 2. What each side actually ships (architectural map)

### Omnigent layout

```
omnigent/                       — 64 top-level Python modules + 18 subpkgs
├── claude_native*.py (8 files) — Claude Code harness adapter
├── codex_native*.py (5 files)  — Codex CLI harness adapter
├── chat.py                     — Pi-style chat harness
├── cli.py / cli_*.py           — CLI surface (auth, diagnostics, sandbox)
├── cost_plan.py                — per-turn brain-model cost advisor
├── harness_aliases.py          — harness↔alias mapping
├── server/                     — FastAPI server (accounts, OIDC, sessions)
│   ├── app.py / API.md / DBSPEC.md
│   ├── auth.py / oidc.py       — auth layer
│   ├── managed_hosts.py        — cloud sandbox provider integrations
│   └── mcp_pool.py             — MCP server pool
├── runner/                     — per-session runner (the inner agent loop)
│   ├── cost_advisor.py         — runtime cost decisions
│   ├── pending_approvals.py    — operator-approval surface
│   ├── policy.py / mcp_manager.py
│   └── transports/             — terminal/web/mobile transports
├── sandbox/                    — OS sandbox backends
│   ├── bwrap.py                — Linux bubblewrap
│   └── seatbelt.py             — macOS Sandbox.framework
├── inner/                      — internal substrate (NOT public API)
│   ├── egress/                 — network egress proxy (CA, certs, proxy, rules)
│   ├── nessie/                 — internal state machine
│   └── static/                 — embedded static assets
├── policies/                   — typed policy DSL
│   ├── schema.py               — PolicyEvent / PolicyResponse TypedDicts
│   ├── registry.py / base.py
│   └── builtins/               — shipped policies
├── spec/                       — YAML agent spec parser
│   ├── parser.py / validator.py
│   ├── types.py                — typed agent-spec dataclasses
│   └── AGENTSPEC.md            — spec docs
├── stores/                     — durable storage layer
├── llms/                       — LLM provider integrations
└── tools/                      — tool registry (builtins + client-specified)

tests/                          — 64+ test directories mirroring omnigent/
ap-web/                         — Next.js web UI
sdks/                           — Python client SDK + UI SDK
docs/AGENT_YAML_SPEC.md         — spec authoring guide
docs/POLICIES.md                — policy authoring guide
openapi.json (310 KB)           — auto-generated API spec
```

### Mini-ork layout

```
mini-ork/                       — recipe-shaped task OS
├── bin/                        — 18 user-facing entrypoints
│   ├── mini-ork                — dispatcher (case-based subcommand routing)
│   ├── mini-ork-{classify,plan,execute,verify,reflect,improve,...}
│   ├── mini-ork-spawn          — recursive child dispatch
│   ├── mini-ork-rollback       — version-registry CLI verb (shipped this session)
│   └── ...
├── lib/                        — 50 framework primitives (~16K LOC bash + py)
│   ├── llm-dispatch.sh         — provider routing + cost ledger
│   ├── coalition_gate.sh       — ρ + family diversity (Rajan 2025)
│   ├── krippendorff_alpha_gate.sh + 7 other oracle gates (this session)
│   ├── pricing_strategy.sh     — config-driven (provider, model, kind) rates
│   ├── checkpoint.sh           — per-node recipe checkpoint (this session)
│   ├── verifier_rubric.sh      — ground-truth FP/FN feedback (this session)
│   ├── circuit_breaker.sh      — behavioral cost-burn detection
│   ├── version_registry.sh     — workflow/agent versioning + rollback
│   ├── group_evolver.sh        — workflow-candidate mutation
│   └── providers/cl_{codex,glm,kimi,minimax,deepseek,anthropic,zhipu}.sh
├── recipes/                    — 24 recipes (composable workflow shapes)
│   ├── code-fix/               — minimal reference recipe
│   ├── refactor-audit/         — 4-distinct-family lens audit
│   ├── research-synthesis/     — multi-source paper synthesis
│   ├── recursive-validate-impl/ — 5-tier verification (this session)
│   ├── epic-runner/            — multi-epic orchestrator (this session)
│   ├── recursive-self-improve/ — bottleneck-scan + arXiv lens loop
│   └── ... (18 more)
├── db/migrations/              — 25 SQL migrations
├── mini_ork/web/               — FastAPI server (read-only observability)
├── ui/                         — React+TS dashboard (separate from server)
├── tests/
│   ├── integration/            — bash integration suite
│   └── unit/ + test_web_smoke.py
├── ROADMAP.md / GOVERNANCE.md / SAFETY.md
└── docs/                       — positioning, architecture, research notes
```

---

## 3. Pros — where Omnigent is strictly better

### Ω-pro-1. Real OS sandbox backends with platform-specific implementations

Omnigent ships `omnigent/sandbox/bwrap.py` (Linux bubblewrap) and
`omnigent/sandbox/seatbelt.py` (macOS Sandbox.framework) as
properly-engineered backends with platform default selection. When
`bwrap` is missing, it falls back to `none` rather than silently
sandbox-leaking. This is a non-trivial security primitive that
mini-ork **does not have at all** — our agents run in the operator's
user-shell context with the operator's filesystem permissions.

### Ω-pro-2. Network egress proxy with token injection

`omnigent/inner/egress/` is a complete egress proxy: CA certs, MITM
proxy, traffic relay, rule engine. The blog post's claim — "the agent
never sees the GitHub token; the proxy injects it on approved
requests" — is real, not vaporware. Mini-ork's secrets live in
`.mini-ork/config/secrets.local.sh` and are sourced directly into the
dispatched agent's environment. Worst-case: a compromised provider
response that contains a tool-call printing all env vars exfiltrates
every secret to the trace store.

### Ω-pro-3. Typed policy DSL with TypedDict contracts

`omnigent/policies/schema.py` defines `PolicyEvent` and
`PolicyResponse` as TypedDicts with documented event types and result
codes. Policy callables are typed; the docstring is the authoritative
reference. Mini-ork's `scope_gate.sh` is a 100-line bash script
matching glob patterns. Policy expressiveness gap is wide:

```python
# Omnigent (typed, stateful)
def my_policy(event: PolicyEvent, config: dict[str, str]) -> PolicyResponse | None:
    if event["type"] == "tool_call" and event["data"].get("tool") == "git_push":
        if session_state.get("recent_npm_install"):
            return {"result": "REQUIRE_APPROVAL", "reason": "git push after npm install"}
    return None
```

```bash
# Mini-ork (static, path-only)
scope_allow=("recipes/" "lib/")
case "$file" in
    ${scope_allow[*]}) return 0 ;;
    *) return 1 ;;
esac
```

### Ω-pro-4. Per-turn cost advisor (LLM-judged model selection)

`omnigent/runner/cost_advisor.py` + `cost_plan.py` ships an LLM judge
that picks ONE model per user turn sized to the turn's difficulty
(trivial → cheap, medium → medium, hard → expensive). Mini-ork's
budget is static per-lane in `agents.yaml` — operators tune lanes,
not turn-difficulty.

### Ω-pro-5. Multi-surface client SDKs

`sdks/python-client/` + `sdks/ui/` ship as first-class consumers of
the server API. `openapi.json` (310 KB) is auto-generated and
versioned. macOS native desktop app is downloadable. Mobile browser
access works. Mini-ork's server exposes a read-only React dashboard
in a separate repo; there's no SDK story, no mobile app, no native
desktop client.

### Ω-pro-6. YAML agent spec with validator + versioning

`omnigent/spec/{parser,validator,types}.py` + `AGENT_YAML_SPEC.md` ship
a complete spec authoring framework. Authors can declare harness +
model + tools + sub-agents + policies in one YAML file. Validator
catches malformed specs at load time. Mini-ork recipes are
directory-shaped (workflow.yaml + prompts/ + verifiers/) with no
unified validator — drift between workflow.yaml and the actual
shipped prompts is caught only by integration tests.

### Ω-pro-7. Engineering maturity signals

- `pyrefly.toml` (Python type-checker config)
- `pyproject.toml` (22 KB — proper dependency tree)
- `uv.lock` (827 KB — reproducible builds)
- Pre-commit hooks (`.pre-commit-config.yaml`)
- `.github/` workflows (4+ workflows visible)
- `SECURITY.md` with disclosure process
- `CONTRIBUTING.md` shipped at root
- 334K LOC tests vs 224K LOC core = **1.49x test:core ratio**
- TypedDict / type annotations throughout

Mini-ork has tests but ratio is closer to **0.91x** (15K test vs 17K
core); shellcheck + smoke + integration but no type-checker for bash.

---

## 4. Cons of Omnigent (where mini-ork is strictly better)

### M-pro-1. Heterogeneity as a **mechanical contract**, not user choice

Mini-ork's `coalition_gate.sh` REFUSES to publish synthesis from a
panel where pairwise output correlation ρ exceeds 0.25 OR family count
is less than lens count. Operators **cannot** opt out; this is grounded
in Rajan 2025 submodularity. Omnigent treats harness mixing as a user
*option* — the README says "ask one agent to review another's work"
but doesn't enforce that the reviewer is from a different family. Two
Claude Code instances reviewing each other satisfies Omnigent's API
but is the homogenization trap Rajan 2025 proves is worse than single-
agent self-correction.

This is mini-ork's strongest competitive position. Omnigent ships
composition; mini-ork ships **honest composition**.

### M-pro-2. Deterministic verifier gates beat policy guardrails

Omnigent's policies decide allow/deny/approve on tool calls. Mini-ork
recipes ship `verifiers/*.sh` that decide pass/fail on the produced
ARTIFACT by exit code. A verifier that runs `pnpm tsc` returns the
typecheck result; no LLM opinion needed. Omnigent's policy says "does
this tool call look risky?"; mini-ork's verifier says "did the
artifact actually satisfy its contract?" Different questions, both
necessary, but mini-ork's question is the one that catches
hallucinated implementations that pass review prose but fail
compilation.

### M-pro-3. Eight orthogonal oracle gates with literature grounding

Mini-ork ships, with self-tests + literature anchors:

- `coalition_gate.sh` — Rajan 2025 (ρ + family diversity)
- `cw_por.sh` — Agarwal & Khanna 2025 (CW-POR participation orthogonality)
- `krippendorff_alpha_gate.sh` — Nasser 2026 (α < 0.4 → escalate)
- `citation_verifier_mechanical.sh` — Sistla 2025 (mechanical citation coverage)
- `refute_or_promote_gate.sh` — Agarwal 2026 (adversarial fabrication survival)
- `honest_ci_gate.sh` — Dai 2025 (per-finding confidence intervals)
- `anchor_corpus.sh` — Wang 2026 (held-out recall scoring)
- `langfuse_score_mapper.sh` — verdict → trace score conversion

Omnigent's `policies/builtins/` directory ships a much smaller set
focused on tool-call guardrails (allow/deny). No equivalent of
"validator fabrication-survival rate" or "panel α calibration" or
"citation recall floor." Mini-ork's oracle gates are a competitive
moat that 13x more LOC cannot trivially replicate — they require
literature curation, not just engineering effort.

### M-pro-4. Durable state.db + cross-run learning

Mini-ork's `state.db` (sqlite, 25 migrations) records every run,
every LLM call, every gradient, every promotion decision. Six-month-
old runs are queryable, comparable, and feedable into learning
records. Omnigent has `omnigent/db/` and `omnigent/stores/` but the
durability story is session-oriented, not run-history-oriented. The
distinction:

- **Omnigent's strength**: a live session you can share + resume.
- **Mini-ork's strength**: a corpus of past runs you can mine for
  what worked, what didn't, what evolved.

Both matter; they're different products of the same architecture.

### M-pro-5. Token-level cost ledger + behavioral circuit breaker

`circuit_breaker.sh` detects three orthogonal stagnation signals
(artifact-hash invariance, verdict-stuck, cost-burn-without-write)
with CLOSED→OPEN→HALF_OPEN state machine. Omnigent's `cost_advisor`
+ `cost_plan` decide per-turn brain selection but don't detect
"this dispatch has spent $40 and not produced new artifacts." A
silently-hallucinating dispatch in Omnigent burns budget until the
session-pause threshold; in mini-ork it gets killed by the breaker.

### M-pro-6. Recipe portfolio with composition discipline

24 mini-ork recipes share one grammar (8 node types, 6 edge types,
7 gates) so they compose. `epic-runner` dispatches `framework-edit`
children; `recursive-validate-impl` consumes the tier-1..tier-4
verifier shape. Omnigent's YAML agents are individual — there's no
"recipe-of-recipes" abstraction with mechanical compatibility
contracts.

### M-pro-7. Local-first + filesystem-transparent

A mini-ork operator owns their state.db, their run dirs, their
recipe code. No accounts, no auth, no server required for the
runtime. `bin/mini-ork run` works on a fresh checkout with no
network setup beyond LLM provider keys. Omnigent's `omnigent/server/`
+ `accounts_bootstrap.py` + `oidc.py` + `mcp_pool.py` is a real
server deploy; the "no laptop required" framing assumes you trust
Databricks's managed hosts OR set up your own Modal/Daytona account.

---

## 5. Where they have meaningfully different goals (neither side wrong)

| Dimension | Omnigent | Mini-ork |
|---|---|---|
| **Target user** | Engineer juggling 4-5 coding agents + wants them to share session state | Engineer who wants verifier-gated, cross-family-reviewed artifacts with durable trajectory |
| **Primary unit** | Live session | Run (task instance) |
| **Verification model** | Policy-gated tool calls | Verifier-gated artifacts |
| **Composition primitive** | YAML agent specs (one agent = one config) | Recipes (one workflow = composable node graph) |
| **Quality signal** | "Did the agent do something risky?" | "Did the artifact pass the executable contract?" |
| **State substrate** | Live session DB + sharing | Durable run history DB + learning records |
| **OS access** | Sandboxed by default (bwrap/seatbelt) | User-shell context |
| **Network** | Egress-proxied with token injection | Direct from agent process |
| **Cost control** | Per-turn LLM-judge cost advisor + pause checkpoints | Per-lane budget caps + behavioral breakers |
| **UI surface** | Web + macOS app + mobile + terminal | CLI + separate-repo read-only dashboard |
| **Collaboration** | Live shared sessions, comments, co-driving | Post-hoc shared artifacts |
| **Provider model** | "Use any model" — first-party API key OR subscription OR gateway | Provider-neutral, lane-routed; secrets stay local |

The honest read: **Omnigent is a meta-harness for live-collaborative
coding sessions**. **Mini-ork is a task OS for artifact-producing
multi-family agent runs.** Same conceptual abstraction layer — both
"the thing above the harness" — but optimized for different jobs.

A team running Omnigent gets pairing, sharing, sandboxed live work,
and policy guardrails on a long-running session. A team running
mini-ork gets verifier-passed artifacts, cross-family reviewed
findings, durable run history, and a compounding gradient-extracted
learning corpus.

You could plausibly run both: Omnigent for the human-in-the-loop
sessions, mini-ork for the autonomous artifact-producing recipes.

---

## 6. Strategic implications for mini-ork

Three takes:

### Take 1 — Don't try to outbuild Omnigent on their strengths

We are not going to ship a real bubblewrap + seatbelt sandbox + MITM
egress proxy + macOS native app + mobile browser + collaborative
session-sharing layer at 1 author of OSS effort vs Databricks-internal
team. Adopting Omnigent's bwrap/seatbelt + egress proxy + sandbox
sandbox abstractions verbatim (Apache 2.0 → Apache 2.0 is compatible)
is a faster path than reimplementing.

### Take 2 — Double down on our strengths

The oracle-gate constellation, heterogeneity-as-contract, deterministic
verifier shape, durable trajectory store, and recipe-composition
grammar are the moat. The Omnigent comparison shows they have neither
matched these nor announced plans to. Mini-ork's positioning should be
**"verifier-gated cross-family artifact production"** vs Omnigent's
**"live-collaborative meta-harness"**.

### Take 3 — Selectively adopt their substrate

The improvement plan at `docs/research/omnigent-mini-ork-improvement-plan.md`
proposes six phases. With this comparison in hand, the phases re-rank:

| Phase | Build vs adopt | Rationale |
|---|---|---|
| Ω-A cost-pause + ask-to-continue | **Build** | Composes with our pricing.yaml + circuit_breaker; small scope |
| Ω-B stateful policy engine | **Build, but inspired by Omnigent's TypedDict shape** | Adopting their typed-event schema buys us interop |
| Ω-C HTTP API | **Build** | Our dashboard repo needs this anyway |
| Ω-D cloud sandboxes | **Adopt Omnigent's `inner/sandbox` substrate** | Reimplementing bwrap/seatbelt wrappers is wasted effort |
| Ω-E OS sandbox + egress proxy | **Adopt Omnigent's `inner/egress` proxy** | This is the right primitive; rewriting helps no one |
| Ω-F collab + harness wrappers | **Build harness wrappers; defer collab** | Wrapping Claude Code as a node is mini-ork-shaped; live collab is Omnigent-shaped |

The dependency on Omnigent in Ω-D and Ω-E is the only place this plan
crosses a project boundary. Apache 2.0 makes that legitimate; we'd
ship a `lib/sandbox/omnigent-bridge.sh` that shells out to
`omnigent sandbox --backend bwrap` rather than rebuilding the
hundreds of lines of bwrap argument tuning ourselves.

---

## 7. Citations + references

- **Omnigent repo (cloned 2026-06-14)** —
  https://github.com/omnigent-ai/omnigent at HEAD as of clone time.
  Apache 2.0, alpha status.
- **Omnigent blog post** — Zaharia + Uhlenhuth, *Introducing
  Omnigent.* Databricks engineering, 2026-06-13.
- **Mini-ork repo** — https://github.com/SourceShift/mini-ork at
  HEAD `c6eec81`.

### Companion notes in this directory

- `omnigent-mini-ork-improvement-plan.md` — original six-phase plan
  before clone-based comparison (commit `c6eec81`).
- `code2lora-mini-ork-application.md` — Code2LoRA → mini-ork
  primitives (commit `e37e175`).
