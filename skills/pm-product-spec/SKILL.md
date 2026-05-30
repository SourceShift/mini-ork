---
name: pm-product-spec
description: Research-driven product spec walker. Use when evaluating whether a FE page satisfies its audience+value-prop given best-in-class competitor designs. Invokes WebSearch to find current state-of-art and is empowered to propose UI redesigns, not just patches against current implementation.
---

# pm-product-spec — proactive product PM

Most PM workflows in this repo are reactive: walk current UI, find broken
data-testids, file tickets. This skill is the OPPOSITE: research what the
page SHOULD do for the target audience, compare to current, propose
redesigns + file tickets that bridge the gap (FE OR BE).

## Inputs (required)

The caller passes via `--add-dir` and prompt args:

1. `docs/product/audience.md` — personas the product targets
2. `docs/product/value-prop.md` — core promises across product
3. `docs/product/pages/<page>.md` — page-specific brief (purpose, MUST-show,
   MUST-NOT, BE contract)
4. Latest pm-audit walker JSON for this page (rendered DOM inventory) at
   `.agentflow/runs/_pm-audit/<latest>/walker/<route>.json`
5. Current BE response shape (a sample fetch) — caller pre-runs curl + jq
   so the prompt has concrete data, not a guess

## Workflow

### Step 1 — competitor research (Perplexity sonar via MCP, ~3-5 queries)

**Use `mcp__perplexity-mcp__*` tools as the PRIMARY research mechanism.**
Perplexity sonar returns sourced answers with citations, much higher
signal for product research than raw search-engine pages. Fall back to
WebSearch only if Perplexity is unavailable.

Search current state-of-art for THIS page's problem:
- "best knowledge graph visualization tools 2026 + UX patterns"
- "how Obsidian / Roam / Notion render concept maps"
- "research-app empty state UX patterns 2026"
- "academic-paper navigation graph design"
- "value-prop UX patterns for <audience-from-brief>"

Cite specific apps + UX patterns observed. Capture the Perplexity citation
URLs in the JSON output's `ideal_spec[].reference` field — they're auditable.

Goal: build a target spec NOT constrained by what we currently render. If
Perplexity surfaces a paradigm shift (e.g. "every modern research-app has
abandoned force-directed for hierarchical clusters since 2024"), reflect
that in the ideal_spec — don't anchor to current implementation.

### Step 2 — synthesize ideal spec

For the page being audited:
- Restate audience + value-prop intersection in 1 sentence
- List 5-8 features the BEST app would have, ranked by audience value
- For each: which competitor does it well + screenshot/description if available
- Mark features as `MUST` (audience can't function without it), `SHOULD` (lifts
  perceived quality), `COULD` (delighter)

### Step 3 — diff against current

Compare the ideal spec to:
- The brief's existing MUST-show list — flag MUSTs we identified that brief
  doesn't have (suggest brief update)
- The walker's rendered DOM inventory — flag MUSTs from brief AND ideal that
  are NOT rendered today
- The BE response shape — flag fields the ideal needs that BE doesn't return

### Step 4 — propose tickets / epics

Output strict JSON (caller will parse + insert):

```json
{
  "page": "PageGraph",
  "research_summary": "1-2 sentences citing the strongest reference + why",
  "ideal_spec": [
    { "feature": "...", "tier": "MUST|SHOULD|COULD", "audience": "researcher",
      "reference": "Obsidian Graph view does X", "value_evidence": "..." }
  ],
  "brief_updates": [
    { "section": "MUST show", "addition": "...", "rationale": "..." }
  ],
  "fe_tickets": [
    { "category": "missing_must_show|forbidden_render|redesign_proposal",
      "severity": "blocker|major|minor",
      "evidence": "what's missing or wrong, citing walker JSON",
      "fix_brief": "concrete steps including which competitor pattern to apply",
      "file_hint": "frontend/src/pages/PageX.tsx" }
  ],
  "be_tickets": [
    { "category": "contract_violation|endpoint_missing|missing_field",
      "severity": "blocker|major|minor",
      "evidence": "...",
      "fix_brief": "...",
      "file_hint": "server/routes/...",
      "endpoint": "/api/..." }
  ],
  "redesign_proposed": true | false,
  "redesign_rationale": "if true, why the current approach is fundamentally
                         wrong for the audience and the new approach"
}
```

## Constraints

- DO use WebSearch. The whole point is to escape current-impl bias.
- DO propose redesigns when warranted. If current approach is wrong for
  audience, say so + propose alternative — don't paper over.
- DO assume brief is incomplete. The brief is a starting point; suggest
  additions when research surfaces value the brief missed.
- DO NOT modify any file. This skill is read-only + emits JSON.
- DO NOT cite features without an evidence reference. Every MUST item needs
  competitor cite OR direct audience-need rationale.
- DO NOT repeat what the walker already filed via pm-audit (those are
  data-testid / dead-button defects, separate concern).
- LIMIT WebSearch to ~5 queries — quality over coverage.

## Output

Strict JSON only on stdout. Caller pipes to jq. Any narration goes to stderr.

## Calibration anchors

- "missing empty state" → `missing_must_show` blocker
- "node labels show UUIDs" → `forbidden_render` blocker
- "graph layout doesn't scale past 200 nodes" → `redesign_proposal` major
- "BE returns nodes without sourceCitation" → BE `missing_field` blocker (per
  trust contract in value-prop)
- "could add minimap" → `COULD` delighter, NOT a ticket unless competitor
  evidence + audience pain are both clear
