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

This module also exposes `replay_check` — the verifier-side twin of the oracle's private
delta gate. Same shape on purpose, so a downstream consumer that already understands
"abstain" for the LLM path reads zero new code for the shell-test path. The caller
owns the base worktree; the helper only consumes the path it's given.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from mini_ork.certify import differential
from mini_ork.certify import invariants as mr
from mini_ork.certify import probe as poc_plus
from mini_ork.certify import relations
from mini_ork.certify.context import CodeContext
from mini_ork.certify.verdict import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    Verdict,
)
from mini_ork.runtime import ExecOutcome
from mini_ork.verify.test_env import scrubbed_test_env


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


# ── Delta-gate replay helper (verifier-side twin of the oracle's delta gate) ──
# Pytest -v output looks like:
#   tests/test_mod.py::test_add PASSED                              [ 50%]
#   ERROR tests/test_mod.py - ImportError: ...                     [100%]
# Match both shapes so a collection error on the base still counts as a
# failure for the overlap test (rather than vanishing into "no overlap").
_TEST_RESULT_RE = re.compile(
    r"(?P<id>(?:\S+::\S+|\S+\.py))\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)\b"
)


def _ensure_pytest_verbose(cmd: str) -> str:
    """Insert `-v --tb=no` into a pytest command that lacks verbose flags.

    Non-pytest commands are returned unchanged; the caller is responsible
    for skipping the replay when the test runner cannot produce per-test
    output. We auto-augment so most callers don't have to think about it.
    """
    if "pytest" not in cmd:
        return cmd
    if re.search(r"(?:^|\s)(?:-v\b|--verbose\b)", cmd):
        return cmd
    return re.sub(r"\bpytest\b", "pytest -v --tb=no", cmd, count=1)


def _tail(text: str, n: int) -> str:
    """Return the last `n` characters of `text`; `""` for non-strings.

    Used to surface the most-specific failure text on a kept invariant
    without bloating the certificate with full pytest tracebacks.
    """
    return text[-n:] if isinstance(text, str) else ""


def _failure_signature(output: str) -> str | None:
    """First pytest `E ` line of `output`, normalised.

    Returns the body of the first line that starts with literal ``E ``
    (E plus a single space — pytest's assertion/exception marker), with the
    leading ``E`` and surrounding whitespace stripped and internal runs of
    whitespace collapsed to a single space. ``None`` when ``output`` has no
    such line — the caller treats a missing signature as "cannot tell" and
    falls back to the conservative broken count.
    """
    if not isinstance(output, str):
        return None
    for line in output.splitlines():
        if not line.startswith("E "):
            continue
        body = line[2:]
        collapsed = re.sub(r"\s+", " ", body).strip()
        return collapsed or None
    return None


def replay_check(
    cmd: str,
    *,
    base_cwd: str,
    candidate_cwd: str | None = None,
    candidate_log: str | None = None,
    base_log: str | None = None,
) -> dict:
    """Replay `cmd` on a clean base and demand overlap with the candidate.

    Returns a dict::

        {
          "passed":     bool,   # True iff at least one test passed on candidate
                                # AND failed on base
          "reason":     str,    # human-readable
          "unverified": bool,   # True if base state could not be evaluated;
                                # the caller should abstain rather than
                                # pass/fail
          "replay":     dict | None,
        }

    Where ``replay`` carries ``{candidate_passed, base_failed, overlap}``
    as sorted lists — the audit trail a downstream consumer needs to
    decide whether to override the verdict.

    Outcomes (mapped to the kickoff):

      base-fails / candidate-passes (overlap non-empty)
            → ``passed=True, reason="tests exercise the change (delta-gate overlap)"``

      candidate-passes / nothing-fails-on-base
            → ``passed=False, reason="tests-do-not-exercise-change"``

      base state cannot be evaluated (rc==-1, base uncollectable, no tests
      collected anywhere, or `pytest` not in `cmd`)
            → ``unverified=True`` (the caller abstains, not passes/fails)

    CALLER OWNS THE BASE WORKTREE — this helper only consumes the path it
    is given. Constructing the base (typically `git worktree add --detach
    HEAD <tmp>`) is the caller's responsibility because the caller's repo
    state, branch, and stash hygiene are not ours to manage.
    """
    if not cmd or not cmd.strip():
        return {"passed": False, "reason": "no command", "unverified": True, "replay": None}
    if "pytest" not in cmd:
        # We can only parse per-test results from pytest -v output. Other
        # runners (npm, cargo, go) have their own conventions; abstaining
        # here is the honest answer until a sibling helper exists.
        return {"passed": False, "reason": "replay supports pytest commands only",
                "unverified": True, "replay": None}
    if not base_cwd or not os.path.isdir(base_cwd):
        return {"passed": False, "reason": f"base cwd not a directory: {base_cwd!r}",
                "unverified": True, "replay": None}

    augmented = _ensure_pytest_verbose(cmd)
    cand_cwd = candidate_cwd or os.getcwd()
    cand_log = candidate_log or os.path.join(base_cwd, ".replay_candidate.log")
    b_log = base_log or os.path.join(base_cwd, ".replay_base.log")

    def _run(cwd: str, log: str) -> tuple[int, set[str], set[str]]:
        passed: set[str] = set()
        failed: set[str] = set()
        try:
            with open(log, "wb") as fh:
                rc = subprocess.run(
                    augmented, shell=True, cwd=cwd, env=scrubbed_test_env(),
                    stdout=fh, stderr=subprocess.STDOUT,
                ).returncode
        except OSError:
            return -1, passed, failed
        try:
            text = Path(log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return rc, passed, failed
        for m in _TEST_RESULT_RE.finditer(text):
            tid = m.group("id")
            st = m.group("status")
            if st == "PASSED":
                passed.add(tid)
            elif st in ("FAILED", "ERROR"):
                failed.add(tid)
        return rc, passed, failed

    _, cand_pass, _ = _run(cand_cwd, cand_log)
    base_rc, base_pass, base_fail = _run(base_cwd, b_log)

    if base_rc == -1:
        return {"passed": False, "reason": "base state could not be evaluated",
                "unverified": True, "replay": None}
    if base_rc != 0 and not (base_pass or base_fail):
        # rc!=0 AND no per-test lines parsed → pytest could not collect or
        # run. We cannot claim the base "passes" or "fails" by test, so we
        # abstain rather than silently fail or pass.
        return {
            "passed": False,
            "reason": f"base could not run (rc={base_rc}); cannot establish delta",
            "unverified": True,
            "replay": None,
        }
    total = len(cand_pass) + len(base_pass) + len(base_fail)
    if total == 0:
        return {"passed": False, "reason": "no tests collected; cannot establish delta",
                "unverified": True, "replay": None}

    overlap = cand_pass & base_fail
    info = {
        "candidate_passed": sorted(cand_pass),
        "base_failed": sorted(base_fail),
        "overlap": sorted(overlap),
    }

    if overlap:
        return {
            "passed": True,
            "reason": "tests exercise the change (delta-gate overlap)",
            "unverified": False,
            "replay": info,
        }
    return {
        "passed": False,
        "reason": "tests-do-not-exercise-change",
        "unverified": False,
        "replay": info,
    }


# ── Oracle judge ─────────────────────────────────────────────────────────────
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
        no_cands_reason = ("the probe passes, but no invariant that reproduces the bug could be built "
                           "-> cannot rule out a special-cased patch")
        if relations.enabled() and relations.rescue_enabled():
            rec = relations.check(poc_src, issue, patch, runner=runner,
                                  dispatch=d_fn, context=context, rescue=True)
            return Verdict(rec["verdict"] or UNVERIFIED,
                           rec["reason"] or no_cands_reason,
                           poc_plus=poc_src, detail={"relations": rec})
        return Verdict(UNVERIFIED, no_cands_reason, poc_plus=poc_src)

    kept, dropped, holds, broken, inconclusive = [], [], 0, 0, 0
    for name, src in cands:                               # already validated informative
        res = runner.run_test(src, patch=patch)
        hit = res.status == "passed"
        rec = {"mr": name, "on_patch": res.status, "holds": hit, "src": src}
        if hit:
            # Passing invariants do not need a base re-run: they hold on the
            # patch and would also hold on base (otherwise mr.build dropped them).
            rec["outcome"] = "holds"
            holds += 1
        else:
            # exc names the exception; output carries the assertion text an auditor needs.
            exc = res.exc if isinstance(getattr(res, "exc", ""), str) else ""
            out = getattr(res, "output", "")
            out = out if isinstance(out, str) else ""
            rec["detail"] = _tail(f"{exc}\n{out}".strip(), 600)
            # Cost note: one extra sandbox test run per failed invariant (the
            # base re-run on the un-patched source to compare signatures).
            # No extra model calls — the signature is a string compare.
            base = runner.run_test(src)                   # no patch — re-run on base
            sig_patch = _failure_signature(out)
            base_out = getattr(base, "output", "")
            base_out_str = base_out if isinstance(base_out, str) else ""
            sig_base = _failure_signature(base_out_str)
            base_exc = base.exc if isinstance(getattr(base, "exc", ""), str) else ""
            # Classify per certify C6: a failing invariant is "broken" only when
            # the patch did not change the failing behaviour (same signature on
            # patch and base — the cheat shape). Otherwise the patch changed the
            # behaviour the invariant tests, so the invariant is "inconclusive":
            # it cannot rule out a correct fix.
            if (base.status == "failed" and res.status == "failed"
                    and sig_patch is not None and sig_patch == sig_base):
                rec["outcome"] = "broken"
                broken += 1
            elif sig_patch is None or sig_base is None:
                # Cannot tell — conservative fallback matches today's behaviour
                # (counts as broken; cannot rule out a cheat).
                rec["outcome"] = "broken"
                broken += 1
            else:
                rec["outcome"] = "inconclusive"
                inconclusive += 1
            rec["base_detail"] = _tail(f"{base_exc}\n{base_out_str}".strip(), 600)
        if gold is not None:                              # MEASUREMENT ONLY — never a decision
            g = runner.run_test(src, patch=gold)
            v_ok, v_why = mr.valid(g)
            rec["valid_on_gold"], rec["gold_says"] = v_ok, v_why
        kept.append(rec)

    n = len(kept)
    n_eff = holds + broken
    rate = (holds / n) if n else 0.0
    verdict, reason = mr.score(rate, n)

    # Asymmetric verdict (certify C6). PROVEN is byte-identical to today's
    # mr.score on (rate, n) — precision cannot move; the change can only turn
    # a REFUTED into an UNVERIFIED (false reject becomes abstention).
    if verdict != PROVEN:
        if n_eff >= 2 and (holds / n_eff) < 0.5:
            # Conclusive evidence says the bug survives; re-format the reason
            # on the conclusive rate and conditionally append the exclusion
            # suffix so the audit trail names the excluded invariants.
            _, ref_reason = mr.score(holds / n_eff, n_eff)
            reason = ref_reason
            if inconclusive > 0:
                reason += f" ({inconclusive} inconclusive invariant(s) excluded)"
            verdict = REFUTED
        elif inconclusive > 0:
            # The patch changed the behaviour the inconclusive invariants
            # test; they cannot judge it. Surface that explicitly so the
            # auditor can rerun them with a corrected expectation.
            reason = (f"{inconclusive} of {n} invariants failed in a way the "
                      f"buggy code did not — the patch changed the behaviour "
                      f"they test, so they cannot judge it; {holds} of {n} hold")
            verdict = UNVERIFIED
        # else: keep mr.score's UNVERIFIED reason verbatim.

    # ── Term 4: metamorphic relations (veto a would-be PROVEN) ──────────────
    # A special-cased patch passes every invariant yet breaks a relation BETWEEN two
    # executions. This can only turn PROVEN -> REFUTED (it cannot create PROVEN, and it
    # never runs on a would-be REFUTED/UNVERIFIED). Knobs off => no dispatch, no key.
    detail = {"invariants": kept, "dropped": dropped,
              "n_effective": n_eff, "inconclusive": inconclusive}
    proven_before_vetoes = verdict == PROVEN
    if verdict == PROVEN and relations.enabled():
        rec = relations.check(poc_src, issue, patch, runner=runner,
                              dispatch=d_fn, context=context,
                              k=relations.k_from_env())
        detail["relations"] = rec
        if rec["verdict"] == REFUTED:
            verdict = REFUTED
            reason = rec["reason"]
    if proven_before_vetoes and differential.enabled():
        drec = differential.check(poc_src, issue, patch, runner=runner,
                                  dispatch=d_fn, context=context,
                                  n=differential.n_from_env())
        detail["differential"] = drec
        if drec["verdict"] == REFUTED and verdict == PROVEN:
            verdict = REFUTED
            reason = drec["reason"]

    return Verdict(verdict, reason, poc_plus=poc_src,
                   mr_pass_rate=(rate if n else None), mr_n=n,
                   detail=detail)


__all__ = ["judge", "replay_check"]