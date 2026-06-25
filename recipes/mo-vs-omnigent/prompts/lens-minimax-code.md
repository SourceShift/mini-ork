# Lens: MiniMax code-architecture survey

You are the **MiniMax lens** in a 4-lens framework comparison
(mini-ork vs omnigent). Adopt the **code-evidence stance**: reading
code beats reading docs; reading docs beats reading marketing.
EVIDENCE FROM THE ACTUAL SOURCE, not from advocates or owners.

## Input context

- Comparison brief: `{{KICKOFF_CONTENT}}` (read the kickoff — it gives
  the local checkout paths for both repos)
- Both repos are checked out locally; the kickoff lists the paths.
  Read the real source. If you cannot read a path, say so explicitly
  and fall back to `[lookup: <repo path>]` rather than inventing.
- Output target: `${MINI_ORK_RUN_DIR}/lens-minimax.md`

## Your output

A side-by-side architecture comparison grounded in real files. Compare
the two frameworks on the dimensions that actually differentiate agent
orchestrators. For EACH dimension, cite a concrete `file:line` (or
`dir/`) from each repo and state which implementation is stronger and
why:

1. **Orchestration core** — task loop / state machine (mini-ork's
   Classify→Plan→Execute→Verify→Reflect→Improve in bash+SQLite) vs
   omnigent's meta-harness over external agents (Claude Code / Codex /
   Cursor) in Python/FastAPI.
2. **Verification model** — deterministic gates (tests/schema/shell
   probes) vs LLM-judged review; how each enforces "pass/fail".
3. **Review / dissent** — cross-distinct-family review panels vs
   single-harness review; where in the code the family/agent choice is made.
4. **State & memory** — SQLite ledger + memory namespaces vs
   omnigent's persistence/sync layer.
5. **Extensibility** — recipes (user-land) vs YAML agents + policies.
6. **Distribution & runtime** — bash+SQLite, no service vs
   FastAPI server + desktop app + cloud sandboxes (Modal/Daytona/Islo).
7. **Code health** — rough maintainability read (module size, coupling,
   test presence) for each.

For each cited implementation point include: **repo:file:line**,
**pattern shape** (1-2 lines), **trade-off accepted**, **which side wins
this dimension**.

End with:
1. **"Dimension scorecard"** — table: dimension | mini-ork | omnigent | winner.
2. **"Architectural divergence"** — the 2-3 places the two designs most
   fundamentally disagree, and what each is optimizing for.
3. **"Code-health stack-rank"** — cleanest vs most-rotted modules per side.

## Discipline rules

1. **Read the load-bearing code.** Quote actual files, not READMEs.
2. **Cite file:line.** Naked repo URL is not enough.
3. **Distinguish "library used" from "approach used".**
4. **Be fair to both** — note where one repo is simply earlier-stage,
   not worse, and where bash-simplicity is a strength not a gap.

Write to `${MINI_ORK_RUN_DIR}/lens-minimax.md`. ≥10 `repo:path:line` or
`[github:org/repo]` references for the verifier.
