# Framework Edit: Cost Saved Badge

## Goal

Add a compact "Cost saved vs Opus" badge to the Trajectory UI page header.

## Scope Hint

- `ui/src/routes/trajectory/**`
- `ui/src/components/trajectory/**`

## Expected Edit

Touch two files:
1. The Trajectory page header component.
2. The nearby cost or run-summary formatter used by that page.

## Requirements

- Reuse existing cost data already shown on the page.
- Do not add a new backend endpoint.
- Do not modify `.mini-ork/config/**`.
- Keep the badge small enough not to wrap the header on desktop.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed two-file UI
  patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains:
  `{ "files_changed": 2, "tests_pass": true, "static_pass": true, "pass": true }`
- Static checks and `pytest tests/test_web_smoke.py` pass in the isolated
  worktree.
