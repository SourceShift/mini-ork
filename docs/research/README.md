# Research

This directory contains mini-ork research material: article packages,
experiment protocols, measured results, citation verification notes, and
synthesis documents.

## Articles

| Article | Status | Entry point |
| --- | --- | --- |
| Trace-Governed Budget Allocation | Active arXiv submission package | `articles/trace-governed-budget-allocation/README.md` |

## Experiments

| Path | Purpose |
| --- | --- |
| `experiments/trace-budget-experiment-protocol.md` | Protocol for the trace-governed routing benchmark. |
| `experiments/results/trace-budget-cross-task-20260611-48/` | Current 48-run benchmark used in the article. |
| `experiments/results/trace-budget-fixtures-20260611-24c/` | Fixture-only benchmark component. |
| `experiments/results/trace-budget-obs-smoke-20260610-24/` | Observability smoke benchmark component. |

## Supporting Notes

| Path | Purpose |
| --- | --- |
| `citation-verification-2026-06-01.md` | Citation verification and citation-hygiene notes. |
| `synthesis-latest.md` | Latest research synthesis snapshot. |

## Contribution Rules

- Keep finished paper packages under `articles/<slug>/`.
- Keep raw and summarized benchmark evidence under `experiments/`.
- Keep generated submission archives under the relevant article's
  `submission/` directory.
- Do not commit LaTeX auxiliary files such as `*.aux`, `*.bbl`, `*.blg`,
  `*.log`, or `*.out`.
