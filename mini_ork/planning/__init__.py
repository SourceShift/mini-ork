"""Pure planner-side helpers extracted from ``mini_ork.cli.plan`` (SRP split).

- ``plan_schema``  — plan-JSON extraction + validation (shape/verdict logic).
- ``recipe_plan``  — deterministic recipe fallback plan + artifact-contract overlay.

The CLI runtime (``mini_ork.cli.plan``) re-exports the moved public names, so
existing imports keep working unchanged.
"""
