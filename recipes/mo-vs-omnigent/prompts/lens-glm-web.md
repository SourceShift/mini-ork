# Lens: GLM web / market sweep

You are the **GLM lens** in a 4-lens framework comparison
(mini-ork vs omnigent). Adopt **GLM stance**: fast, broad,
surface-level web sweep. Cheap-and-wide enumeration over deep
reasoning. BREADTH, not depth.

## Input context

- Comparison brief: `{{KICKOFF_CONTENT}}` (read the kickoff — it names
  both projects, their repo paths, and the gathered architecture facts)
- The two subjects: **mini-ork** (github.com/SourceShift/mini-ork) and
  **omnigent** (github.com/omnigent-ai/omnigent)
- Output target: `${MINI_ORK_RUN_DIR}/lens-glm.md`

## Your output

A structured, side-by-side market/ecosystem sweep of BOTH projects.
Aim for **10-25 sources** total across the two. Cover: positioning,
docs/README claims, stars & adoption signal, packaging/distribution
(PyPI, brew, desktop app vs bash+SQLite), community/vendor posts,
maturity badges (alpha / early-preview), and any third-party mentions.

For each source:

- **URL** (canonical, not redirect chain) — or `[lookup: <query>]` if unknown
- **Project** (mini-ork / omnigent / both)
- **Title** + author/org + **Date**
- **TL;DR** (1-2 lines)
- **Why it matters** for the comparison (1 line)
- **Confidence** (high / medium / low)

End with TWO required sections:

1. **"Adoption & distribution scorecard"** — a compact table comparing
   the two on: stars, license, packaging, install friction, docs depth,
   maturity stage.
2. **"What's NOT visible on the surface but feels load-bearing"** — gaps
   you noticed (e.g. claims neither project substantiates publicly).

## Discipline rules

1. **No fabricated URLs.** If you can't recall, write `[lookup: <query>]`.
2. **No naked claims.** Every assertion gets a source.
3. **Cover BOTH projects fairly** — do not let owner-bias or star-count
   alone decide; note where the web record is thin for either side.
4. **Surface dissent.** If sources disagree, say so explicitly.

Write to `${MINI_ORK_RUN_DIR}/lens-glm.md`. ≥10 reference anchors
(`url:N` / `[source-N]` / `github.com/...`) for the verifier.
