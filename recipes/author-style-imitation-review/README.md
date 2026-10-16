# author-style-imitation-review

Literature → "which techniques actually work" for reproducing an author's
writing style.

```
query_planner (deterministic: 50 queries → 5 batches of 10)
  → searcher_01..05   (deterministic, PARALLEL: 1 LibWit batch call each, $0)
  → corpus_merger     (deterministic: dedupe, re-rank by cross-batch coverage)
  → corpus_sharder    (deterministic: 1000 papers → 10 shards of 100)
  ├─ extract_shard_01..10   (glm_lens/minimax_lens alternating ← ONLY wide model read)
  → technique_catalog (deterministic: coverage ≥95%, corpus-checked ids)
  → technique_clusterer     (glm_lens: defines exactly 8 families — names, criteria, keywords)
  → cluster_splitter  (deterministic: assigns every technique to a family, then one bounded file per family)
  ├─ extract_group_01..08   (alternating lanes: implementable techniques + evidence)
  → review_packer     (deterministic: evidence check + target-state snapshot)
  → impact_reviewer_glm ∥ impact_reviewer_minimax   (independent scores)
  → review_merger     (deterministic: priority, replication bonus, conservative verdict)
  → report_writer     (glm_lens) → final_assembler → review_completeness
```

## Why the DAG is shaped this way

Three levers, in order of how much they buy:

1. **Retrieval is deterministic and free.** Five nodes, each one LibWit batch
   call over ten queries. 1000+ papers for zero model tokens and one round-trip
   of wall time. Delegating 50 searches to LLM agents would cost real money and
   add a parsing failure mode per search, for no recall benefit — query
   diversity, not search intelligence, is what drives recall here.
2. **Compression happens exactly once.** Only the ten shard extractors read the
   full corpus. Downstream nodes read bounded artifacts: a group file carries a
   500-character summary per paper, never the abstract. Adding stages after the
   catalogue does not re-inflate the context.
3. **Judgment runs on the distilled set.** The two reviewers score ~60-120
   canonical techniques, not 1000 papers. Reviewer cost scales with techniques,
   not with corpus size, so widening the corpus is nearly free end to end.

Quality comes from three places: cross-batch topical coverage as a relevance
rank (a paper found by several independent query batches sits closer to the
topic's centre); two reviewers from different model families, so a family's
blind spot does not become the verdict; and a deterministic verifier that fails
the run if the report cites an arXiv id that is not in the collected corpus.

## Run

```bash
# from the repo root
recipes/author-style-imitation-review/launch.sh [run-id]
```

The launcher restricts every lane to glm + minimax in a run-scoped config, lifts
`ARXIV_API_TOKEN`, and sets the timeouts this corpus needs. Plain
`mini-ork run author-style-imitation-review <kickoff>` also works — `glm_lens`
and `minimax_lens` already exist in `.mini-ork/config/agents.yaml`.

## Tuning

| Knob | Default | Effect |
|---|---|---|
| `MINI_ORK_COLLECTION_PLAN` | `collection-plan.json` | Different query set / corpus floor |
| `MINI_ORK_RESEARCH_MIN_SOURCES` | `1000` | Corpus floor; a plan asking for less fails |
| `MO_STYLE_SEARCH_BATCHES` | `5` | Must match the searcher nodes in `workflow.yaml` |
| `MO_STYLE_SHARD_COUNT` | `10` | Must match the shard outputs in `workflow.yaml` |
| `MO_STYLE_GROUP_COUNT` | `8` | Must match the group outputs in `workflow.yaml` |
| `MO_STYLE_MIN_COVERAGE` | `0.95` | Shard-extraction coverage floor |
| `MO_STYLE_STATE_FILE` | `context/style-imitation-state.md` | What the reviewers treat as already known |

Changing a count means editing `workflow.yaml` to match — the deterministic
stages assert the declared node count and fail loudly on a mismatch.

## Refresh the target state

`context/style-imitation-state.md` is what the reviewers score against: the
known-working baseline, the measurement-only traps, and the documented failure
zones. Edit it (or point `MO_STYLE_STATE_FILE` elsewhere) to re-aim the review
at a different target system without touching the pipeline.
