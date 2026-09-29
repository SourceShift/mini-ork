"""ASSAY — the solve-time oracle. Does this patch ACTUALLY fix the bug?

Crucible tells you what the code did. Assay tells you what it MEANS.
(The crucible melts the ore; the assay determines what the metal actually is.)

Four terms, each defending an attack the literature documents:

  1. PoC+          a reproduction test that asserts the EXPECTED OUTPUT the reporter
                   stated — and that FAILS ON BASE FOR THE REASON THE ISSUE DESCRIBES.
                   A crash-only PoC passes symptom-suppressing patches: 42.3% false
                   discovery (arXiv 2603.06858). A merely-red test is worse still —
                   ours were red because they were BROKEN (4 of 5 failed on the gold
                   patch too).
  2. DELTA GATE    the probe must go red -> green.                            (PR #170)
  3. METAMORPHIC   the same property must hold across perturbed inputs, so a patch that
     AMPLIFICATION special-cases the reported input is filtered out. A single execution
                   test is an extensional verifier and IS gamed: ~73.6% shortcut rates,
                   worsening with compute (arXiv 2604.15149). THIS IS THE ONLY TERM THAT
                   CAN CATCH A PATCH WHICH PASSES THE TEST AND IS STILL WRONG.
  4. ABSTAIN       if any term cannot be established -> UNVERIFIED. Never "pass".
                   This is what kills false-completion.

EVERY GENERATED TEST IS ITSELF VALIDATED BEFORE IT IS ALLOWED TO JUDGE.
That is the lesson that produced this file. A probe is evidence only if it fails on the
buggy code for the RIGHT REASON; an invariant is evidence only if it is about the bug.
Skip that and you are not measuring the patch — you are measuring your test generator.

Verdicts: PROVEN | REFUTED | UNVERIFIED   (never a bare "pass")
"""
from __future__ import annotations

from collections.abc import Callable

from mini_ork.certify import invariants as mr
from mini_ork.certify import probe as poc_plus
from mini_ork.certify.context import CodeContext
from mini_ork.certify.verdict import (
    REFUTED,
    UNVERIFIED,
    Verdict,
)
from mini_ork.runtime import ExecOutcome


# A `runner` exposes the same seam mini_ork.runtime.Crucible does: `.up: bool` and
# `.run_test(src: str, patch: str = "") -> ExecOutcome`. The oracle does NOT construct
# one; the oracle consumes it. That keeps the oracle runtime-agnostic (subprocess,
# prime, mock) and decoupled from any specific backend.
Runner = object

# A `dispatch` is a `Callable[[str], str]` (prompt -> text). The default routes through
# the worker lane via mini_ork.certify.llm.default_dispatch; callers can inject a fake
# to keep the oracle hermetic under unit tests.
DispatchFn = Callable[[str], str]


def _default_dispatch() -> DispatchFn:
    from mini_ork.certify.llm import default_dispatch

    return default_dispatch


def judge(
    issue: str,
    patch: str,
    *,
    runner,
    mr_n: int = 4,
    gold: str | None = None,
    poc: str | None = None,
    dispatch: DispatchFn | None = None,
    context: CodeContext | None = None,
) -> Verdict:
    """Judge `patch`. Nothing here reads `gold` — it is used only to LABEL the invariants
    for measurement, never to decide. Pass gold=None in production.

    `poc` lets a caller supply an ALREADY-VALIDATED reproduction test. Building it is
    stochastic, so re-rolling it here would (a) waste the work a caller already did and
    (b) let a flaky empty generation abstain on a patch that a good probe would have
    judged — depressing recall for no gain. When supplied, we trust it (the caller
    validated it against gold at corpus-construction time).

    `runner` is duck-typed (`.up`, `.run_test`). It is owned by the caller — the oracle
    never constructs one. `dispatch` is duck-typed too (prompt -> text). Both are
    injectable so tests can run hermetically.

    `context` (optional) carries the BASE source of the changed .py files plus the
    importable module names of those files. When supplied, probe.build and invariants.build
    prompt the model with the BASE source AND structurally reject any candidate that does
    not import the code under test. SWE-bench-style callers omit `context` and the engine
    behaves exactly as before.
    """
    d_fn = dispatch or _default_dispatch()

    if not patch.strip():
        return Verdict(UNVERIFIED, "no patch")

    # ── runner readiness ────────────────────────────────────────────────────
    # The oracle never constructs a Crucible; it consumes a runner the caller owns.
    # A runner that is not up is a harness fact, not a patch verdict — UNVERIFIED
    # makes the failure observable instead of silently degrading to "no patch".
    if not getattr(runner, "up", False):
        return Verdict(UNVERIFIED, "runner unavailable")

    # ── Term 1: a PoC+ that reproduces the REPORTED failure ─────────────────
    poc_src = poc
    if poc_src is None:
        poc_src, why = poc_plus.build(
            issue, lambda s: runner.run_test(s),
            dispatch=d_fn, context=context,
        )
        if not poc_src:
            return Verdict(UNVERIFIED, f"no trustworthy reproduction test: {why}")

    # ── Term 2: the delta gate ──────────────────────────────────────────────
    post: ExecOutcome = runner.run_test(poc_src, patch=patch)
    if post.status == "apply_fail":
        return Verdict(REFUTED, "patch does not apply", poc_plus=poc_src)
    if post.status == "test_defect":
        return Verdict(UNVERIFIED, f"the probe broke on the patched code ({post.exc}) — cannot judge",
                       poc_plus=poc_src)
    if post.status != "passed":
        return Verdict(REFUTED,
                       f"the reported bug still reproduces after the patch ({post.status}/{post.exc})",
                       poc_plus=poc_src)

    # ── Term 3: metamorphic amplification ───────────────────────────────────
    # The probe is green. That is exactly the state a special-cased patch engineers,
    # so it proves nothing on its own. Now ask whether the fix GENERALISES.
    # build validates informativeness IN the generation loop (each kept invariant fails
    # on base = truly reproduces the bug), retrying with feedback until it has enough.
    # This is the matplotlib-23299 fix: robust for the correct patch AND the cheat.
    cands = mr.build(
        poc_src, issue, patch=patch, n=mr_n,
        base_runner=lambda s: runner.run_test(s),
        dispatch=d_fn,
        context=context,
    )
    if not cands:
        return Verdict(UNVERIFIED,
                       "the probe passes, but no invariant that reproduces the bug could be built "
                       "-> cannot rule out a special-cased patch", poc_plus=poc_src)

    kept, dropped, passed = [], [], 0
    for name, src in cands:                               # already validated informative
        res = runner.run_test(src, patch=patch)
        hit = res.status == "passed"
        rec = {"mr": name, "on_patch": res.status, "holds": hit}
        if gold is not None:                              # MEASUREMENT ONLY — never a decision
            g = runner.run_test(src, patch=gold)
            v_ok, v_why = mr.valid(g)
            rec["valid_on_gold"], rec["gold_says"] = v_ok, v_why
        kept.append(rec)
        passed += hit

    n = len(kept)
    rate = (passed / n) if n else 0.0
    verdict, reason = mr.score(rate, n)
    return Verdict(verdict, reason, poc_plus=poc_src, mr_pass_rate=(rate if n else None), mr_n=n,
                   detail={"invariants": kept, "dropped": dropped})


__all__ = ["judge"]