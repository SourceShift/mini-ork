# 05 — LLM Council Panel Bias: Analysis and mini-ork Adoption Plan

**Date:** 2026-06-30
**Source analysed:** `/private/tmp/miniork-ref-analysis/llm-council`
**mini-ork files read:** `lib/coalition_gate.sh`, `lib/krippendorff_alpha_gate.sh`,
`lib/refute_or_promote_gate.sh`, `recipes/recursive-validate-impl/workflow.yaml`,
`recipes/recursive-validate-impl/prompts/tier4-panel-review.md`,
`recipes/recursive-validate-impl/prompts/tier4-panel-synthesizer.md`,
`recipes/recursive-validate-impl/verifiers/tier4-panel-quorum.sh`

---

## 1. The LLM-Council Flow — Concrete Code Walk

### Source map

```
backend/config.py       → COUNCIL_MODELS, CHAIRMAN_MODEL
backend/openrouter.py   → query_model(), query_models_parallel()
backend/council.py      → stage1_, stage2_, stage3_, run_full_council()
backend/main.py         → HTTP endpoint, streaming SSE wrapper
```

### Stage 1 — parallel fan-out

```python
# council.py:8-32
async def stage1_collect_responses(user_query):
    messages = [{"role": "user", "content": user_query}]
    responses = await query_models_parallel(COUNCIL_MODELS, messages)
    ...
```

`query_models_parallel` (openrouter.py:56-79) creates one async task per model and
awaits all with `asyncio.gather`. Every model in `COUNCIL_MODELS` (gpt-5.1, gemini-3-pro,
claude-sonnet-4.5, grok-4) receives the raw user query with no cross-visibility.
No shuffle, no position injection at this stage.

### Stage 2 — anonymized cross-review + ranking

```python
# council.py:35-112
labels = [chr(65 + i) for i in range(len(stage1_results))]  # A, B, C, D
label_to_model = {f"Response {label}": result['model']
                  for label, result in zip(labels, stage1_results)}

responses_text = "\n\n".join([
    f"Response {label}:\n{result['response']}"
    for label, result in zip(labels, stage1_results)
])
```

The ranking prompt presents all four responses as "Response A … Response D"
with NO model names. Every council member (including the one that produced that
response) reviews all of them under these neutral labels.

The prompt text enforces a strict output format:

```
FINAL RANKING:
1. Response C
2. Response A
3. Response B
```

`parse_ranking_from_text` extracts this section with a regex and falls back to
scanning the whole text for `Response [A-Z]` patterns.

Each model's ranking call is again fired in parallel via `query_models_parallel`.
The ranking prompt text — and therefore the ordering of responses A→D — is
**identical for every reviewer**. There is **no position randomisation**: response A
is always first.

### Rank aggregation — `calculate_aggregate_rankings`

```python
# council.py:211-254
for ranking in stage2_results:
    parsed_ranking = parse_ranking_from_text(ranking['ranking'])
    for position, label in enumerate(parsed_ranking, start=1):
        if label in label_to_model:
            model_positions[model_name].append(position)

aggregate.sort(key=lambda x: x['average_rank'])   # lower is better
```

This is a simple **Borda-style average-position aggregation**: each reviewer's
rank-1 contributes position=1, rank-4 contributes position=4. The aggregate is
the arithmetic mean across all reviewers. No weighting, no Condorcet, no
tie-breaking.

The aggregate ranking is written to `metadata["aggregate_rankings"]` and passed
to stage 3, but the chairman prompt does NOT force the chairman to follow it —
the prompt says "consider … the peer rankings and what they reveal about
response quality".

### Stage 3 — chairman synthesis

```python
# council.py:115-174
chairman_prompt = f"""
...
STAGE 1 - Individual Responses:
{stage1_text}            # ← model names RESTORED

STAGE 2 - Peer Rankings:
{stage2_text}            # ← contains raw ranking prose (model names visible again)
"""
response = await query_model(CHAIRMAN_MODEL, messages)
```

Critically, the chairman's prompt **de-anonymises**: it shows `Model: openai/gpt-5.1`
in both stage 1 and stage 2 sections. The chairman (gemini-3-pro) therefore
knows whose work it is reading when it synthesizes the final answer, reintroducing
the identity-bias that anonymization in stage 2 was meant to prevent.

---

## 2. Bias Mitigation Details — What the Council Does and Does Not Do

| Mechanism | Council does it? | Details |
|---|---|---|
| Answer anonymization (stage 2 reviewers) | Yes | Letter labels A-D; model names stripped from ranking prompt |
| Self-review included | Yes | Each model ranks its own answer alongside others (can't recognize it as its own) |
| Position randomization (shuffle A-D order) | **No** | Order follows insertion order of stage1_results dict, which follows COUNCIL_MODELS list order |
| Chairman anonymization (stage 3) | **No** | Model names restored in chairman prompt |
| Inter-rater agreement check | **No** | No α or ρ measurement |
| Family-diversity enforcement | **No** | Models chosen by config, no enforcement gate |
| Self-ranking exclusion | **No** | Models rank all responses including own (but can't identify it) |
| Aggregate used to constrain synthesis | **No** | Aggregate is metadata only; chairman ignores it by policy |

The council's anonymization is its central bias-mitigation contribution. The
positional-bias gap (A always wins because it's read first) and the chairman's
de-anonymization are the two clearest weaknesses.

---

## 3. mini-ork Current Panel Architecture

### Tier 4 workflow (from `workflow.yaml`)

```
tier4_glm  ─┐
tier4_kimi  ─┤  (parallel)  →  tier4_quorum  →  tier4_synth (reviewer/opus)
tier4_codex ─┤
tier4_minimax┘
```

- **`tier4-panel-review.md`** — each lens reads the SAME implementer artifacts
  (`tier1-evidence.log`, `tier2-evidence.log`, etc.) and produces its own verdict
  file (`tier4-glm.md`, etc.). Lenses do NOT read each other's reports.
- **`tier4-panel-synthesizer.md`** — the synth node (reviewer lane, currently
  opus) reads all four files with model names visible in file paths
  (`tier4-glm.md` → family=zhipu is in the filename). The synthesizer prompt
  includes composition rules but no instruction to ignore lens identity.
- **`tier4_quorum` verifier** — counts present and non-empty lens files;
  passes if ≥ `MO_TIER4_QUORUM` (default 3 of 4). Addresses I-1 (missing-lens
  stall) but not bias.

### Existing gates and what they catch

| Gate | File | What it catches | What it misses |
|---|---|---|---|
| `coalition_gate` | `lib/coalition_gate.sh` | ρ ≥ 0.25 (correlated outputs), family_count < lens_count | Reviewer positional bias; intra-stage cross-review |
| `krippendorff_alpha_gate` | `lib/krippendorff_alpha_gate.sh` | Low inter-rater agreement (α < 0.4) across lens_scores in panel-verdict.json | Bias in HOW scores are assigned; order effects |
| `refute_or_promote_gate` | `lib/refute_or_promote_gate.sh` | Hallucinated fabrication markers surviving into findings | Systematic reviewer preference for certain families |
| `tier4_quorum` verifier | `verifiers/tier4-panel-quorum.sh` | Missing/empty lens reports (I-1 quorum failure) | Systematic bias in the synth node (I-6) |

**Gap the council addresses that mini-ork does not**: The council's stage 2
creates a cross-review signal — each model's assessment of the OTHER models'
work. mini-ork's lenses never read each other. The synth node receives four
independent verdicts and must judge their relative weight without any
cross-validated ranking. This is where **I-6 (reviewer bias)** lives: the
synthesizer may weight one lens more heavily based on name/family, not on
peer-validated quality.

---

## 4. Adoption Plan — Concrete Changes

### 4a. Anonymized Cross-Review Between Lenses

**What to add:** A new library `lib/panel_cross_review.sh` and a new
`tier4_cross_review` node that fires after all four tier-4 lens reports exist
but before `tier4_quorum`.

**Mechanism:**

1. Read all available `tier4-{family}.md` files from `$MINI_ORK_RUN_DIR`.
2. Assign letter labels (A–D), randomly permuting the order each run
   (`shuf` or `python3 -c "import random; ..."`). Store the mapping in
   `$MINI_ORK_RUN_DIR/panel-label-map.json`.
3. Build an anonymized cross-review prompt that presents each report as
   "Lens Report A … Lens Report D" with the family name stripped, asking
   each lens to rank the others. (The lens reviews all reports including
   its own, but cannot identify its own because the file contents have
   no self-identifying header beyond family-specific phrasing — this
   is imperfect but mirrors the council's approach.)
4. Run the cross-review fan-out in parallel: dispatch the same anonymized
   ranking prompt to all four lenses via the existing `dispatch.sh`
   machinery with model lanes `glm_lens`, `kimi_lens`, `codex_lens`,
   `minimax_lens`.
5. Write each ranking to `$MINI_ORK_RUN_DIR/tier4-xrank-{family}.md`.
6. Call `lib/rank_aggregate.sh` (see 4c) to compute the Borda aggregate.
   Write output to `$MINI_ORK_RUN_DIR/panel-rank-aggregate.json`.

**Workflow change in `recipes/recursive-validate-impl/workflow.yaml`:**

```yaml
# Add after tier4_minimax, before tier4_quorum:
- { name: tier4_cross_review, type: researcher,
    model_lane: all_lenses_parallel,         # new multi-lane entry
    prompt_ref: prompts/tier4-cross-review.md,
    dispatch_mode: parallel, gates: [budget_gate] }

# Edges:
- { from: tier4_glm,          to: tier4_cross_review, edge_type: supplies_context_to }
- { from: tier4_kimi,         to: tier4_cross_review, edge_type: supplies_context_to }
- { from: tier4_codex,        to: tier4_cross_review, edge_type: supplies_context_to }
- { from: tier4_minimax,      to: tier4_cross_review, edge_type: supplies_context_to }
- { from: tier4_cross_review, to: tier4_quorum,       edge_type: supplies_context_to }
```

**New prompt `prompts/tier4-cross-review.md`:**

```markdown
# Tier 4 — anonymized cross-review and ranking

You are reviewing N anonymized lens reports that assessed the same implementation.
The reports are labelled A, B, C, D — you do not know which family wrote which.

Read `${MINI_ORK_RUN_DIR}/panel-anon/lens-{A,B,C,D}.md`.

Rank the reports from most to least rigorous.  Output FINAL RANKING as:
1. Lens A
2. Lens C
...

Then explain in 2-3 sentences what distinguished the top-ranked report.
Write to `${MINI_ORK_RUN_DIR}/tier4-xrank-{YOUR_FAMILY}.md`.
```

The anonymization step (stripping family names from lens report headers before
writing to `panel-anon/lens-X.md`) belongs in `lib/panel_cross_review.sh`.

**Connects to:** I-1 (cross-review adds a second quorum dimension — if fewer
than 3 cross-rankings arrive, `tier4_quorum` can count them as missing), I-6
(anonymization removes family-identity signal during peer review).

---

### 4b. Position Randomization and Bias Controls in the Arbiter

**Problem:** `tier4_synth` currently reads lens reports in a fixed order
determined by the prompt template literal. If the synthesizer has positional
recency/primacy bias, the family whose report appears first/last in the prompt
will be systematically over-weighted.

**Changes in `lib/panel_permuter.sh` (new, ~50 lines):**

```bash
mo_permute_panel_inputs() {
  local run_dir="${1:?}"
  local families=(glm kimi codex minimax)
  # Fisher-Yates via python3 random.shuffle — reproducible with PANEL_SEED
  local seed="${PANEL_SEED:-$(date +%s%N)}"
  local order
  order=$(python3 -c "
import random, sys
f = sys.argv[1:]
random.seed(int(sys.argv[0]))
random.shuffle(f)
print(' '.join(f))
" "$seed" "${families[@]}")
  # Write shuffled order to run_dir so the synth prompt includes it
  echo "$order" > "${run_dir}/panel-input-order.txt"
  echo "$seed"  > "${run_dir}/panel-seed.txt"
}
```

The synthesizer node runner reads `panel-input-order.txt` and constructs the
prompt by concatenating lens reports in that order — not alphabetically by
family name. The synthesizer never sees "glm first, kimi second"; it sees
whichever random order was drawn.

**Additional bias controls for the synth prompt
(`prompts/tier4-panel-synthesizer.md`):**

Add this rule block:

```markdown
## Bias controls (MANDATORY)

- The lens reports are presented in random order. Do not infer quality
  from position (first/last).
- A cross-review ranking aggregate is in `panel-rank-aggregate.json`.
  Use it as a signal — it represents peer assessment of rigor, not
  your own judgment of family.
- Cite only evidence anchors (file:line, command output) — never cite
  "the kimi report says" as evidence. Cite what kimi's report CONTAINS.
- Hard-rule violations: ANY single lens flagging a violation triggers
  panel-level violation regardless of other lenses (per existing rule).
  Do NOT override this with aggregate ranking.
```

**Connects to:** I-6 (systematic reviewer bias in synth).

---

### 4c. Rank Aggregation in the Arbiter

**New library `lib/rank_aggregate.sh`:**

Reads `tier4-xrank-{glm,kimi,codex,minimax}.md` files, parses the
`FINAL RANKING:` sections (same regex as llm-council's `parse_ranking_from_text`),
and computes Borda counts across all participating reviewers.

```bash
mo_aggregate_panel_ranks() {
  local run_dir="${1:?}"
  local label_map="${run_dir}/panel-label-map.json"
  local xrank_dir="${run_dir}"
  # Python inline: parse FINAL RANKING sections, average positions,
  # map labels back to families via label_map, emit JSON.
  python3 - "$run_dir" "$label_map" <<'PY'
import json, re, os, sys

run_dir, label_map_path = sys.argv[1], sys.argv[2]
with open(label_map_path) as f:
    label_to_family = json.load(f)  # {"A": "glm", "B": "codex", ...}

family_positions = {}
for reviewer in ["glm", "kimi", "codex", "minimax"]:
    xrank_file = os.path.join(run_dir, f"tier4-xrank-{reviewer}.md")
    if not os.path.isfile(xrank_file):
        continue
    text = open(xrank_file).read()
    if "FINAL RANKING:" not in text:
        continue
    section = text.split("FINAL RANKING:")[1]
    ranked = re.findall(r'\d+\.\s*Lens ([A-Z])', section)
    for pos, label in enumerate(ranked, start=1):
        family = label_to_family.get(label)
        if family:
            family_positions.setdefault(family, []).append(pos)

aggregate = []
for family, positions in family_positions.items():
    aggregate.append({
        "family": family,
        "average_rank": round(sum(positions) / len(positions), 3),
        "borda_score": sum(len(family_positions) + 1 - p for p in positions),
        "review_count": len(positions),
    })
aggregate.sort(key=lambda x: x["average_rank"])
print(json.dumps({"aggregate_rankings": aggregate}, indent=2))
PY
}
```

Output: `$MINI_ORK_RUN_DIR/panel-rank-aggregate.json`

**Feeding the aggregate into `tier4_synth`:** The synthesizer prompt already
references `${MINI_ORK_RUN_DIR}` for all its inputs. Adding a line to the
synthesizer prompt:

```markdown
Read `panel-rank-aggregate.json` for the Borda-count peer ranking. When
two lenses give contradictory verdicts on a DoD probe, the lens with the
lower average_rank (higher peer esteem) should be treated as the stronger
signal, unless the other lens has direct command output evidence.
```

This is a soft signal — it informs but does not override the existing hard
rules (any single ESCALATE triggers panel-level ESCALATE; any hard-rule
violation is absolute). It directly addresses the "how to weight conflicting
lenses" ambiguity in the current synthesizer.

**Mapping to `krippendorff_alpha_gate`:** The gate currently reads
`panel-verdict.json`'s `lens_scores` field. The cross-review rankings can
be converted to numeric scores (1 = rank 1 = score N, rank N = score 1)
and written into `lens_scores` so the existing α gate measures inter-rater
agreement of the CROSS-REVIEW, not just the primary review. This closes the
loop: if α is low, it means reviewers disagree even on which other reports
are high-quality — a strong signal to escalate.

---

## 5. Gap Summary — Council vs mini-ork

| Gap | Council's approach | mini-ork current | Adoption change |
|---|---|---|---|
| No cross-review between lenses | Stage 2 every lens ranks others | Lenses review artifact only | Add `tier4_cross_review` node + `lib/panel_cross_review.sh` |
| Positional bias in synth | Not fixed (known gap) | Fixed order by family name | Add `lib/panel_permuter.sh`, shuffle before synth |
| Chairman/synth sees model identity | De-anonymized in stage 3 (gap) | Synth sees family names in filenames | Strip family IDs from report headers in `panel-anon/` shadow dir |
| Rank aggregation not used by synth | Aggregate passed as metadata only | No ranking at all | Add `lib/rank_aggregate.sh`, feed Borda scores into synth prompt |
| No α on cross-review signal | N/A | `krippendorff_alpha_gate` runs on primary scores | Write cross-review ranks as `lens_scores` in `panel-verdict.json` for α gate |
| Quorum covers cross-review | N/A (no cross-review) | `tier4_quorum` counts primary reports only | Extend `tier4_quorum` to also count xrank files (or separate quorum verifier) |

---

## 6. Issues Addressed

- **I-1 (lens quorum failure):** The new cross-review node adds a second
  dimension: `tier4_quorum` can require both primary report quorum (≥3 of 4)
  and cross-review quorum (≥2 of 4 xrank files). Missing xrank files are already
  caught by the same size-check pattern in `tier4-panel-quorum.sh`.

- **I-6 (reviewer bias in synth):** Three changes combine:
  (a) anonymized cross-review removes family identity during peer assessment;
  (b) `lib/panel_permuter.sh` removes positional primacy/recency in the synth
  prompt; (c) Borda aggregate gives the synth a peer-validated quality signal
  to break ties without guessing by family name.

---

## 7. Implementation Sequence (Low-Risk Order)

1. **`lib/rank_aggregate.sh`** — pure Python, no workflow changes, low risk.
   Wire into `tier4_synth` runner as a pre-prompt step that writes
   `panel-rank-aggregate.json` from any existing `tier4-xrank-*.md` files
   (initially empty → aggregate is absent → synth falls back gracefully).

2. **`lib/panel_permuter.sh`** — modifies how synth prompt is assembled.
   Add `PANEL_SEED` env knob for reproducibility in tests. Wire into
   `tier4_synth` dispatcher. Zero change to workflow YAML.

3. **`prompts/tier4-panel-synthesizer.md`** — add bias-control block and
   reference to `panel-rank-aggregate.json`. Affects prompt text only, no
   code change.

4. **`lib/panel_cross_review.sh`** + **`prompts/tier4-cross-review.md`** —
   new node; requires workflow YAML change. Higher risk because it adds a
   parallelism fan-out and new quorum dependency. Wrap behind
   `MO_CROSS_REVIEW=1` feature flag initially.

5. **Extend `tier4_quorum` verifier** — add optional xrank file check gated
   on `MO_CROSS_REVIEW` env var to avoid breaking existing runs.

6. **Feed cross-review scores into `krippendorff_alpha_gate`** — final
   integration step after cross-review is stable.

---

## 8. Council Limitations NOT Worth Porting

- **Streaming SSE** — mini-ork is a shell framework; SSE is irrelevant.
- **Conversation storage** — mini-ork uses `MINI_ORK_DB` (SQLite) with its
  own schema; not a gap.
- **Title generation** — cosmetic; not a correctness concern.
- **Chairman de-anonymization** — this is actually a gap in the council that
  mini-ork should NOT replicate. Keep the shadow `panel-anon/` dir strategy
  to preserve anonymization through synthesis.
