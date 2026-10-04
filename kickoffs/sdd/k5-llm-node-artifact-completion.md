# K5 — artifact-based completion for LLM nodes (stop false-negative node failures)

## Goal

An LLM node whose declared output artifacts exist and validate must count as
COMPLETE even when the agent's self-report handshake is missing or malformed.
Apply the same principle the SDD verifiers already encode: recompute the
verdict from artifacts, never trust (or require) the agent's self-report.

## Evidence (2026-10-03/04, five runs)

- `run-sdd-k2-recipe-scaffold-202610040022`, `run-sdd-k3-verifiers-202610040104`,
  `run-sdd-k4-e2e-fixture-eval-202610040134` (recursive-validate-impl, repo
  `/Users/admin/ps/mini-ork-sdd-wt`): implementer produced the full deliverable
  on disk; tier1 failed with "no implementer artifact ready_for_tier1=true with
  non-empty touched_files". In K3's case `implementer-summary.json` existed but
  with agent-invented keys (`status/files_changed`) instead of
  `ready_for_tier1/touched_files`.
- `run-sdd10x-202610040245` (spec-driven-delivery, home
  `/Volumes/docker-ssd/ps/sdd-10x-ork/home`): contract_compiler wrote all 35
  `spec-cards/*.json` + `ratification/*.json`, engine marked the node failed,
  11 downstream nodes cascade-skipped; reflector/replanner never emitted
  `reflector.json`/`replan.json` for the same reason.

## Feature scope

- In the execute runtime, when an LLM node's dispatch ends (any rc), run an
  artifact-completion check derived from the node's declared outputs (from the
  recipe's artifact contract / per-node `outputs` where present; for
  recursive-validate-impl's implementer, `implementer-summary.json`):
  - If the declared artifacts exist, are non-empty, and parse (JSON where
    `.json`), mark the node complete and log
    `[ok] node completed via artifact check (self-report missing/invalid)`.
  - Tolerant summary coercion: accept `files_changed` as `touched_files` and
    infer `ready_for_tier1=true` from a non-empty file list, writing the
    normalized summary back so downstream verifiers see the canonical shape.
- Per-node opt-out `strict_handshake: true` in workflow node definitions for
  nodes where the self-report is itself the deliverable.
- Unit tests reproducing the K3 summary-shape case and the compiler
  artifacts-present/handshake-absent case.

## Definition of Done (probes)

```bash
# P1: new tests pass
python3 -m pytest -q tests/test_node_artifact_completion.py

# P2: K3-shape summary (status/files_changed) is normalized and accepted
python3 -c "from mini_ork.execute_compat import normalize_implementer_summary as n; s=n({'status':'implemented','files_changed':['a.py']}); assert s['ready_for_tier1'] and s['touched_files']==['a.py']"

# P3: scoped neighbors still green
python3 -m pytest -q tests/test_sdd_verifiers.py tests/test_specdir_ingest.py
```

(Exact module/function placement may differ — keep the probe updated with the
real import path; the behavior contract is what is fixed.)

## Hard rules

- NEVER run the repo-wide full pytest suite as a probe (flaky under
  concurrent campaigns; 19+ min).
- Artifact check must not mask real failures: a node with missing/invalid
  declared artifacts still fails with the original reason.
- No behavior change for verifier/publisher/rollback node types.

## Success command

```bash
python3 -m pytest -q tests/test_node_artifact_completion.py
```

## Verification command

- `python3 -m pytest -q tests/test_node_artifact_completion.py`
