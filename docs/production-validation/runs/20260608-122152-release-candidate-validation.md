# mini-ork release-candidate validation — 2026-06-08

This report validates the public mini-ork feature surface before cutting the
next release. The pass intentionally covers both framework internals and the
user-facing markdown kickoff path.

## Environment

- Date: 2026-06-08
- Provider policy for production scenarios: `codex-only`
- Live Phase E provider: `codex`
- Anthropic validation lanes: not used for this pass

## Claim-to-proof matrix

| Claimed feature surface | Proof run | Result | Notes |
| --- | --- | --- | --- |
| Universal lifecycle: classify -> plan -> execute -> verify | `PYTHONPATH=. timeout 1200 bash tests/run-all.sh` | PASS, 525 OK / 0 FAIL | Covers bin-level integration plus e2e recipe loops. |
| User can run from a markdown kickoff | `PYTHONPATH=. python3 scripts/run_production_scenarios.py --mode dry-run --provider-policy codex-only --md-only` | PASS, 9 OK / 0 FAIL | Exercises `mini-ork run <kickoff.md>` dispatcher resolution for all scenarios. |
| Explicit recipe override works | `PYTHONPATH=. python3 scripts/run_production_scenarios.py --mode dry-run --provider-policy codex-only` | PASS, 9 OK / 0 FAIL | Exercises `mini-ork run <recipe> <kickoff.md>` for all scenarios. |
| Dispatcher builds a run profile before planning | `FILTER='bin_dispatcher|bin_plan|python_framework' PYTHONPATH=. bash tests/run-all.sh integration` | PASS, 50 OK / 0 FAIL | `run_profile.json`, `profile_path=`, plan `run_profile_path`, and strict high-risk blocker are pinned. |
| Python framework facade remains importable and usable | `tests/integration/test_python_framework.sh` via integration filter | PASS | Confirms `mini_ork.MiniOrk` wrapper still runs dry-run lifecycle. |
| Opus lens remains part of the refactor-audit contract | `tests/integration/test_refactor_audit_verifier_opus.sh` via full suite | PASS | Protects against accidental removal of the Opus architectural-shape lens. |
| Security guardrails | Full security layer via `tests/run-all.sh` | PASS, 10 security files OK | Includes command injection, traversal, malformed YAML, oversized input, SQL injection, symlink attacks. |
| Self-improvement/promotion chain live path | `PHASE_E_PROVIDER=codex timeout 900 bash tests/live/phase_e_live_validation.sh` | PASS, 8 OK / 0 FAIL | Report: `docs/_meta/phase-e-live-validation-20260608-141559.md`. |

## Gap found and fixed

### G-20260608-01: profile enrichment existed only as a designed requirement

The production scenario plan said users should be able to start from a
markdown file and have the dispatcher build a profile before planning. The
code had `classify -> plan -> execute -> verify`, but no persisted
`run_profile.json`, no `profile_questions=`, and no strict block for high-risk
missing context.

Fixed in this pass:

- `mini-ork run` now writes `.mini-ork/runs/<run-id>/run_profile.json` between
  classification and planning.
- The dispatcher emits `profile_path=`, `profile_status=`,
  `profile_confidence=`, and `profile_questions=` when questions exist.
- `mini-ork-plan` receives the run profile in prompt context and records
  `run_profile_path` in dry-run plans.
- `MINI_ORK_PROFILE_STRICT=1` blocks incomplete high-risk profiles before
  planning.

## Remaining honest limitations

- Profile answers are persisted in `run_profile.json`, not yet relational DB
  tables. Future work should add `run_profiles`, `run_profile_questions`, and
  `run_profile_answers` once the questionnaire UX needs cross-run querying.
- Dry-run planning still writes a placeholder plan. This validates topology,
  profile threading, and verification plumbing, but not model planning quality.
- The next release should not claim provider-live validation for every recipe;
  this pass live-validates Phase E with Codex and dry-run-validates the full
  production scenario catalog.

## Release recommendation

Release can proceed after the profile patch and this validation report are
committed and pushed. The release notes should call this a framework/profile
and validation release, not a guarantee that every shipped recipe has been
live-run against every provider family.
