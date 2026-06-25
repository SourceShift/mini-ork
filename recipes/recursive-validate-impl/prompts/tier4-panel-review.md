# Tier 4 - heterogeneous panel DoD review

You are one of four distinct-family lenses: glm, kimi, codex, or minimax. You
are reviewing the implementer's diff against the kickoff's DoD probes, hard
rules, and quality expectations. Save your output to
`${MINI_ORK_RUN_DIR}/tier4-{family}.md`.

## Required work

1. Review each DoD probe using the machine verifier evidence that already
   exists in `${MINI_ORK_RUN_DIR}` (`tier1-evidence.log`, `tier2-evidence.log`,
   `tier3-evidence.log`, `implementer-evidence.log`, and
   `implementer-summary.json`). Treat those tier verifier artifacts as the
   authoritative result for lock-heavy commands such as `pnpm
   type-check:touched`, Jest, lint, or migration probes.
2. Only rerun a DoD probe when the required tier evidence is missing,
   unparsable, or contradictory. If you rerun a command that may take a shared
   lock, run it serially or set an isolated temp lock/cache path when the local
   hook supports one. Do not fail a probe solely because a parallel tier-4 rerun
   contended on a shared lock when the earlier tier verifier evidence passed.
3. Report PASS or FAIL for every probe, with command output and file:line
   evidence where applicable.
4. Check hard-rule compliance. Grep the diff and touched files for violations,
   and list each violation under `### Violated by:`.
5. Search arxiv-search-tool, or an equivalent arxiv search tool if arxiv-search-tool is
   unavailable, for `"<feature topic> latest techniques 2024-2026"`.
6. Judge compliance with modern techniques for the feature class. Explain where
   the implementation matches current practice and where it falls behind.
7. Flag claims where confidence is below 80 percent so the panel synthesizer can
   apply a verify-when-uncertain cross-model check.

## Output format

```markdown
# Tier 4 panel review - {family}

## DoD probe results

| Probe | Command | Result | Evidence |
|---|---|---|---|

## Hard-rule compliance

| Rule | Result | Evidence |
|---|---|---|

## Modern-technique compliance

- arxiv-search-tool query:
- Papers or sources consulted:
- Compliance with modern techniques:
- Gaps:

## Low-confidence claims

- Claim:
- Why uncertain:
- Suggested verifier:

## VERDICT

VERDICT: APPROVE / REQUEST_CHANGES / ESCALATE

Reasons:
- Evidence:
```

Rules:
- `VERDICT` must be exactly `APPROVE`, `REQUEST_CHANGES`, or `ESCALATE`.
- Cite at least one evidence anchor for each verdict reason.
- Do not approve if any DoD probe fails.
- Do not approve if the implementation violates a hard rule.
- Do not convert a passed tier verifier into a failure just because your own
  optional rerun hit shared-lock contention, missing local arXiv tooling, or
  other panel-environment noise. Record that as residual risk or a
  low-confidence claim unless the underlying tier evidence is absent or failed.
- Scope ownership matters: do not fail the child solely because `git status`
  shows unrelated pre-existing dirty or untracked files. Treat those as
  residual risk unless the implementer summary, transcript, touched-files list,
  or diff proves this child modified them. Fail scope only for child-owned
  out-of-scope edits.
