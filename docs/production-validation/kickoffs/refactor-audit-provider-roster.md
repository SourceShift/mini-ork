# Production scenario: refactor-audit provider roster

## Goal

Audit whether mini-ork's provider-roster promises match its actual workflow and
dispatch implementation.

## Target repo

This repository: `mini-ork`.

## Audit dimensions

- README and roadmap claims.
- `config/agents.yaml` lane mappings.
- `recipes/*/workflow.yaml` model lanes.
- Provider wrappers under `lib/providers/`.
- Guardrails that prevent temporary provider policy from mutating durable recipes.

## Success criteria

- Four lens reports exist: `lens-glm.md`, `lens-kimi.md`, `lens-codex.md`,
  `lens-opus.md`.
- The synthesis identifies any stale MiniMax-vs-Opus claim drift.
- Each finding cites file:line evidence.
- `recipes/refactor-audit/verifiers/lens-completeness.sh` passes.

## Provider policy

Requires GLM, Kimi, Codex, and Opus for live validation. During a no-Claude
window, run only in dry-run topology mode.

## Risk tolerance

Read-only audit. Do not edit files during the run.
