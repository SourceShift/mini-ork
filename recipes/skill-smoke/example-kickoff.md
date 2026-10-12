# Skill-registry smoke run

Probe the skill grant pipeline. Two researcher nodes: one granted the
`skill-smoke-demo` skill, one granted nothing. Follow the prompts exactly;
the deterministic verifier checks the marker isolation.

Required artifacts — declare EXACTLY these three in the plan's
artifact_contract (outputs), copied verbatim, and nothing else. All three
are run-dir sidecars written by the workflow itself. Do NOT declare any
path under the target working tree:

- ${MINI_ORK_RUN_DIR}/lens-granted.md
- ${MINI_ORK_RUN_DIR}/lens-control.md
- ${MINI_ORK_RUN_DIR}/verifier-result-skills-isolation.json
