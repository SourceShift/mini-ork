# OpenClaw star-growth panel — 2026-10-18

Four independent lenses (growth, activation, ecosystem, capability) read the
OpenClaw reference checkout and one synthesizer reconciled them into a ranked
adoption plan for mini-ork. This directory is the run's output, copied from `run-1792352699-84084`
with the two host-specific checkout paths normalized to `$REF` and
`$MO_TARGET_CWD`.

- Recipe: `recipes/star-growth-panel/`
- Kickoff: `kickoffs/star-growth/openclaw-feature-adoption.md`
- Gate: `recipes/star-growth-panel/verifiers/lens-completeness.py` (pass; log
  in `verifier.log`)
- Served lanes and families: growth `codex_lens`/openai (gpt-5.6-luna),
  activation `glm_lens`/zai (GLM-5.3), ecosystem `deepseek_lens`/deepseek
  (deepseek-v4-flash), capability `minimax_lens`/minimax (MiniMax-M3)

Start with `synthesis.md`; the four `lens-*.md` reports carry the receipts and
the `context-*.json` sidecars carry the machine-readable candidates.
