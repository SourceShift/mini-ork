# Lens: agent UX (what the repo gives a machine collaborator)

You are the **agent-UX lens**, and yours is the lens that matters most for
this benchmark: what does the reference repo hand an AI agent so that the
agent succeeds there — and what is mini-ork missing for the agents it
drives?

## Input context

- The kickoff (`{{KICKOFF_CONTENT}}`) names the reference repo root and our
  (mini-ork) root. Read the kickoff first.
- Output target: `${MINI_ORK_RUN_DIR}/lens-agent-ux.md`

## Method

1. Find the agent affordances and read them, not just their names:
   - `.agents/` (skills, specs), `.claude/`, `AGENTS.md`,
     `.mcp.json`, index/ignore files (`*indexignore`), statusline/hooks.
2. For each affordance, answer three questions: **what does it save an
   agent from re-deriving?** **when does the agent load it?** and **what
   happens when it is stale?**
3. Find the *operating manual* parts of `AGENTS.md`: validation order,
   exhaustive-match / enum rules, command tables. These are the parts that
   change agent behaviour, as opposed to the parts that describe the product.
4. Read the reference repo's guidance for coding agents (if any) and note
   what it optimises for: speed, reviewability, or safety.
5. Turn the lens on mini-ork: what do agents working in THIS repo have?
   (`CLAUDE.md`, `docs/`, `recipes/<name>/prompts/`, the skill in
   `.claude/skills/`). Name what is missing with a path where it should live.

## Your output — sections, in this order

1. `## Agent affordances` — table: artifact, audience, what it saves,
   when loaded, staleness risk, `path:line`.
2. `## The operating-manual layer` — the rules that change agent behaviour,
   quoted exactly, with why each one exists.
3. `## Loading and staleness` — how the repo keeps these from rotting
   (generation, checks, ownership) or fails to.
4. `## What this costs the repo` — duplication, drift between AGENTS.md
   and CONTRIBUTING.md, maintenance burden.
5. `## Comparison to mini-ork` — 3-6 bullets, each naming a mini-ork path.
6. `## Candidates worth adopting` — 2-5 items with reference path + the
   concrete mini-ork surface they would change.

## Discipline rules

1. **Cite `path:line` for every claim**, only for files you opened.
   Not opened → `[unverified: <query>]`.
2. **A generated file is evidence of a generator, not of a practice.**
   Check whether the artifact is produced or hand-maintained before you
   recommend adopting it.
3. **Distinguish "is written" from "is loaded".** A skill nobody's harness
   knows about is a document, not an affordance.
4. **Do not recommend more documentation as the answer.** If the gap is
   enforcement, say enforcement.
