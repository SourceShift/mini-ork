"""Pre-push multi-lens code review building blocks.

Extracted from ``mini_ork/pre_push_review.py`` (SRP split):

  * ``common``   — env resolution + issue row shape helpers
  * ``lenses``   — heuristic check lenses + native LLM panel dispatch
  * ``gitdiff``  — git merge-base / diff subprocess helpers
  * ``verdict``  — verdict policy (compute + persist)

The public API remains re-exported from ``mini_ork/pre_push_review.py``;
import from there, not from here, unless you need a submodule directly.
"""
