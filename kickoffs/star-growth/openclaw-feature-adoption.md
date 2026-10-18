# Which OpenClaw features should mini-ork adopt to grow its stars?

## Goal

Produce one ranked, evidence-grounded adoption plan: the features and packaging
decisions from OpenClaw that mini-ork should adopt because they would most
increase mini-ork's GitHub stars. Four lenses analyse the reference
independently; a synthesizer reconciles them into a top-10 plan with first
steps, effort and a 90-day sequence. Nothing is implemented in this run.

Execute the compiled workflow exactly. Do not research, do not add lens,
drafter, prelude-panel or arbiter nodes — the four lens nodes ARE the analysis.

## Reference checkout

Absolute path: `/Volumes/docker-ssd/ps/openclaw-brief/repo` (this host).
On another host, clone `https://github.com/openclaw/openclaw` shallow and
blob-filtered and use that checkout instead; the lenses only need
`git ls-tree` and `git show HEAD:<path>` to work.

Shallow, blob-filtered clone of `https://github.com/openclaw/openclaw` at
`2026-10-18`. Checked out: `README.md`, `VISION.md`, `CHANGELOG.md`,
`AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md`, `package.json`, `docs/`.
Any other path can be read with
`git -C /Volumes/docker-ssd/ps/openclaw-brief/repo show HEAD:<path>`
(read-only; this may fetch a blob from the promisor remote).

Measured reference metrics (verify what you can, treat the rest as context):

- 392,373 stars, 82,480 forks, 9,433 open issues
- created 2025-11-24, last push 2026-10-18 — under one year old
- MIT license, TypeScript, topics: ai, assistant, crustacean, molty, openclaw,
  own-your-data, personal
- 183 entries under `extensions/`, 50 under `skills/`, 28 under `packages/`
- product shape: a personal AI assistant that meets the user in Discord,
  iMessage, Slack, Teams, Telegram, WhatsApp and 20+ more channels, runs from
  one local Gateway, with native apps on macOS/iOS/Android/Windows/Linux,
  a ClawHub skills registry, and model providers as swappable plugins.

Reference **mechanisms** matter, not the product category. Do not recommend
that mini-ork become a personal assistant.

## Target: mini-ork

The target tree is `$MO_TARGET_CWD` (read-only for this run). mini-ork is a
task operating system for agents: a recipe/DAG runner with heterogeneous model
lanes and fallback, cost governance (`MO_DAILY_BUDGET_USD`, `budget_gate`),
runtime verification (relations, differential, suite adequacy, gate
hackability), a GRPO learning loop, a Python SDK, a Zed-fork IDE surface, and
artifact contracts. Its audience is developers building and running agent
workflows, not end users chatting with an assistant.

Judge every candidate by whether it makes that audience say "I want this in my
stack" — a star — not by whether it makes mini-ork a different product.

## Lenses

The four nodes run in parallel and do not see each other's work:

- `growth_lens` — positioning, naming, README-as-landing-page, proof, momentum.
- `activation_lens` — install-to-first-value, onboarding, defaults, recovery.
- `ecosystem_lens` — extension/plugin surface, registries, SDKs, integration
  breadth, contributor funnel.
- `capability_lens` — demonstrable capability, the "wait, it can do that?" demo,
  composition, model swapping, interfaces.

Each lens must propose at least six candidates with lane-scoped ids
(`G<n>`, `A<n>`, `E<n>`, `K<n>`), a real receipt from the reference checkout,
a star-impact rating, an effort rating and a fit rating, plus the first
concrete change in mini-ork.

## Required artifacts — declare EXACTLY these, and nothing else

The workflow writes these under `${MINI_ORK_RUN_DIR}`; do not invent absolute
paths in the target tree:

- `${MINI_ORK_RUN_DIR}/lens-growth.md`
- `${MINI_ORK_RUN_DIR}/lens-activation.md`
- `${MINI_ORK_RUN_DIR}/lens-ecosystem.md`
- `${MINI_ORK_RUN_DIR}/lens-capability.md`
- `${MINI_ORK_RUN_DIR}/context-growth_lens.json`
- `${MINI_ORK_RUN_DIR}/context-activation_lens.json`
- `${MINI_ORK_RUN_DIR}/context-ecosystem_lens.json`
- `${MINI_ORK_RUN_DIR}/context-capability_lens.json`
- `${MINI_ORK_RUN_DIR}/synthesis.md`

## Acceptance

- AC1 — all four lens reports and `synthesis.md` exist in the run directory.
- AC2 — each lens report has the five sections in order: Evidence Read,
  Candidate Features, Ranked Shortlist, mini-ork Gap Analysis, Open Questions.
- AC3 — each lens proposes at least six candidates, each with an evidence
  receipt, a star-impact rating, an effort rating, a fit rating and a first
  step; ids use the lane prefix.
- AC4 — each lens writes its JSON sidecar with candidates, shortlist,
  gap_analysis, open_questions and read_only_attestation; sidecar ids match the
  report and the shortlist ranks every id.
- AC5 — the four lenses were served by four distinct model families.
- AC6 — the synthesis has the seven required sections in order and 8–12 ranked
  top items; every `sources` id exists in a lens sidecar.
- AC7 — no candidate is silently dropped: every candidate id appears somewhere
  in the synthesis, in the top plan or in `Rejected Or Not Worth It`.
- AC8 — no file under the reference checkout or `$MO_TARGET_CWD` is modified.

## Out of scope

- Implementing any candidate feature.
- Editing recipes, code, docs or configuration in either repository.
- Recommending that mini-ork become a personal assistant, a chat product, or a
  hosted service.
- Speculating about star counts without a causal mechanism.

## Verification command

`python3 ${MINI_ORK_ROOT}/recipes/star-growth-panel/verifiers/lens-completeness.py`
