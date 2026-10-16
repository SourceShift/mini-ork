# Decision: deterministic gate materialization replaces the test_author LLM node

Date: 2026-10-04 · Consensus: **3/3 unanimous for (a)** (Builder, Reviewer, Future-Maintainer; sonnet voters)

## Problem

`spec-driven-delivery`'s `test_author` LLM node authored `gates/<spec_id>.json`
before implementation. On the first real 34-spec campaign
(`run-sdd10x-202610040602`) it produced **1 of 34** gate files in 73 min at
**$30.73** — it live-explored the app per gate (high quality, unbounded cost).
Meanwhile the SpecCards' `gate.probe` fields are lossy LLM paraphrases, but the
ORIGINAL spec .md files are lint-required to carry executable ```bash fences —
the spec AUTHOR's own probes.

## Options

- (a) Pure deterministic extraction from the spec fences; no LLM; gaps fail
  loud into the spec-revision loop.
- (b) Sharded LLM test_author (one small call per spec, exploration banned).
- (c) Hybrid: deterministic first, LLM templating for probes failing an
  is-executable sniff.

## Votes (verbatim-condensed)

**Builder → (a).** Load-bearing risk: order/label fence→AC mapping is a
heuristic that misattributes silently on reordered specs; caught only
downstream. $30/gate is unbounded-cost-no-backpressure; (a) is grep-able,
diff-able, byte-reproducible, $0. (c) = two code paths + a sniff heuristic
that is itself a silent-failure source. Alt-if-wrong: (c) with the LLM pass
gated strictly and loudly logged.

**Reviewer → (a).** Load-bearing risk: fail-closed only protects integrity if
operators actually revise specs instead of hand-patching probes. (a) is the
only option honoring authorship separation in the strong sense: provenance =
human-authored lint-enforced bash → deterministic script, zero stochastic
step. test-validity checks probe *hygiene*, not *fidelity* — an LLM paraphrase
can test a weaker property and look green. (c) concentrates risk exactly where
fences are weakest and the fallback becomes the default under pressure.
Alt-if-wrong: constrained (c) that may only template the author's literal
tokens, never invent assertions.

**Future-Maintainer → (a).** Load-bearing risk: (a) assumes fence-per-AC
coverage the lint doesn't guarantee (lint requires ≥1 fence per SPEC). Over 20
campaigns the killer is silent nondeterminism — (b)/(c) re-roll gate wording
per campaign with no diff to point at. (a) converts the cost into a one-time
lint/spec investment that amortizes. Alt-if-wrong: (c) restricted to verbatim
templating of already-extracted text.

## Synthesis / chosen sub-option

`verifiers/gates-materialize.py` (verifier node `gates_materialize`, replacing
`test_author` in workflow.yaml):

- cmd/contract ACs: AC-labeled fence segment (with fence prelude) → else
  unlabeled fences joined → else ALL fences joined (spec-level verify script;
  duplicating one self-asserting script across its ACs is contract-legal and
  faithful).
- ui ACs: labeled segment → unlabeled `agent-browser` fences → deterministic
  template built ONLY from the author's literal `data-testid` + route tokens
  (the Reviewer/Maintainer "constrained templating" fallback, implemented
  deterministically rather than via LLM) → `unprobeable`, which fails the node
  into the spec-revision loop.
- expect: the probe's own printed success literal, else `exit 0` (fences are
  self-asserting; `is_exit_only` admits it). Source-hash drift fails the spec.
- Probe provenance recorded per row (`spec-fence` / `spec-literal-template`).

Maintainer's lint-gap risk accepted knowingly: first real campaign needed spec
revision r3 for exactly 4 of 118 ACs (2 specs) — the revision loop, not a
tooling gap. Hardening `specs lint` to warn on ui-ACs without browser fences
is the follow-up (see todo below).

## Decision-risk audit

| Risk | Owner mitigation |
|---|---|
| Fence→AC misattribution on odd specs | per-row provenance + test-validity broken-baseline still executes every probe |
| Operators hand-patch gates instead of specs | gates/ regenerates every run from source_hash-pinned specs; hand edits are overwritten |
| Spec-level script duplicated across ACs dilutes per-AC resolution | smoke-live still passes only when the whole script passes; per-AC ledger rows share the verdict honestly |

## Standards-based uplift

Standard: *spec-as-source-of-truth; verifier payload authoritative* (recipe
rule #2/#5). Gap: an LLM sat inside the gate-authorship chain. Fix: gates now
derive deterministically from the lint-enforced spec artifact; the only
remaining LLM authorship in the pipeline is the SpecCard compile, which
ratification + reclassification already gate.

## Follow-up

- `docs/todos/20261004-specdir-lint-ui-fence-warning.md` — add a lint WARNING
  for specs whose ui-shaped ACs have no `agent-browser` fence and no
  testid+route literals (would have caught r3's 4 ACs at authoring time).

## Addendum 2026-10-16 — the "lint requires ≥1 fence per SPEC" premise was false

The Options section and the Maintainer note above both rest on "lint requires
≥1 fence per SPEC". It did not. `NO_VERIFY_CMD` is satisfied by an *inline*
`` `command args` `` span (`mini_ork/specdir/lint.py`), so a spec with
acceptance criteria, no fence at all, and one backticked command linted clean.

Measured: both 2026-10-16 run-initiation corpora — 5 specs, 36 ACs, zero fences
— reported `lint clean (0 warning(s))` while `gates-materialize` built zero
probes and failed with one `NO_EXECUTABLE_FENCE` per AC. The
reflector→replanner loop cannot converge on that: it mutates SpecCard
`acceptance[].gate.probe`, which `gates-materialize` ignores, rebuilding from
the spec's fences every iteration. Two runs died on it; one was stopped
mid-loop (round 2 of 6) rather than left to burn to the iteration cap.

Landed `e13b67f2`: a `NO_EXECUTABLE_FENCE` WARNING that fires only when a spec
has ≥1 acceptance criterion and no shell fence with a body. Narrower than the
risk accepted above (that was about *per-AC* fence coverage): it enforces the
≥1-fence-per-spec rule this record believed was already enforced. Warning, not
error, so no existing corpus changes its `has_errors` result.

Still open: the per-AC gap is unchanged — a spec with one fence and nine ACs
still yields nine identical whole-spec probes. The follow-up below is unanswered
and its todo file is not present under `docs/todos/`.
