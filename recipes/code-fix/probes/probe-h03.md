---
oracle: git
---
# Frozen probe probe-h03 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(promote): authority-capture check fails CLOSED when it cannot run

`_cw_por_compute` answered 'default_passed' when the checker could not be
imported and 'indeterminate_default_passed' when it raised, and the caller
in `mo_promote_synthesis_gate` blocked only on 'failed'. All three statuses
therefore cleared the CW-POR condition and fell through to the structural
check — so deleting the checker, breaking its import, or feeding it a panel
with no ground truth silently *permitted* the promote instead of catching
authority capture. The check that exists to stop a captured panel from
licensing itself was itself a way to license a promote.

The statuses are now named for what happened and only an affirmative
'passed' clears the condition:

  * 'unavailable'   — import failed; no check happened.
  * 'error'         — the checker raised.
  * 'indeterminate' — it ran but found no ground-truth signal to compute from.
  * 'failed'        — authority capture suspected (unchanged).
  * 'passed'        — panel healthy (unchanged).

The caller's comparison is widened from `cw_status == "failed"` to
`cw_status != "passed"`, with 'failed' keeping its own `authority_capture`
reason code so the pre-existing blocking path is unchanged. Everything else
rejects as `cw_por_unverified`. A panel with no ground truth is *unmeasured*
capture, not absent capture, and the difference now decides the outcome.

This closes the last fail-open in the promotion path and completes the set
begun by B1 and B2: an unavailable or forgeable check must not resolve to
permission.

[hidden] case (b) is updated deliberately,
not weakened: it seeded an empty `voters` list, which is now rejected earlier
as cw_por_unverified, so it could no longer prove the structural condition is
reachable. It now seeds a correctly-labelled panel that clears CW-POR, which
tests the same thing more precisely.

New [hidden] pins all three fail-closed statuses, the two
statuses that must still work, and asserts the retired fail-open names are
not produced by the function body — so the escape hatch cannot return by
rename. Its import-blocking helper drops the cached parent-package attribute
as well as blanking sys.modules, because `from X import Y` reaches the
attribute first and a sys.modules-only monkeypatch would pass when it ran
first and silently fail to block when it ran third.

Verified live: a verdict clearing panel_score and all three structural
signals gives decision=approved when the checker is present, and
decision=rejected / reason=cw_por_unverified (rc=1) once the checker module
is removed.

## Files in scope

- mini_ork/gates/promotion_gate.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
