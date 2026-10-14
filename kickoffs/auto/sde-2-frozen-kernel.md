# SDE-2 frozen kernel for auto-opened fix epics (self-driving epic, step 2)

## Goal

The self-driving loop will open `framework-edit` fix epics on its own, including
on model-made diagnoses (operator chose the aggressive policy). An auto-opened fix
must never be able to edit the brakes that bound it: the escalation policy, the
circuit breaker, the budget pause, the abstention gate, the sandbox, the integrity
guard, the verifier that enforces all this, or the list itself. Today nothing
stops it: the only path guard is `_high_blast_radius_guard` in
`recipes/framework-edit/verifiers/static-check.py`, which covers three legacy
paths and is unlocked by the literal token `ALLOW_HIGH_BLAST_RADIUS` anywhere in
the run's `kickoff.md` / `context-pack.json` — and an auto-promoted kickoff embeds
raw log excerpts, so a log line carrying the token would unlock it.

This adapts the never-launched E8 "unfireable kernel" (`kickoffs/auto/rsi-e8-unfireable-kernel.md`)
to the fix-epic boundary. All paths are relative to the repository root.

## Mechanism (exact spec)

1. New tracked file `config/kernel-frozen.yaml`:

   ```yaml
   version: 1
   # Paths an auto-opened (origin=auto-triage) framework-edit may never change.
   # fnmatch globs, repo-relative.
   paths:
     - config/kernel-frozen.yaml
     - recipes/framework-edit/verifiers/static-check.py
     - mini_ork/vcs/repo_integrity_guard.py
     - mini_ork/runtime/sandbox.py
     - mini_ork/gates/gate_bootstrap.py
     - mini_ork/gates/abstain_gate.py
     - mini_ork/recovery/circuit_breaker.py
     - mini_ork/recovery/escalation_policy.py
     - mini_ork/dispatch/cost_pause.py
     - mini_ork/stores/safety_events.py
     - mini_ork/observability/bug_report.py
   ```

2. `mini_ork/vcs/repo_integrity_guard.py` gains
   `load_frozen_paths(root: str) -> list[str]` (missing/unparsable file → the
   built-in default list above, never an empty list — fail closed) and
   `frozen_hits(changed: list[str], patterns: list[str]) -> list[str]`.

3. `mini_ork/observability/bug_report.py` `_render_promoted_kickoff`, for
   `recipe == "framework-edit"`:
   - first line of the body is the marker `<!-- mini-ork:origin=auto-triage -->`;
   - every occurrence of `ALLOW_HIGH_BLAST_RADIUS` in `desc` and `fix` is replaced
     with `<redacted-allow-token>` before rendering.
   The default (non-framework-edit) body stays byte-identical.

4. `recipes/framework-edit/verifiers/static-check.py`:
   - New check `frozen-kernel-guard`: when the run's `kickoff.md` carries the
     `mini-ork:origin=auto-triage` marker and any changed file matches the frozen
     list, the check fails. The ALLOW token can NEVER unlock it.
   - The frozen list is loaded from the ENGINE checkout the verifier already
     resolves (`_engine`), unioned with the target's committed
     `git show HEAD:config/kernel-frozen.yaml` when present — never from the
     working-tree copy the diff modifies, so a diff cannot unfreeze itself.
   - In `_high_blast_radius_guard`, an origin-marked kickoff ignores the ALLOW
     token (the guard then fails for the legacy paths too).
   - On a frozen-kernel failure, best-effort
     `mini_ork.stores.safety_events.emit("frozen-kernel-edit", "high", <json with run_id and offending paths>, run_id=...)`;
     a missing DB must not turn the failure into a pass.
   - A human-authored kickoff (no marker) behaves exactly as today.

## Files in scope

- config/kernel-frozen.yaml (new)
- mini_ork/vcs/repo_integrity_guard.py
- mini_ork/observability/bug_report.py
- recipes/framework-edit/verifiers/static-check.py
- tests/unit/test_frozen_kernel.py (new)

## Tests

`tests/unit/test_frozen_kernel.py`, hermetic (temp git repos; follow the
fixture style of `tests/unit/test_framework_edit_static_check.py`):
- origin-marked kickoff + diff touching `mini_ork/recovery/circuit_breaker.py` →
  `frozen-kernel-guard` fails, even with `ALLOW_HIGH_BLAST_RADIUS` present;
- origin-marked kickoff + diff that deletes the circuit-breaker entry from
  `config/kernel-frozen.yaml` → still fails (list read from engine/HEAD);
- origin-marked kickoff + diff touching only `mini_ork/recovery/retry_hint.py` → passes;
- human kickoff (no marker) + frozen path → `frozen-kernel-guard` passes;
  `_high_blast_radius_guard` behaviour unchanged;
- `_render_promoted_kickoff(recipe="framework-edit", desc="... ALLOW_HIGH_BLAST_RADIUS ...")`
  starts with the marker and contains no live token; default recipe body is
  byte-identical to before;
- `load_frozen_paths` on a missing or garbage file returns the non-empty default.

Also run `tests/unit/test_framework_edit_static_check.py tests/unit/test_bug_report_py.py tests/unit/test_repo_integrity_guard_py.py`.

## Self-application measurement

Inject one candidate per frozen path (origin-marked) and one per three allowed
paths. Accepted iff frozen refusal is 100% and allowed false-refusal is 0%.

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/kernel-refusal.json` —
  `{"frozen_refused": <int>, "frozen_total": <int>, "allowed_refused": <int>, "allowed_total": <int>}`.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_frozen_kernel.py tests/unit/test_framework_edit_static_check.py tests/unit/test_bug_report_py.py tests/unit/test_repo_integrity_guard_py.py` passes.
- Human-authored framework-edit runs are unaffected.

## Rules

Edit files directly; do not emit unified diffs. Do not touch execute.py,
publisher*.py, auto_repair.py, retry_hint.py, or triage/.
