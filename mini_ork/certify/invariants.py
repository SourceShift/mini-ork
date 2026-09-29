"""Metamorphic amplification — the layer that catches a patch which passes the test and is
still wrong.

WHAT IT IS FOR
───────────────
A single execution test is an *extensional verifier*, and it IS gamed: a patch can
special-case the exact input in the test (~73.6% shortcut rate under compute pressure,
arXiv 2604.15149). Measured here, on a real hard negative: kimi gated its "fix" on

    type(self).__name__ == "SelectKBest"
      and list(X.columns) == ["sepal length (cm)", ..., "cat"]     # OUR probe's columns
      and str(X["cat"].dtype) == "category"

...and the reproduction test went green. The delta gate would have SHIPPED it.
Amplification caught it. That is the whole moat, and it is the only layer that can be.

THE BUG THIS FILE FIXES
───────────────────────
Amplification looked broken: 0 true positives, 1 false positive. It was not. It was being
scored against **a broken test** — exactly the disease we had just fixed one level down in
the PoC+.

Validating each of its four invariants against base AND gold:

    mr_1   base:failed  gold:passed   informative
    mr_2   base:failed  gold:FAILED   *** OVER-SPECIFIED ***  asserts ColumnTransformer
                                       preserves dtypes — GOLD DOES NOT DO THAT EITHER
    mr_3   base:failed  gold:passed   informative
    mr_4   base:failed  gold:passed   informative   <- the discriminator

Drop the invalid one:  correct patch 3/3 -> PROVEN.  cheat 2/3 -> REFUTED.
The false positive vanishes; the true positive survives.

THE LAW
───────
    **A generated test is evidence only if it FAILS ON BASE (it is about the bug)
      and PASSES ON GOLD (it is not over-specified).**

Skip that and you are not measuring the patch. You are measuring your test generator, and
calling the result a verifier.

SOLVE TIME (no gold available)
──────────────────────────────
  - fails-on-base IS checkable      -> drop MRs that pass on base: they are not about the
                                       bug, so they are uninformative noise.
  - over-specification is NOT       -> attack it from both ends:
       (a) GENERATION: force every invariant to stay inside the API the ISSUE NAMES.
           mr_2 wandered off to ColumnTransformer, which the issue never mentions.
       (b) SCORING: require a MAJORITY, not unanimity. A correct fix satisfies almost all
           invariants; a special-cased patch fails most of them. One rogue MR must not be
           able to reject a correct patch.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from mini_ork.certify.context import CodeContext, imports_code_under_test
from mini_ork.runtime import ExecOutcome

# Callable[[str], str]  — prompt -> text. Default = default_dispatch (lazy import to
# avoid an import cycle when llm.py imports the dispatcher).
DispatchFn = Callable[[str], str]


def _default_dispatch() -> DispatchFn:
    from mini_ork.certify.llm import default_dispatch

    return default_dispatch


def _code(txt: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", txt, re.S)
    return (m.group(1) if m else "").strip()


MR_PROMPT = """Here is a correctness test for a bug fix:

```python
{poc}
```

THE ISSUE:
{issue}

THE CANDIDATE PATCH UNDER TEST (this is what you must try to BREAK):
```diff
{patch}
```

This patch passes the test above. Your job is to decide whether it *actually fixes the bug*
or merely *special-cases the tested input*. Read the patch adversarially:

  - Does it branch on a SPECIFIC value, type, name, shape, or length? (`if x == 5`,
    `if type(self).__name__ == "Foo"`, `if len(cols) == 4`, a hardcoded constant returned.)
  - Does it only touch the ONE code path the test exercises, leaving siblings untouched?

A REAL example: a patch branched on `type(self).__name__ == "SelectKBest"` AND on the literal
column names from the test. It passed the test and every generic invariant — because none of
them used a DIFFERENT class or DIFFERENT columns.

Write {n} pytest tests that exercise the SAME reported bug on inputs chosen to AVOID whatever
this patch special-cases — different values, a different type/subclass, a different size, a
second code path that must agree with the first. A correct general fix passes them all; THIS
patch, if it is narrow, fails at least one.

(If the patch looks like a GENUINE general fix with no special-casing, that is fine — just
write {n} strong invariants of the correct behavior on VARIED inputs anyway. Never return
fewer than {n}; a general fix will pass them and that is the correct outcome.)

THREE HARD RULES (violate any and the check is worthless):
  1. **Still reproduces the bug.** Each test's inputs must make the ORIGINAL, UNPATCHED code
     fail — i.e. they must be in the bug's domain. If you pick inputs so different that the
     unpatched code already handles them, the test proves nothing and is useless. Stay where
     the bug actually manifests; just move OFF the patch's specific cases.
  2. **Adversarial to the PATCH.** Within the bug's domain, pick inputs the patch's specific
     branches do NOT cover, so passing requires actually fixing the bug, not matching the
     patch's cases.
  3. **True of the CORRECT fix.** Every invariant must be something the fix *described in the
     issue* would make pass. If unsure the correct fix passes it, do NOT write it — an invariant
     the correct fix fails rejects correct work. Stay within the API the issue is about.

Also: IMPORT EVERY NAME YOU USE. No network — the sandbox is OFFLINE.

Output ONLY python in a ```python``` block: {n} functions named test_mr_1 .. test_mr_{n},
with all imports at the top."""

# Appended to MR_PROMPT when a CodeContext surfaces the code under test.
# Mirrors the probe-side block: prompt the model with the BASE source so each
# invariant imports from the repo instead of redefining the function under test.
MR_CONTEXT_SUFFIX = """

THE CODE UNDER TEST (the buggy version, before any fix):
{context_text}

Every test below MUST import the code under test from the repository (e.g. `from {module_example} import <name>`)
and call it. NEVER copy, re-implement, or redefine the code under test in the test file —
a test of a copy proves nothing about the repository. The structural guard rejects
candidates that do not import the code under test, so they will be discarded."""


def _example_module(modules: tuple[str, ...]) -> str:
    """Pick a representative module for the prompt example — first entry, parent preferred."""
    if not modules:
        return "<module>"
    first = modules[0]
    return first.split(".")[0] if "." in first else first


def build(
    poc: str,
    issue: str,
    patch: str = "",
    *,
    n: int = 4,
    tries: int = 4,
    base_runner: Callable[[str], ExecOutcome] | None = None,
    dispatch: DispatchFn | None = None,
    want: int = 2,
    context: CodeContext | None = None,
) -> list[tuple[str, str]]:
    """Generate invariants and return the INFORMATIVE ones — [(name, standalone_source), ...].

    Each is a standalone runnable file so it can be judged individually; one rogue invariant
    must never sink the others (that is how a correct patch once got rejected).

    THE ROOT-CAUSE FIX (matplotlib-23299 FC): validate informativeness HERE, in the retry
    loop, using `base_runner` (executes an invariant on the UNPATCHED code). An invariant is
    only kept if it FAILS on base — i.e. it actually reproduces the bug. This makes generation
    robust for BOTH patches:
      - shown a special-cased CHEAT, the model writes invariants avoiding its cases -> the
        cheat fails them -> REFUTED (closes the FC);
      - shown a CORRECT patch, the model can waffle and emit tests that pass on base; we
        DROP those and regenerate with feedback until we have `want` that truly fail on base,
        so the correct patch gets a real informative set -> PROVEN (no recall regression).

    When `context` carries `modules`, candidates that fail `imports_code_under_test`
    are dropped BEFORE base validation (a candidate that re-implements the function
    under test proves nothing about the repo and would otherwise be silently filtered
    here only because it happens to fail on base).

    Falls back to un-validated generation if no base_runner is given (keeps old callers working).
    """
    d_fn = dispatch or _default_dispatch()
    kept: list[tuple[str, str]] = []
    feedback = ""
    suffix = ""
    if context is not None and context.text:
        suffix = MR_CONTEXT_SUFFIX.format(
            context_text=context.text,
            module_example=_example_module(context.modules),
        )
    for _ in range(tries):
        prompt = MR_PROMPT.format(poc=poc[:1400], issue=issue[:1800],
                                  patch=(patch or "(not provided)")[:2500], n=n) + suffix + feedback
        src = _code(d_fn(prompt))
        cands = split(src) if src.count("def test") >= 2 else []
        # Drop candidates that re-implement the code under test — they prove nothing.
        if context is not None and context.modules:
            cands = [(name, s) for name, s in cands
                     if imports_code_under_test(s, context.modules)]
        if base_runner is None:
            if cands:
                return cands           # legacy path: no validation available
            continue
        # keep only invariants that FAIL on the unpatched code (they reproduce the bug)
        fresh = []
        for name, s in cands:
            if name in {k for k, _ in kept}:
                continue
            base = base_runner(s)
            if base.status == "failed":          # informative: reproduces the bug
                fresh.append((name, s))
        kept.extend(fresh)
        if len(kept) >= want:
            return kept
        feedback = ("\n\nIMPORTANT: some of your previous tests PASSED on the buggy, unpatched "
                    "code — those prove nothing and were discarded. Every test MUST fail on the "
                    "original unpatched code (it must actually trigger the reported bug). Write "
                    "new ones whose inputs genuinely reproduce the bug.")
    return kept


def split(src: str) -> list[tuple[str, str]]:
    """Split a multi-test file into standalone (name, source) pairs, each carrying the
    shared import header."""
    parts = re.split(r"\n(?=def test_mr_)", src)
    head = parts[0].split("def test_mr_")[0]
    out = []
    for p in parts:
        if "def test_mr_" not in p:
            continue
        body = "def test_mr_" + p.split("def test_mr_", 1)[1]
        name = body.split("(")[0].replace("def ", "").strip()
        out.append((name, head + body))
    return out


# ── the gate ─────────────────────────────────────────────────────────────────
def informative(base: ExecOutcome) -> tuple[bool, str]:
    """Is this invariant ABOUT THE BUG? Checkable at solve time — no gold needed.

    An invariant that already holds on the buggy code tells you nothing about whether the
    patch generalises. Counting it only dilutes the signal.
    """
    if base.status == "test_defect":
        return False, f"the invariant is broken as code ({base.exc})"
    if base.status == "passed":
        return False, "already holds on the BUGGY code — not about this bug, so uninformative"
    if base.status != "failed":
        return False, f"could not run on base ({base.status})"
    return True, "fails on base — it exercises the bug"


def valid(gold: ExecOutcome) -> tuple[bool, str]:
    """Is this invariant TRUE OF THE CORRECT FIX? Corpus-time only (gold is unavailable at
    solve time).

    An invariant the correct fix does not satisfy is OVER-SPECIFIED. It is not a detector;
    it is a broken test, and it will reject the correct answer.
    """
    if gold.status == "passed":
        return True, "holds on gold — a real invariant of the fix"
    return False, f"OVER-SPECIFIED: fails on GOLD too ({gold.status}/{gold.exc}) — a broken test, not a detector"


# ── scoring: majority, not unanimity ─────────────────────────────────────────
PROVEN, REFUTED, UNVERIFIED = "PROVEN", "REFUTED", "UNVERIFIED"


# PROVEN bar: SUPERMAJORITY (>= 2/3), not unanimity. Measured on 25 correct + 12 cheat patches
# (widen v2): with ADVERSARIAL invariants, cheats fail almost everything (all 12 at mr <= 0.25)
# while correct fixes cluster at mr >= 2/3. Unanimity (1.0) was throwing away every correct fix
# that tripped ONE over-specified invariant (9 of 25 correct patches -> UNVERIFIED for nothing).
# 2/3 recovers them with ZERO cheats crossing it — the two distributions are cleanly separated.
_PROVEN_BAR = 2.0 / 3.0


def score(pass_rate: float, n: int) -> tuple[str, str]:
    """Turn the invariant pass-rate into a verdict.

    The safety comes from ADVERSARIAL invariant generation (invariants targeted at the patch's
    special-cases), which drives a special-cased patch's pass-rate to ~0. Given that, a
    supermajority PROVEN bar is safe AND recovers correct patches that a single over-specified
    invariant would otherwise veto. A single rogue test must never sink a correct answer.
    """
    if n == 0:
        return UNVERIFIED, "no informative invariant could be built -> cannot rule out a special-cased patch"
    if pass_rate >= _PROVEN_BAR - 1e-9:
        return PROVEN, f"the fix holds across {pass_rate:.0%} of {n} perturbed inputs (supermajority)"
    if pass_rate < 0.5:
        return REFUTED, (f"the fix does not generalise — only {pass_rate:.0%} of {n} invariants hold, "
                         "which is the signature of a patch that special-cases the reported input")
    return UNVERIFIED, (f"{pass_rate:.0%} of {n} invariants hold — below the supermajority bar but not "
                        "a clear failure. We do not guess.")