# arXiv Submission Checklist

## Human-Owned Metadata

- [x] Replace `mini-ork Contributors` with the final author names.
- [ ] Add author affiliations and contact email.
- [ ] Confirm repository URL and license are public.
- [ ] Choose target category, likely `cs.SE` first and `cs.AI` / `cs.MA` as cross-list candidates.
- [ ] Confirm whether the submitting account needs endorsement for the chosen category.

## Scientific Claim Gate

- [x] Narrowed claim to the controlled 48-run mini-ork benchmark.
- [x] Included deterministic verifier pass rate, reviewer accept rate, cost per success, median time, and expensive-call count.
- [x] Stated that this does not prove arbitrary replacement of frontier models.
- [x] Documented query-level workflow generation as future work, not a completed baseline.
- [x] Documented fixture ceiling effects and planner fallback contamination.

## Build Gate

- [x] Build `main.pdf` with `pdflatex` and `bibtex`.
- [ ] Review the PDF for table width, citation rendering, and author metadata.
- [ ] Remove generated auxiliary files before committing unless the project intentionally tracks them.
- [x] Regenerate Nano Banana figures after renewing `GEMINI_API_KEY`, if richer raster illustrations are desired.

## Submission Package

- [ ] Upload `main.tex`.
- [ ] Upload `references.bib`.
- [ ] Upload `assets/deterministic-figures/*.pdf`.
- [ ] Upload `assets/paperbanana-figures/*.png`.
- [ ] Do not upload `assets/ai-studio-figures/*.jpg` unless `main.tex` is changed to reference them.
- [ ] Include source only, not local experiment logs, unless the logs are converted into a compact appendix.
