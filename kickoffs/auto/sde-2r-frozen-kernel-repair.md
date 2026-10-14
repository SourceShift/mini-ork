# SDE-2 repair — auto-triage origin read from the pre-run snapshot

## Goal

Pass 1 (commit `f86a118f`) added `frozen-kernel-guard` to
`recipes/framework-edit/verifiers/static-check.py`. It decides whether a run is
auto-opened by reading the FIRST LINE of the kickoff FILE at verify time
(`_kickoff_is_auto_triage` → `_run_kickoff_candidates`). Auto-promoted kickoffs
live under the mini-ork repo's `kickoffs/auto/`, which an auto fix epic's
implementer can write: deleting the marker line from its own kickoff turns the
run "human" and unlocks every frozen path. The contract a run received is
already snapshotted before any node runs, as `kickoff_snapshot` +
`kickoff_sha256` in `context-pack.v2.json` (`mini_ork.context_v2.load_pack`,
the same source `mini_ork/verify/kickoff_guard.py` uses).

Also: the frozen list omits `mini_ork/recovery/escalation_policy.py`, the
operator-owned policy that admits auto fix epics (it lands in a later step; a
glob for an absent file is harmless).

All paths are relative to the repository root.

## Mechanism (exact)

1. `_kickoff_is_auto_triage()` returns True when EITHER source carries the
   marker as its first line (fail closed — any positive wins):
   - the pre-run snapshot: `context_v2.load_pack(RUN_DIR).get("kickoff_snapshot")`;
   - the kickoff file (today's behaviour).
   Additionally, when the pack has a `kickoff_sha256` and the file's current
   sha256 differs from it, treat the run as auto-triage (a run that rewrote its
   own contract gets no human privileges) and write
   `frozen-kernel: kickoff modified since run start` to the evidence log.
   A missing pack keeps today's file-only behaviour.
2. Add `mini_ork/recovery/escalation_policy.py` to all three copies of the list:
   `config/kernel-frozen.yaml`, `FROZEN_PATHS_DEFAULT` in
   `mini_ork/vcs/repo_integrity_guard.py`, and the inline fallback list in
   `static-check.py`. Keep `mini_ork/recovery/auto_repair.py` frozen.

## Files in scope

- recipes/framework-edit/verifiers/static-check.py
- mini_ork/vcs/repo_integrity_guard.py
- config/kernel-frozen.yaml
- tests/unit/test_frozen_kernel.py

ALLOW_HIGH_BLAST_RADIUS: this operator-authored repair intentionally edits the frozen-kernel list and its verifier.

## Tests (extend `tests/unit/test_frozen_kernel.py`, hermetic)

- snapshot starts with the marker, kickoff file had the marker removed, diff
  touches `mini_ork/recovery/circuit_breaker.py` → `frozen-kernel-guard` fails;
- pack `kickoff_sha256` ≠ current file sha, no marker anywhere, frozen path →
  fails, and the evidence log names the modified kickoff;
- human kickoff, intact sha, frozen path → passes (unchanged behaviour);
- no pack at all → file-only behaviour (existing tests keep passing);
- `load_frozen_paths` default and the yaml both contain
  `mini_ork/recovery/escalation_policy.py`.

## Self-application measurement

Inject: (a) marker-stripped-after-start candidate, (b) sha-drift candidate,
(c) intact human candidate, each touching one frozen path. Accepted iff (a) and
(b) are refused and (c) passes.

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/kernel-refusal.json` —
  `{"frozen_refused": <int>, "frozen_total": <int>, "allowed_refused": <int>, "allowed_total": <int>, "tamper_refused": <int>, "tamper_total": <int>}`.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_frozen_kernel.py tests/unit/test_framework_edit_static_check.py tests/unit/test_repo_integrity_guard_py.py` passes.

## Rules

Edit files directly; do not emit unified diffs.
