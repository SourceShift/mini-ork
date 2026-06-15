# Context-formation strategy registry (E-MO-02)

Per the 3-axis topology framework
(docs/_meta/research/20260602-2030-context-formation-diversity-framework-multi-agent-panels.md
in the host application + upstream's positioning doc), the C axis — context formation
distance — measures HOW DIFFERENTLY each lens constructed its working
evidence from the same input.

Today every lens in a mini-ork recipe consumes the same `KICKOFF_PATH`
verbatim → C ≈ 0 → coalition along the C axis even when I (inductive
prior) is high.

This directory ships per-lens context-construction strategies that recipes
can declare via `workflow.yaml`:

```yaml
nodes:
  - { name: a11y_lens, type: researcher, model_lane: glm_lens,
      context_strategy: chunk_semantic,
      prompt_ref: prompts/lens-a11y.md, ... }
```

## Available strategies

| Strategy name | What it does | When useful |
|---|---|---|
| `passthrough` | Emit the input as-is. Identity. | Baseline / when no variation wanted |
| `chunk_fixed`     | Fixed-N-line chunking (default N=80). Each lens gets one chunk. | Coarse variation when input is long |
| `chunk_semantic`  | Paragraph/section-boundary chunking. | Code or markdown with structural separators |
| `chunk_structural` | AST-aware chunking (markdown headers / shell functions / TS exports). | Code-shape input |
| `reorder_shuffle`  | Same chunks, randomised order (stable per-strategy seed). | Anti-positional-bias variation |
| `reorder_reranked` | Same chunks, re-sorted by relevance to the lens's domain keywords. | Bias variation when lens has a domain |

## Strategy contract

Each strategy module exports a single function:

```bash
cs_<name>_prepare <input_path> <output_path> <lens_name>
```

- `input_path`: the original `KICKOFF_PATH` or upstream node output
- `output_path`: where to write the prepared context (caller-supplied)
- `lens_name`: the requesting lens's name (lets strategies vary per lens
  even when called from a panel)

Strategies emit the prepared context as a file. The dispatcher
(`cs_dispatch.sh`) routes a recipe-declared strategy name to the right
prepare function + delivers the output to the lens.

## How recipes use this

1. Recipe's `workflow.yaml` adds `context_strategy: <name>` to one or
   more lens nodes.
2. `bin/mini-ork-execute` (when reading the workflow) checks for the
   field; if present, calls `cs_dispatch <strategy_name>` BEFORE
   dispatching the lens.
3. The lens receives the prepared context as its `KICKOFF_PATH`
   override.
4. Post-cycle, `lib/topology_metrics.sh:measure_C` will see DIFFERENT
   `files_read` per lens (because strategies prepare different
   evidence) → C > 0.

## When this lands

After E-MO-02 + E-MO-03 (recipe `panel_topology:` block), recipes can
declare 5 lenses × 5 strategies × 5 families to hit any quadrant of
the (ρ, C, I) cube. Measured by E-MO-01's metrics. Optimised by E-MO-04's
mutation type. Aggregated by E-MO-05's topology-aware synthesizer.
