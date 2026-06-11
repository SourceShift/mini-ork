# Trace-Governed Budget Allocation

This article package contains the LaTeX manuscript, generated figures,
experiment evidence pointers, and arXiv submission artifacts for:

> Trace-Governed Budget Allocation for Cost-Efficient Multi-Agent LLM Software Workflows

## Directory Layout

| Path | Purpose |
| --- | --- |
| `main.tex` | Canonical LaTeX manuscript source. |
| `references.bib` | Bibliography verified against arXiv IDs. |
| `main.pdf` | Built review copy. Regenerate after manuscript or figure changes. |
| `assets/paperbanana-figures/` | PaperBanana PNG figures used for Figure 1 and Figure 2. |
| `assets/deterministic-figures/` | HeadlessChrome and Matplotlib PDF figures; Figure 3 uses the benchmark chart here. |
| `assets/ai-studio-figures/` | Optional Nano Banana / Google AI Studio raster experiments, not currently used by `main.tex`. |
| `assets/prompts/` | Saved AI Studio prompts for reproducibility. |
| `scripts/` | Figure-generation helpers. |
| `submission/` | arXiv checklist and upload source archive. |
| `drafts/` | Earlier Markdown draft retained for historical comparison. |

## Current Figure Provenance

| Figure | File | Source |
| --- | --- | --- |
| Figure 1 | `assets/paperbanana-figures/trace-governed-paperbanana-v2.png` | PaperBanana with Gemini direct image generation. |
| Figure 2 | `assets/paperbanana-figures/policy-decision-paperbanana.png` | PaperBanana with Gemini direct image generation. |
| Figure 3 | `assets/deterministic-figures/benchmark_cost_calls.pdf` | Matplotlib chart generated from measured benchmark values. |

The deterministic backup architecture PDFs remain in
`assets/deterministic-figures/` so the paper can be switched back to fully
programmatic vector figures if needed.

## Build

From this directory:

```bash
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

The generated `main.pdf` is the review copy.

To rebuild the arXiv source archive:

```bash
tar -czf submission/trace-governed-budget-allocation-arxiv-source.tar.gz \
  main.tex references.bib assets/deterministic-figures assets/paperbanana-figures
```

## Figure Generation

Regenerate deterministic backup figures and the benchmark chart:

```bash
python3 scripts/generate_figures.py
```

Generate optional Nano Banana / Google AI Studio candidates:

```bash
GEMINI_API_KEY=... python3 scripts/generate_ai_studio_figures.py
```

The AI Studio generator writes candidate images to `assets/ai-studio-figures/`
and prompts to `assets/prompts/`.

## PaperBanana Notes

PaperBanana was tested from `https://github.com/dwzhu-pku/PaperBanana`.
For local tests, disable OpenRouter when using Gemini directly; otherwise
PaperBanana will prefer `OPENROUTER_API_KEY` if it exists in the environment.

```bash
env -u OPENROUTER_API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  GOOGLE_API_KEY=... \
  MAIN_MODEL_NAME=gemini-2.5-flash \
  IMAGE_GEN_MODEL_NAME=gemini-3-pro-image \
  python skill/run.py \
    --content-file /tmp/mini-ork-paperbanana/method.txt \
    --caption "Academic method diagram for trace-governed budget allocation in mini-ork..." \
    --task diagram \
    --output /tmp/mini-ork-paperbanana/trace-governed-paperbanana-v2.png \
    --aspect-ratio 16:9 \
    --retrieval-setting none \
    --exp-mode demo_planner_critic \
    --max-critic-rounds 1 \
    --num-candidates 1 \
    --main-model-name gemini-2.5-flash \
    --image-gen-model-name gemini-3-pro-image
```

Generated raster diagrams must be visually reviewed before submission. The
accepted candidates currently used by the manuscript have no embedded title,
use a white or near-white background, and contain the required mini-ork routing
components.

## Evidence

The benchmark table in `main.tex` is sourced from:

- `../../experiments/results/trace-budget-cross-task-20260611-48/summary.csv`
- `../../experiments/results/trace-budget-cross-task-20260611-48/comparisons.csv`
- `../../experiments/results/trace-budget-cross-task-20260611-48/summary.md`

The benchmark figure is generated from the same values.
