# Artifact-based completion: per-kind contracts (a well-formed fabrication must not pass)

## Goal

`declared_artifacts_ok` (`mini_ork/execute_compat.py:102`) is the gate that
lets a researcher/implementer node complete from its declared outputs when the
self-report is missing (watchdog kill, rc=124). Its current bar per file:
regular file + non-empty + mtime ≥ dispatch start + parses as JSON. That bar
accepts any well-formed fabrication: an agent that writes `{}` — or a summary
whose `files_changed` lists nothing, or lists files it never touched — completes
the node and the run proceeds on phantom work. Tighten it with per-kind
contract checks so "the file exists and parses" stops meaning "the node
delivered".

This is the deferred item D of the 2026-10-09 artifact-honesty batch (A:
listings prune scratch, `da191c35`; B: publisher authored-patch scoping,
`3b56dbe3`; C: node-artifacts provenance, `90ce6ebe`). D is what closes the
loop: B and C made attribution honest; this makes completion honest.

## Files in scope

- `mini_ork/execute_compat.py` — `declared_artifacts_ok` + the new contract
  registry. Nothing else in the module changes semantics.
- `tests/test_node_artifact_completion.py` — extend with the fabrication
  fixtures below.
- `docs/architecture/artifact-graph.md` — § "Artifact-based completion": the
  documented bar gains the contract layer.

Read-only references (do NOT edit):
- `mini_ork/workflow/artifacts.py` — the ArtifactLedger; its per-kind checks
  stay independent (declared outputs vs completion gate).
- `mini_ork/cli/execute_handlers.py` — the caller that logs
  `[ok] node completed via artifact check` and emits `node.artifact_completion`.

## Design

1. **Registry, not a switch statement.** A module-level
   `register_completion_contract(pattern, fn)` where `pattern` is a filename
   basename (exact or `fnmatch`-style) and `fn(path, since_mtime) -> str | None`
   returns `None` on pass or a one-line failure reason. Mirrors
   `mini_ork.gates.gate_registry.register_gate_evaluator` — new kinds land
   without editing the checker. Unknown filenames keep today's checks exactly
   (regular + non-empty + fresh + JSON-parse): the default must not regress.
2. **Built-in contracts (registered at import):**
   - `implementer-summary.json`: must be a JSON object; `files_changed` (or
     normalized `touched_files`) must be a non-empty list of strings, OR the
     summary carries an explicit `ready_for_tier1: false` with a non-empty
     `block_reason` (an honest "I found nothing to change" completes; an empty
     list with no reason does not). Additionally every listed path that is
     relative must resolve under the run's target repo and carry
     `mtime >= since_mtime` — freshness extended from the summary file to the
     files it claims. Absolute paths in the list: skip the existence check
     (run dirs get moved), keep string-shape validation only.
   - `plan.json`: JSON object with a non-empty `steps` collection whose entries
     are objects.
   - `*.diff` / `*.patch` (basename endswith): contains at least one
     `--- ` line followed by a `+++ ` line (a parseable unified diff shape).
     Applied-on-source checks stay OUT of scope — the verifier owns those.
3. **Reason strings name the contract.** A failure returns
   `f"{path}: contract {name} failed ({detail})"` so a run's failure line says
   WHICH contract and WHY (same shape as today's `stale`/`invalid JSON`
   reasons). `declared_artifacts_ok` returns on the FIRST failing path, as now.
4. **Opt-outs unchanged.** `strict_handshake: true` on a node skips
   artifact-based completion entirely (existing `node_strict_handshake`);
   contracts add no new opt-out. The researcher/`recursive-validate-impl`
   fallback paths inherit the contracts through the same function.
5. **Event payload.** The `node.artifact_completion` run event gains a
   `contracts` field: `{"implementer-summary.json": "pass"}` — pass/fail per
   contract that ran, so a triage can see the gate that fired. Keep it
   additive; older readers ignore it.

## Required behaviour (tests)

Fabrication fixtures in `tests/test_node_artifact_completion.py`, all against
`declared_artifacts_ok` directly (unit-level, no dispatch):

1. Summary `{"files_changed": []}` → FAIL, reason mentions the contract and
   "empty".
2. Summary `{"files_changed": ["a.py"]}` where `a.py` does not exist relative
   to the passed repo root → FAIL (claims files that aren't there).
3. Summary `{"files_changed": ["a.py"]}`, `a.py` exists but mtime <
   `since_mtime` → FAIL (stale claim — freshness extends to claimed files).
4. Summary `{"ready_for_tier1": false, "block_reason": "no repro"}` → PASS
   (honest abstention completes).
5. `plan.json` `{"steps": []}` → FAIL; `{"steps": [{"id": "s1"}]}` → PASS.
6. A `.patch` file with valid header pair → PASS; one with prose only → FAIL.
7. Unknown filename (e.g. `notes.md`) with today's checks → PASS unchanged
   (default did not regress).
8. Registry: `register_completion_contract` with a custom pattern + fn is
   honored by a later `declared_artifacts_ok` call for a matching basename,
   and does not affect non-matching basenames.

## Out of scope

- Content-level semantic validation (does the plan make sense, is the summary
  true) — that is reviewer/verifier territory. This kickoff is about shape
  and self-consistency only.
- The ArtifactLedger's own per-kind checks.
- Any change to when artifact-based completion fires (node types, fallbacks,
  `strict_handshake` semantics).
