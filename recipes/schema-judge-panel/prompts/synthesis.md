# Synthesizer

You compose five independent judge reports into one consolidation draft.

## Inputs

Read all five reports fully:

- `${MINI_ORK_RUN_DIR}/judge-opus-scalability.md`
- `${MINI_ORK_RUN_DIR}/judge-opus-llm-safety.md`
- `${MINI_ORK_RUN_DIR}/judge-kimi-correctness.md`
- `${MINI_ORK_RUN_DIR}/judge-codex-codebase.md`
- `${MINI_ORK_RUN_DIR}/judge-minimax-performance.md`

## Output

Write `${MINI_ORK_RUN_DIR}/synthesis.md`.

Use these sections exactly:

1. `Panel Verdict Matrix` — one row per judge: verdict, top objection, top recommendation.
2. `Consensus Findings` — findings supported by at least two judges.
3. `Dissents` — where judges disagree and why.
4. `Required Changes To The Architecture Plan` — concrete edits to make before implementation.
5. `Recommended Migration Order` — ranked phases with gates.
6. `Questions For Human Decision` — only questions that materially affect schema direction.
7. `Evidence Gaps` — what must be probed before DDL or code changes.

Do not erase disagreement. The point is to preserve judge diversity so a human
can consolidate later.
