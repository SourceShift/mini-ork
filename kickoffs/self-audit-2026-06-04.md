# Refactor-Audit: mini-ork framework code review (2026-06-04)

**Task class:** refactor_audit
**Audit type:** scalability + tech debt + architecture review + code review
**Target:** `/Users/admin/ps/mini-ork` — the mini-ork framework itself.

This is a SCALABILITY AUDIT + ARCHITECTURE REVIEW + TECH DEBT scan + CODE
REVIEW of the mini-ork framework codebase. NOT a delivery; NOT a fix
session. The recipe produces audit findings only — pure refactor-audit
shape per recipes/refactor-audit/task_class.yaml.

---

## Audit scope

Refactor + tech-debt + code-review of the mini-ork lib/, bin/, recipes/,
schemas/, migrations/ + documentation. Six audit axes (each lens decides
prioritization):

### Axis 1: lib/ correctness (substrate-level audit)

The 13+ shell libraries that constitute the v0.2 substrate. Refactor-audit
for: SQL column drift, error-handling gaps, silent-die failure modes,
schema mismatches with migrations, tightly-coupled mid-pipeline assumptions.

### Axis 2: bin/ universal-loop runtime architecture review

`mini-ork`, `mini-ork-execute`, `mini-ork-eval`, `mini-ork-improve`,
`mini-ork-plan`, `mini-ork-classify`, `mini-ork-init`, `mini-ork-metrics`.
Architecture review for: inconsistent flag handling, missing `--help`
text, error-path gaps, exit-code semantics that surprise callers.

### Axis 3: recipes/ refactor pass

8 recipes total. Look for: redundant prompt patterns, lens-stance
collapse, recipes that share prompts without intent (anti-coalition
per Rajan 2025 ρ < 0.25 requirement).

### Axis 4: schemas + migrations drift audit

The canonical state.db schema + migration sequence. Cross-reference each
`bin/*.sh` SQL against `PRAGMA table_info()` on a fresh init'd state.db.
This is the pt-33..36 9-bug pattern that already cost real iteration.

### Axis 5: scalability + tech-debt audit on documentation honesty

README.md + ROADMAP.md + docs/positioning. Refactor-audit for: claims
that have NOT been LIVE-validated (e.g. Phase E "empirically closed at
dry-run" — is the claim still accurate after recent commits? does
positioning grounding cite papers that actually justify the claim?).
Flag any claim with missing evidence or contradicted by recent commits.

### Axis 6: multi-agent honesty-gap inventory (positioning-doc honest-gaps)

- Krippendorff α calibration gate (Nasser 2026) — present? wired?
- Adversarial fabricated-bug injection (Agarwal 2026) — present?
- Wireheading enforcement (validator-cites-files check) — present?
- Honest confidence intervals (Dai 2025) — present?

---

## Definition of Done

The recipe will emit:
- 4 per-lens audit reports at `~/.mini-ork/runs/<run_id>/<lens_name>-report.md`
- 1 synthesizer audit report at `~/.mini-ork/runs/<run_id>/synthesis-report.md`
- 1 published canonical audit doc at
  `docs/_meta/audits/20260604-self-audit-via-4-lens-panel.md`

Each lens MUST cite concrete file:line ranges for every finding.

---

## Constraints

- **No code mutation** — audit-only. Lenses produce REPORTS, not patches.
- **Heterogeneous-family precondition** — recipe routes 4 lenses to 4
  DISTINCT model families (glm=Zhipu, kimi=Moonshot, codex=OpenAI,
  minimax=MiniMax). Synthesizer is opus (Anthropic). 5 distinct
  families satisfies Rajan 2025 submodular-utility precondition.
- **Cost budget** — `MO_DAILY_BUDGET_USD=15` for the full run.

---

## Per-lens stance hints

- **glm_lens** — tactical bottleneck / hot-path / common-case bugs.
- **kimi_lens** — code-level refactor opportunities, naming clarity,
  dead code, idiomatic patterns.
- **codex_lens** — LLM dispatch + provider routing + telemetry +
  cost-accounting paths.
- **minimax_lens** — architectural shape / cross-component coupling /
  whole-system observations (architecture review stance).

---

## Why this audit matters

mini-ork claims to be a "self-improving" framework. The strongest
demonstration is the framework auditing ITSELF via its own recipes,
with the auditing models explicitly heterogeneous (satisfying its own
positioning-doc precondition).

This is dispatch #5 in the LIVE panel-mode lineage. Prior 4 were
libwit-side panels via `.agentflow/mini-orch/`; this one is
upstream-side via `~/ps/mini-ork/bin/mini-ork run`.

---

## Synthesizer instructions (opus meta-reviewer)

1. Cross-tabulate findings: which findings did ≥ 2 lenses surface
   (cross-family agreement = high signal per Rajan 2025)? Which are
   single-lens-only (lower signal but possibly material)?
2. Rank by IMPACT × LIKELIHOOD, not just count of mentions.
3. For each finding, include: severity (P0-P3), evidence anchor
   (file:line), suggested fix-shape (≤ 1 paragraph), estimated effort.
4. Surface contradictions between lenses honestly.
