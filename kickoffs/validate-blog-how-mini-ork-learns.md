# Validate chapter: How mini-ork Learns

## Goal

Run the chapter-validation-10lens recipe against the technical blog post at
`/Volumes/docker-ssd/ps/mini-ork/docs/research/articles/how-mini-ork-learns.md`.
Each lens scores one slice; the synthesizer rolls them into a single
pass/revise/block verdict; the publisher emits a report.

The factuality lens MUST verify the post's math and architecture claims against
the actual source files in this repo (the assigned sources), not external papers.

## Chapter context

- title: How mini-ork Learns
- genre: technical
- target_word_count: 3000
- assigned_source_ids: ["src:bin/mini-ork-execute", "src:lib/process_reward.sh", "src:lib/rho_aggregator.sh", "src:lib/cn_client.sh", "src:docs/LEARNING-LOOP-LIFECYCLE.md"]
- publisher_style: null
- language: en

## Chapter artifact

`/Volumes/docker-ssd/ps/mini-ork/docs/research/articles/how-mini-ork-learns.md`

## Verification commands

- `bash recipes/chapter-validation-10lens/verifiers/lens_outputs_complete.sh`
  exits 0 → all 10 lens-NN-verdict.json + panel-verdict.json present + schema-valid

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` exists with `overall_verdict` ∈ pass|revise|block
- `${MINI_ORK_RUN_DIR}/chapter-validation-report.md` exists
- Verifier `lens_outputs_complete.sh` exits 0
