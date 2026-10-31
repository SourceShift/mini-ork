---
oracle: git
---
# Frozen probe probe-h06 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(probe-scorer): freeze probe planning via MO_STATIC_RECIPE_PLAN

Live smoke: the opus planner shape-failed twice consecutively on the
probe kickoff (rc=65 looks_like_json) — a stochastic planner failure
kills a random arm and fabricates regression signal. Probe runs now
render the temp recipe's workflow.yaml deterministically
(recipe_fallback_plan, zero LLM): the probe measures the EXECUTION
effect of the directive, not the planner's mood.

## Files in scope

- mini_ork/learning/probe_scorer.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
