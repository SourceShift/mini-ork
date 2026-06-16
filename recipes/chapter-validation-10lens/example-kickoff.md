# Validate chapter: <CHAPTER-TITLE>

## Goal

Run the chapter-validation-10lens recipe against the chapter at
`<absolute-path-to-chapter.md>`. Each of 10 lenses scores one
validation slice; the synthesizer rolls them into a single
pass/revise/block verdict; the publisher emits a human-readable
report.

## Chapter context

- title: <chapter title>
- genre: technical|academic|trade|textbook|practical|literary|other
- target_word_count: 2500
- assigned_source_ids: ["arxiv:2410.12345", "arxiv:2502.67890"]
- publisher_style: <slug-or-null>
- language: en|fa|es|...

## Chapter artifact

`<absolute-path-to-chapter.md>`

## Verification commands

- `bash recipes/chapter-validation-10lens/verifiers/lens_outputs_complete.sh`
  exits 0 → all 10 lens-NN-verdict.json + panel-verdict.json present + schema-valid

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` exists with `overall_verdict` ∈ pass|revise|block
- `${MINI_ORK_RUN_DIR}/chapter-validation-report.md` exists
- Verifier `lens_outputs_complete.sh` exits 0
