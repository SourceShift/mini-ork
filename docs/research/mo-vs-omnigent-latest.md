# Synthesis: mini-ork vs omnigent — head-to-head

**Run:** mo-vs-omnigent-1781980506 · **Date:** 2026-06-20 ·
**Repos:** [mini-ork](https://github.com/SourceShift/mini-ork) (HEAD dde1388,
`feat/cn-outcome-feedback-mini-ork`) vs
[omnigent](https://github.com/omnigent-ai/omnigent) (depth-1 squashed clone, alpha).

> ## ⚠ Provenance warning — read before trusting Section 5
> This run was designed as a **4-lens** comparison (glm/kimi/minimax/opus).
> **Only 3 lenses produced output.** The **glm lens (BREADTH / market /
> adoption sweep) died on a vendor 429 Fair-Usage block** at 2026-06-21
> 02:39 UTC (`llm-failures/1781980787-glm.out`, `api_error_status:429`) and
> wrote **no `lens-glm.md`**. Per the plan's own risk note ("synthesis must
> not silently proceed on <4 lenses"), this synthesis does **not** pretend
> the market lens ran. Concretely:
> - The **adoption / ecosystem dimension has no empirical evidence** — no
>   GitHub stars, no PyPI/Homebrew download counts, no community-signal
>   sweep. Every adoption claim below is **reasoned from code surface
>   (minimax) and strategy (opus), not measured.** It is marked
>   `LOW (no market lens)`.
> - The **growth-potential verdict (Section 5) is the most weakened** by
>   this gap, because growth was the glm lens's home turf. It is delivered
>   as a **reasoned, not measured, call** and flagged accordingly.
> - **Consensus markers max out at ★★ (3-lens), not ★★★** — ★★★ ("all 4
>   lenses agree") is unreachable this run by construction.
> - The downstream `source-completeness.sh` verifier **will hard-fail** on
>   the missing `lens-glm.md` (verifier lines 24-30). That is the gate
>   working as intended, not a synthesis defect. Re-run the glm lane (or a
>   substitute breadth lane not subject to the same 429 policy) to close
>   the market dimension before treating the growth verdict as settled.

---

## Section 1 — TL;DR (≤6 bullets)

- **The two frameworks are nearly orthogonal bets, not competitors on one
  axis:** mini-ork bets on *epistemics* (can you trust the output? →
  enforced cross-family review + deterministic gates); omnigent bets on
  *ergonomics & reach* (can you drive/govern any harness from any device? →
  meta-harness + product surface). · opus + minimax · **HIGH**.
- **On verification discipline, mini-ork wins decisively and the literature
  backs it:** exit-code gates + Krippendorff-α dissent checks + vacuous-pass
  detection have no omnigent analog, and judge-shortcut/self-correction
  papers undercut LLM-as-judge for high-stakes review. · kimi + minimax +
  opus (★★) · **HIGH**.
- **Cross-family review is mini-ork's structurally-enforced invariant;
  omnigent leaves it to user discipline** — and omnigent's own `swe_org.yaml`
  ships a same-family reviewer pair, the exact failure the literature
  (Nasser 2026, α=0.042) says produces review theater. · kimi + minimax +
  opus (★★) · **HIGH**.
- **omnigent wins distribution & extensibility-breadth** — FastAPI server,
  57 OpenAPI paths, Docker/K8s/5-cloud deploy, Python+UI SDKs, a YAML spec
  that runs across 4 harnesses — but this is *code surface observed*, not
  *adoption measured*. · minimax + opus · **MEDIUM (no market lens)**.
- **The literature does not crown either:** mini-ork wins the
  quality-assurance bets (diverse review, deterministic gates, ledgered
  memory); omnigent wins the orchestration-efficiency bet (routing /
  meta-harness is economically rational — Chen 2023, Li 2024, Lee 2026). ·
  kimi · **MEDIUM-HIGH**.
- **Verdict (preview, see §5):** mini-ork is **more futuristic
  *conditional* on a plural-model world**; omnigent has **higher 12-24mo
  growth potential** — and the growth call *goes against the home team*,
  which is itself evidence the owner-affiliation bias correction is
  working. · opus + minimax · **futuristic: MEDIUM-HIGH; growth: LOW-MEDIUM
  (no market lens)**.

---

## Section 2 — Dimension-by-dimension scorecard

Edge cited by lens. `glm` absent throughout — adoption/distribution rows
carry no market evidence.

| Dimension | mini-ork | omnigent | Edge |
| --- | --- | --- | --- |
| **Orchestration model** | Explicit 6-phase bash state machine, resumable per-step, crash-finalized, closed node/dispatch-mode sets (MiniMax-1/3/4: `bin/mini-ork:15`, `bin/mini-ork-execute:5-14,195-200`) | Implicit per-turn loop inside a generic executor adapter wrapping 4 harnesses; loop buried in an 18,333-LOC `sessions.py` (MiniMax: `_executor_adapter.py:1-31`, `sessions.py`) | **Tie / split** — mini-ork on loop-explicitness & resumability; omnigent on "wrap any runtime" (MiniMax-D1) |
| **Verification** | Exit-code gates (0/1/2), vacuous-pass detection, Krippendorff-α dissent gate, closed-enum decisions (MiniMax: `gate_registry.sh:5-28`, `mini-ork-execute:82`, `krippendorff_alpha_gate.sh`); lit favors deterministic verifiers (Kimi: Lightman 2023, Cobbe 2021) and undercuts LLM-judge (Marioriyad 2026, Huang 2023) | Per-tool-call PolicyEngine ALLOW/DENY/ASK; deterministic *or* prompt policies; **no inter-annotator / population-level agreement** (MiniMax: `engine.py:54-117`) | **mini-ork** (Kimi + MiniMax + opus) ★★ |
| **Cross-family review** | Lane-bound family diversity enforced at config layer; standing directive bans same-family/gemini reviewers (MiniMax: `agents.yaml:11-13,18,40-45`); lit shows judge identity is a stable non-interchangeable signal (Kimi: Nasser 2026 α=0.042, De Nobili 2026) | Sub-agent hierarchy; diversity is a *user choice* of per-agent model string; ships a same-family reviewer pair in its own example (MiniMax: `swe_org.yaml:275,311`) | **mini-ork** (Kimi + MiniMax + opus) ★★ |
| **Memory** | SQLite ledger, 14 memory tables, git-anchored provenance per write, batch-bounded reflection (MiniMax: `memory.sh:5-100`, `reflection_pipeline.sh:46-62`); lit elevates provenance/lineage (Kimi: Wang 2026, Reflexion 2023) | 7-8 named stores, long-lived multi-user conversations, no git-anchored provenance (MiniMax: `app.py:60-69`) | **mini-ork** on discipline; **omnigent** on breadth (MiniMax + Kimi) ★ |
| **Extensibility** | Directory-recipe (workflow + contract + prompts + verifiers), line-editable YAML nodes, `.sh` verifiers (MiniMax: `mini-ork:73-87`, `workflow.yaml:15-24`) | YAML agent-spec travels across 4 harnesses; sub-agents-as-tools; strict fail-loud on unsupported fields (MiniMax: `omnigent.py:1-29`, `parser.py` 3,287 L) | **omnigent** on breadth; **mini-ork** on debuggability (MiniMax-D5) |
| **Distribution / UX** | bash + SQLite, no service, opt-in read-only `serve` UI, `git clone + install.sh` (MiniMax: `mini-ork:413-433,31-32`) | FastAPI server, 57 OpenAPI paths, Docker/K8s/Railway/Render/Fly/Cloudflare, Python+UI SDKs, desktop app (MiniMax: `openapi.json` 57 paths, `deploy/*`; opus §3) | **omnigent** (MiniMax + opus) ★ — but `MEDIUM (no market lens)` |
| **Maturity** | "early preview", v0.3.0-rc2 / self-described v0.1 (MiniMax: `mini-ork:436,440`) | "alpha", depth-1 squashed clone (no history); polished surface may inflate *perceived* maturity past code quality (opus §3; MiniMax-D7) | **Tie** — both pre-1.0, conditional verdicts (all 3 lenses) ★★ |
| **Ecosystem / adoption** | *no data — glm lens dead* | *no data — glm lens dead* | **UNRESOLVED** — `LOW (no market lens)` |
| **Governance / multi-user** | Lane policy + standing directives + promotion-gate enums; single-machine, single-user (MiniMax: `promotion_gate.sh:23-40`) | Policy engine scoped to server/agent/chat, OAuth, accounts, sandbox-credential-proxy & external-contributor designs, multi-tenant (opus §3; MiniMax: `app.py` OAuth/host-pool) | **omnigent** for multi-user; **mini-ork** for population-level verdict discipline (opus + MiniMax) |

---

## Section 3 — Consensus findings (≥2 lenses agree)

Marker scale (capped this run): **★** = 2 of the 3 present lenses · **★★** =
all 3 present lenses (kimi + minimax + opus). **★★★ (all 4) is unreachable —
glm absent.**

- **★★ Verification belongs to deterministic gates, not LLM judges, for
  high-stakes artifacts.** Kimi (Marioriyad 2026 hidden shortcuts; Huang
  2023 self-correction fails; Lightman 2023 + Cobbe 2021 verifiers work) +
  MiniMax (`gate_registry.sh`, vacuous-pass detection `mini-ork-execute:82`,
  Krippendorff-α gate) + opus (deterministic gates survive both consolidation
  *and* plural scenarios). `(Kimi-§2 + MiniMax-D2 + opus-§2)`.
- **★★ Family/model identity is a stable, non-interchangeable signal, so
  enforced cross-family review is real, not cosmetic.** Kimi (Nasser 2026
  α=0.042; De Nobili 2026 model-dependent collective fingerprints) + MiniMax
  (lane binding at `agents.yaml:40-45`, banned same-family reviewers) + opus
  (mini-ork makes diversity *mandatory*, omnigent only *possible*).
  `(Kimi-§1 + MiniMax-D3 + opus-§4)`.
- **★★ omnigent's meta-harness/routing bet is itself well-founded** — this is
  the one big axis where the *other* framework wins. Kimi (Chen 2023
  FrugalGPT, Li 2024 RouterBench, Lee 2026 Meta-Harness) + MiniMax (generic
  executor adapter over 4 harnesses, `_executor_adapter.py:48-60`) + opus
  ("the platform usually eats the feature", §3). `(Kimi-§3 + MiniMax-D1/D5 +
  opus-§3)`.
- **★★ Both frameworks are pre-1.0 with churn; all verdicts are conditional.**
  Kimi (preprint-dominated evidence base, §6) + MiniMax (alpha vs
  early-preview, `mini-ork:436`) + opus (Scenario A/B framing). `(Kimi-§6 +
  MiniMax-D7 + opus-§2)`.
- **★ omnigent wins distribution/product surface** (no market lens to confirm
  it converts to *adoption*). MiniMax (`openapi.json` 57 paths, `deploy/*`,
  SDKs) + opus (desktop app + mobile + PyPI/brew = the rational growth
  favorite, §3). `(MiniMax-D6 + opus-§3)`.
- **★ Ledgered, provenance-bearing memory favors mini-ork.** Kimi (Wang 2026
  execution-provenance survey; Reflexion 2023) + MiniMax (git-anchored
  `_mo_capture_reflection`, `memory.sh:47-100`). `(Kimi-§4 + MiniMax-D4)`.

---

## Section 4 — Disputed findings (lenses disagree)

Per discipline rule 2 and the plan's anti-bias note, disputes are **reported
honestly and NOT vote-ruled** (Nasser 2026, arxiv:2601.05114 — voting between
same-conviction agents amplifies bias).

**Dispute 1 — Which bet is "more futuristic": epistemics or ergonomics?**
- *MiniMax* leans mini-ork on the code axis (5-2 net tally) but explicitly
  flags its wins are *operational* while omnigent's are *user-facing*, and
  declines to call the future-bet (says it's opus-lens territory).
- *opus* says mini-ork is more futuristic **only conditional on a plural-model
  world (Scenario B)**, and steel-mans that omnigent's "conduct any harness"
  may be the *more general* abstraction that could absorb mini-ork's review as
  one policy ("the platform eats the feature").
- *Kimi* refuses to crown either: mini-ork wins QA bets, omnigent wins the
  orchestration-efficiency bet; "optimizing different points on the same
  design space."
- **Why they disagree:** they are scoring *different axes*. MiniMax scores
  implemented discipline (present-tense), opus scores conceptual leverage
  under future world-states, kimi scores which design bet the academic record
  supports per-dimension. They are not contradicting — they are answering
  three different questions that the kickoff collapsed into one word.
- **What would resolve it:** opus's §5 experiment — a held-out *defect-escape*
  benchmark on identical tasks, run as (1) single-agent, (2) omnigent driving
  3 sub-agents *as users actually configure them*, (3) mini-ork's mandatory
  cross-family panel — measured across model generations. A stable/growing
  mini-ork gap confirms the plural world; a shrinking gap confirms
  consolidation and flips "futuristic" to omnigent.

**Dispute 2 — Does omnigent's product surface convert to a durable moat?**
- *opus* treats distribution as destiny: the installable multi-device product
  "almost always out-grows the technically purer substrate", and calls
  omnigent the rational growth favorite *even after* bias correction.
- *MiniMax* observes the surface is real in code (57 endpoints, multi-cloud
  deploy) **but** flags it sits on an 18,333-LOC god-module (`sessions.py`)
  and an integration treadmill (per-harness bridges), i.e. surface ≠
  robustness; omnigent is "one large refactor away" from maintainable.
- *glm* — **the lens that would have settled this with actual adoption /
  star / download data is missing.**
- **Why they disagree:** opus reasons from the *historical pattern* of dev
  tools; MiniMax reasons from *current code health*. Neither has the
  adoption-trajectory data that only the dead market lens could supply.
- **What would resolve it:** the glm market sweep (stars over time, PyPI/brew
  installs, contributor count, real-world usage anecdotes), timestamped and
  treated as point-in-time. **Until re-run, Dispute 2 is genuinely open.**

**Dispute 3 — Is the cross-family proxy ("family diversity = independence")
load-bearing or decaying?**
- *Kimi* supports it empirically (fingerprinting) **but** could not verify the
  submodularity proof (Rajan 2025) mini-ork cites in `coalition_gate.sh`, so
  the lean rests on correlation, not a formal guarantee.
- *opus* raises proxy-decay as a named mini-ork failure mode: family ≠
  measured ρ; two "different" families sharing pretraining lineage buy
  correlated noise at premium cost.
- **Why they disagree:** they don't, exactly — both flag the same soft spot
  from different sides (missing proof / proxy fragility). The honest reading
  is the heterogeneity bet is *evidence-supported but not proven*.
- **What would resolve it:** locate Rajan 2025 (or its real title/venue), and
  have mini-ork measure realized inter-judge ρ rather than infer it from
  family labels.

---

## Section 5 — The verdict (REQUIRED)

Two explicit calls, each naming the world-assumption that would falsify it,
each corrected for owner-affiliation bias.

### More futuristic: **mini-ork — conditional**
Because its core bet (enforced, executable, cross-family verification) has
**superlinear leverage in family count** while omnigent's integration value
is roughly linear and maintenance-taxed per harness (opus §2; MiniMax-D1/D5),
and because the academic record on judge heterogeneity currently favors the
plural world that makes the bet pay off (Kimi-§1/§2: Nasser 2026, Marioriyad
2026, Lightman 2023).
- **World-assumption it rests on:** the model ecosystem stays **plural** —
  many roughly-comparable families coexist and none dominates *judgment*
  through 2028.
- **What would falsify it:** if one family decisively dominates judgment,
  mini-ork's headline differentiator decays to a feature and omnigent's
  "conduct any harness" becomes the more general future (opus §2, Scenario A).

### Higher growth potential (12-24 mo): **omnigent — reasoned, not measured**
Because a FastAPI server + 57-endpoint API + multi-cloud deploy + desktop/
mobile + client SDKs + governance is a distribution and enterprise-wedge
surface that a bash+SQLite "early preview" will not out-adopt on architectural
merit alone (opus §3; MiniMax-D6).
- **World-assumption it rests on:** distribution and product ergonomics drive
  adoption more than verifiability does, and buyers do **not** yet treat
  independent verifiability as a hard procurement requirement.
- **What would falsify it:** if regulated-AI procurement makes mandatory
  independent review a hard requirement, growth could swing to whoever owns
  verifiability (opus §6).
- **⚠ Confidence: LOW-MEDIUM.** This is the call most damaged by the missing
  glm lens. It is supported by *observed code surface* and *strategic
  reasoning*, **not by any adoption metric** (no stars, no downloads, no
  contributor data). Treat it as a hypothesis pending the market re-run, not
  a finding.

### When the other one wins instead
mini-ork wins growth too **if** verifiability becomes a procurement gate
(regulated, safety-critical, or research-synthesis buyers) — then mandatory
independent review is the feature people pay for. omnigent wins "more
futuristic" **if** the world consolidates onto one dominant judgment model, or
**if** it ships cross-family review as a *default policy* (closing the one
structural gap opus and minimax both identify), at which point the platform
absorbs the feature.

### Owner-affiliation bias check (explicit)
mini-ork is the **dispatching operator's own project** — a material conflict
of interest; all three lenses disclosed it. The failure mode would be a
verdict that flatters the home team on *both* axes. **This synthesis does
not:** the *futuristic* call for mini-ork is **conditional** and names the
exact world (consolidation) that flips it; the *growth* call goes **against**
mini-ork. A split verdict that hands the home team the conditional/conceptual
win and hands the rival the practical/adoption win is the signature of a
correction that held. The remaining honest caveat is **not** bias — it is the
**missing market lens**, which leaves the growth call under-evidenced in the
one place mini-ork would *least* want it weak (the axis where the rival wins),
so the gap does not flatter mini-ork either.

---

## Section 6 — Numbered recommendations (for a builder choosing today)

1. **Run opus's defect-escape benchmark before committing.** Run identical
   tasks through single-agent, omnigent-as-typically-configured, and
   mini-ork's mandatory panel, across two model generations. · opus §5 +
   Kimi-§1 · **Wrong if** the realized mini-ork defect-escape gap is <~5
   points or shrinking — then omnigent's simpler surface wins on cost.
2. **Choose mini-ork when your binding constraint is trustworthy output under
   plural vendors** — research synthesis, safety-critical or regulated review.
   · MiniMax-D2/D3 + Kimi-§2 · **Wrong if** your reviewers would collapse to
   one family in practice anyway (then you pay panel cost for theater).
3. **Choose omnigent when your binding constraint is reach** — multi-device,
   team collaboration, governance, fast onboarding, "pip install and go." ·
   MiniMax-D6 + opus §3 · **Wrong if** you need population-level verdict
   discipline (Krippendorff-α, vacuous-pass refusal) that omnigent's per-
   tool-call policy engine structurally lacks.
4. **Test the hybrid: run mini-ork's cross-family panel as a custom agent
   inside omnigent.** If it composes cleanly, omnigent absorbs the feature and
   mini-ork's "futuristic" edge collapses. · opus §6 + MiniMax-D5 · **Wrong
   if** omnigent's uniform-harness adapter cannot enforce family-distinctness
   (then the panel degrades to same-family review — the exact `swe_org.yaml`
   trap).
5. **If you build on omnigent, make cross-family review a *default policy*,
   not a possibility,** and budget the `sessions.py` refactor. · MiniMax-D2/D7
   + opus §4 · **Wrong if** your sub-agents are genuinely single-family by
   requirement (then the default is friction).
6. **If you build on mini-ork, ship one installable artifact and one
   throttle-resilient panel mode** — this run's own glm-lane 429 death is the
   live proof that the cross-family precondition degrades silently under
   vendor throttling (the differentiator is also the single point of
   operational failure). · opus §4 + this run's `llm-failures/` · **Wrong if**
   the panel can already fall back to fewer distinct families without
   collapsing to same-family consensus (verify `coalition_gate.sh` behavior
   under a dead lane).
7. **Before treating the growth verdict as settled, re-run the market/breadth
   lens.** · this synthesis's provenance warning · **Wrong only if** you
   accept a growth call with zero adoption data — which the discipline rules
   forbid.

---

## Section 7 — Source manifest

Grouped by lens. `glm` lens produced no sources (429 Fair-Usage block,
`llm-failures/1781980787-glm.out`).

### glm lens (BREADTH / market) — **ABSENT**
- No sources. Lane failed before producing `lens-glm.md`. Adoption /
  ecosystem dimension is unevidenced this run.

### kimi lens (literature) — 21 arXiv IDs (all verified by direct fetch per Kimi-§7)
- Heterogeneous review: Nasser 2026 `arxiv:2601.05114`; Liang 2023
  `arxiv:2305.19118`; Zhang 2026 `arxiv:2605.24048`; De Nobili 2026
  `arxiv:2605.10528`; `[lookup: Rajan 2025 submodularity — unverified]`.
- Verification gates: Zheng 2023 `arxiv:2306.05685`; Marioriyad 2026
  `arxiv:2602.07996`; Huang 2023 `arxiv:2310.01798`; Lightman 2023
  `arxiv:2305.20050`; Cobbe 2021 `arxiv:2110.14168`; Wang 2022
  `arxiv:2203.11171`.
- Meta-harness / routing: Chen 2023 `arxiv:2305.05176`; Li 2024
  `arxiv:2403.12031`; Lee 2026 `arxiv:2603.28052`; Wu 2023 `arxiv:2308.08155`;
  Qin 2023 `arxiv:2307.16789`.
- Memory / ledger: Shinn 2023 `arxiv:2303.11366`; Wang 2023 `arxiv:2305.16291`;
  Wang 2026 `arxiv:2606.04990`; Zhang 2026b `arxiv:2602.06052`.

### minimax lens (code-architecture) — 29 repo:file:line anchors
- mini-ork: `bin/mini-ork:15`; `bin/mini-ork:37-410`; `bin/mini-ork-execute:5-14`;
  `bin/mini-ork-execute:18`; `bin/mini-ork-execute:69-116`;
  `bin/mini-ork-execute:195-200`; `.mini-ork/config/agents.yaml:11-13`;
  `.mini-ork/config/agents.yaml:18`; `.mini-ork/config/agents.yaml:40-45`;
  `lib/gate_registry.sh:5-28`; `lib/memory.sh:5-10`; `lib/memory.sh:47-100`;
  `lib/reflection_pipeline.sh:46-62`; `lib/promotion_gate.sh:23-40`;
  `recipes/mo-vs-omnigent/workflow.yaml:15-38`;
  `recipes/mo-vs-omnigent/artifact_contract.yaml:1-16`; `mini-ork:413-433,436,440`.
- omnigent: `omnigent/server/app.py:12`; `omnigent/server/app.py:60-69`;
  `omnigent/runtime/harnesses/_executor_adapter.py:1-31,48-60`;
  `omnigent/runtime/policies/engine.py:40,43-117`; `omnigent/spec/types.py:39`;
  `omnigent/spec/omnigent.py:1-29`;
  `tests/resources/examples/swe_org.yaml:154-378,275,311`;
  `tests/resources/examples/agent_with_policies.yaml:52-67`;
  `tests/resources/examples/claude_code_agent.yaml:31-46`;
  `omnigent/server/routes/sessions.py` (18,333 LOC);
  `omnigent/openapi.json` (57 paths); `deploy/{docker,kubernetes,railway,render,fly,cloudflare}/`.

### opus lens (strategic narrative) — 7 sources
1. mini-ork README — https://github.com/SourceShift/mini-ork
2. omnigent README — https://github.com/omnigent-ai/omnigent · https://omnigent.ai
3. Nasser 2026 — https://arxiv.org/abs/2601.05114
4. Rajan 2025 — https://arxiv.org/abs/2511.16708
5. Karanam 2025 — https://arxiv.org/abs/2512.21352
6. Zietsman 2026 — https://arxiv.org/abs/2603.25773
7. First-hand repo inspection (this run, HEAD 2026-06-20): omnigent ~421 .py
   files / 57 OpenAPI paths; mini-ork ~171 bash/sh files / SQLite state.db.

### this synthesis
- Run failure evidence: `.mo-run-home/runs/mo-vs-omnigent-1781980506/llm-failures/1781980787-glm.out`
  (glm 429 Fair-Usage block).
- Verifier gating on 4-lens completeness:
  `recipes/mo-vs-omnigent/verifiers/source-completeness.sh:24-30`.

---

*Note on Rajan 2025: opus cites it as `arxiv:2511.16708` (multi-agent code
verification / conditional independence); kimi could not locate a matching ID
for the submodularity proof referenced in mini-ork's `coalition_gate.sh` and
held it as an unverified lookup. The two may be different papers or a
mis-citation — flagged for the verifier, not silently merged.*
