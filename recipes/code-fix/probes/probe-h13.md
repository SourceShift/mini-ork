---
oracle: git
holdout: true
---
# Frozen probe probe-h13 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(web): strip z-insight at transcript render + backfill tokens from llm_calls ledger

Covers runs persisted before the dispatch-boundary fixes:

- load_transcript strips <z-insight> protocol blocks from turn text
  (both transcript.json and artifact-fallback paths).
- agent_detail backfills zero-token transcripts from the llm_calls
  ledger (executable lanes harvested real usage into llm_calls but the
  old fallback transcript writer recorded zeros), tagged
  tokens_source=llm_calls. run-1781095892-69202's implementer now shows
  77.0k in / 1.1k out instead of '— in / — out'.

## Files in scope

- mini_ork/web/agents.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
