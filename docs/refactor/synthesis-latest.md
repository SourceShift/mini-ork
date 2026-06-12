# Synthesis — run-1781279430-19794 (FAIL-FAST: under quorum)

**Status:** ❌ NO SYNTHESIS PRODUCED — panel collapsed to single in-scope lens. Per learned failure modes (workflow.recipe.feature-inventory-cmgk + workflow.recipe.refactor_audit), the synthesizer refuses to fabricate consensus from a single-lens result.

**Recipe:** `refactor-audit` (Chapter Writer personalization integration bug-hunt — 7 lanes)
**Quorum required:** ≥3/4 non-empty, in-scope lens artifacts.
**Quorum achieved:** **1/4 in-scope** (Opus). Hard fail.

---

## Section 0 — Panel state (truth table)

| Lens | File size | Substantive? | In-scope? | Status |
|---|---:|---|---|---|
| **lens-glm.md** | (absent) | ❌ | n/a | **Provider 429** — Fair-Usage rate-limit at `1781279670-glm.out`; `api_error_status: 429`, `output_tokens: 0`, no artifact written. |
| **lens-kimi.md** | 1578 B | ❌ | n/a | **Self-blocked** — Kimi correctly detected `plan_status: needs_answers` + `profile_status: needs_answers` and refused to produce findings. Output is a meta-acknowledgment ("unblock profile first"), not a 7-lane bug ledger. |
| **lens-codex.md** | 11819 B | ✅ | ❌ | **Scope drift** — 15 findings target `/Volumes/docker-ssd/ps/mini-ork/lib/*` (the orchestrator itself), NOT the kickoff's `server/services/bookGeneration/` etc. Findings are real and valuable for mini-ork v0.x+1, but they do not address the Chapter Writer personalization audit. |
| **lens-opus.md** | 36060 B | ✅ | ✅ | **Only valid lens** — bug-hunt against `bookOrchestratorContextFirst.ts` + `gepaCron.ts` etc. as the kickoff required. |

**Net in-scope quorum: 1/4.** A 4-lens audit with 1 valid lens cannot produce cross-lens consensus markers (★) — every finding would be marked single-source and the synthesis matrix would be a transcription of Opus.

---

## Section 1 — Root cause chain (why the panel collapsed)

```
profile_status: needs_answers
        │  (user has not answered "What command should prove this run succeeded?")
        ▼
plan_status: needs_answers ─────────► planner dispatch skipped
        │
        ▼
lens fan-out dispatched ANYWAY (recipe lacks plan-readiness gate)
        │
        ├─► GLM    : 429 Fair-Usage (provider-side)
        ├─► Kimi   : detected profile gap, self-blocked
        ├─► Codex  : ignored kickoff scope, audited mini-ork instead
        └─► Opus   : worked correctly
        │
        ▼
synthesizer dispatched at 1/4 in-scope ────► FAIL-FAST (this doc)
```

Three orthogonal failures stacked: **(1) provider outage** (GLM), **(2) recipe-level guard gap** (no plan-readiness gate before lens fan-out — Kimi correctly refused; the recipe should have refused first), **(3) lens prompt context bleed** (Codex audited the orchestrator binary instead of the target repo — likely the dispatch prompt over-emphasized `${MINI_ORK_*}` env vars and Codex grounded on the wrong working tree).

---

## Section 2 — Preserved findings (do not discard, do not promote)

The single in-scope lens (Opus, `lens-opus.md`, 36KB) emitted substantive findings against the 7 lanes. These are PRESERVED for the re-run, NOT promoted to "synthesis P0/P1/P2" because consensus signal is absent.

**Opus headline findings** (full bodies in `lens-opus.md` — read there, not here):

- **L1 — ContextEngine:** F-L1-1 `_capsuleStates` Map process-lifetime, no mtime invalidation (P1, `bookOrchestratorContextFirst.ts:138-140`); F-L1-2 `token_budget: 4000` hardcoded (P2, `:144-150`); F-L1-3 malformed `chapter_quality_snapshot` silently skipped — Zero-Fallback violation (P2, `:178-186`).
- **L2 — GEPA runtime:** F-L2-1 `MIN_RATED_EXPLANATIONS = 100` calibrated for explanation pipeline, book prompts use `book_chapter_feedback` (different source) → book-prompt eligibility gate always skips silently (P1, `gepaCron.ts:37-51`).
- **L3–L7:** see `lens-opus.md` (not transcribed here to avoid creating the illusion of synthesis from a single source).

**Codex's mini-ork findings** (`lens-codex.md`) are a separately useful artifact — file them under mini-ork's own audit backlog, NOT the chapter-writer audit. Headline cost-saving observations worth promoting upstream to `SourceShift/mini-ork`:

- D-11 + D-12 (★ self-consistent): quorum-gate + lens-completeness must run BEFORE synthesizer, not after — **this very run is the proof case**.
- D-04 + D-05 + D-06: retryable error classification without backoff/fuse/fallback turned the GLM 429 into a panel-killer when same-family fallback (deepseek) would have salvaged the lens.
- D-08: gradient extraction guard against `status=running` / vacuous traces — matches learned failure mode #2 and #3.

---

## Section 3 — Hardest open question

**Why did Codex's lens audit `mini-ork` instead of the researcher repo?**

Hypothesis A — context-pack contamination: the lens prompt injected `${MINI_ORK_RUN_DIR}` paths and Codex grounded its file scan there.
Hypothesis B — codex CLI working-directory drift: the worker spawn may have `cd`'d into the mini-ork lib tree.
Hypothesis C — kickoff scope_allow strings were not pinned to absolute paths in the lens prompt and Codex picked the path most-cited in the lens template instead.

Resolution: read `agent-codex_lens.transcript.json` for the actual file-read trail before re-dispatching. This is a recipe bug, not a Codex bug — and it directly affects what `lens-codex.md` will see on retry.

---

## Section 4 — Required actions to unblock (in order)

1. **Answer the profile question**: "What command should prove this run succeeded?" Strawman: `bash verifiers/lens-completeness.sh` (per success_criteria line 16) plus `npx tsc --noEmit -p tsconfig.server.json` on any code Opus says to touch in a follow-up impl run. Without this answer, every future re-run repeats the exact same Kimi self-block.
2. **Wait out GLM rate-limit OR configure fallback**: `lanes.glm_lens` needs a same-family fallback (e.g. `deepseek_lens`) before any re-dispatch, otherwise GLM 429 will block again. Codex finding D-06 is the canonical fix.
3. **Add plan-readiness gate to `refactor-audit/workflow.yaml`**: an edge condition that blocks lens fan-out when `plan_status != ready`. Without it, the recipe burns ~$5-15 on lens dispatches that Kimi correctly refuses.
4. **Pin Codex lens working tree**: pass `--cwd /Volumes/docker-ssd/Migration/Development/researcher` explicitly + scrub `${MINI_ORK_*}` env from the lens-prompt body so Codex cannot drift.
5. **Re-dispatch only failed lenses** (per failure-mode fix: "retry-failed-lenses subgraph"): keep `lens-opus.md`, re-run GLM + Kimi + Codex against the fixed prompts.

---

## Section 5 — Dogfood reflection

The audit caught itself failing **exactly the way the prior learned failure modes predicted**:

> [workflow.recipe.feature-inventory-cmgk] 3 of 4 lenses … failed to produce `lens-*.md` artifacts, yet the recipe still advanced to synthesizer … Single-lens output was treated as if it had multi-lens consensus.

This run did not improve on that — the synthesizer node fired despite quorum failure. The learned-failure-mode fix ("Add a hard quorum gate node between lens fan-out and synthesizer") is **NOT YET WIRED INTO THE RECIPE**. That is the highest-leverage recipe fix surfaced by this run.

Codex independently identified the same gap as findings D-11 + D-12 — a (★) **single-lens consensus** between this synthesizer's own meta-observation and the Codex lens. (A consensus of one is not consensus; it is corroboration.)

---

## Section 6 — How to re-run (after Section 4 actions)

```bash
# After profile is answered + GLM fallback configured + plan-readiness gate added:

cd /Volumes/docker-ssd/Migration/Development/researcher

# Re-dispatch failed lenses only (preserve lens-opus.md):
bash .mini-ork/recipes/refactor-audit/retry-failed-lenses.sh \
  --run-id run-1781279430-19794 \
  --lenses glm,kimi,codex

# Once 4/4 in-scope artifacts present:
bash .mini-ork/recipes/refactor-audit/synthesize.sh \
  --run-id run-1781279430-19794
```

**`retry-failed-lenses.sh` does not exist yet** — naming it explicitly here as the deliverable that closes learned failure-mode #1.

---

## Section 7 — Bottom line

No P0/P1/P2 fix list. No consensus markers. No severity × leverage matrix. The honest output of a 1-of-4 lens panel is "this audit did not happen yet". The work salvaged:

- `lens-opus.md` — 9 substantive in-scope findings, fully usable as a single-lens preliminary report. Treat as **draft**, not audit.
- `lens-codex.md` — 15 mini-ork cost-cut findings, off-topic for this audit but high-value upstream (file to `SourceShift/mini-ork`).
- This synthesis — recipe-level diagnostic + re-run runbook.

Estimated re-run cost: ~$8-15 (3 lenses × $2-5 each) once Section 4 actions complete.
