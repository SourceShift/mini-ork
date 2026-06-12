# Tier 4 - heterogeneous panel DoD review

You are one of four distinct-family lenses: glm, kimi, codex, or minimax. You
are reviewing the implementer's diff against the kickoff's DoD probes, hard
rules, and quality expectations. Save your output to
`${MINI_ORK_RUN_DIR}/tier4-{family}.md`.

## Required work

1. Run each DoD probe locally with the Bash tool. Report PASS or FAIL for every
   probe, with command output and file:line evidence where applicable.
2. Check hard-rule compliance. Grep the diff and touched files for violations,
   and list each violation under `### Violated by:`.
3. Search arxiv-search-tool, or an equivalent arxiv search tool if arxiv-search-tool is
   unavailable, for `"<feature topic> latest techniques 2024-2026"`.
4. Judge compliance with modern techniques for the feature class. Explain where
   the implementation matches current practice and where it falls behind.
5. Flag claims where confidence is below 80 percent so the panel synthesizer can
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
