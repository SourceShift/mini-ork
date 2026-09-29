"""PoC+ — a reproduction test that is actually a reproduction.

WHY THIS EXISTS
───────────────
Measured: **4 of 5** PoC+ tests the oracle generated FAIL ON THE GOLD PATCH. A test that
fails on the correct fix fails on everything — it is not a verifier, it is a constant
REFUTE function. Seven of eleven oracle verdicts were issued with one, which is why the
"100% precision" result collapsed to two judgments on a single instance.

The four failures had four DIFFERENT causes, and only one was a syntax-level defect:

    sympy-13852     NameError: 'exp_polar' not imported      -> broken as CODE
    astropy-13579   ValueError from wcs_to_celestial_frame   -> broken SETUP; library is right
    astropy-13398   IERSWarning: failed to download ...      -> the sandbox is OFFLINE
    astropy-14369   (see below)

You cannot classify your way out of that. The dominant mode is a test whose setup is wrong,
where the library then raises a perfectly legitimate exception that is indistinguishable
from a real bug by exception type alone.

THE GATE THAT DOES WORK
───────────────────────
**A test must fail for the reason it was written for.**

The issue reports a wrong *value*. A probe that asserts that value should therefore fail on
the buggy code with an **AssertionError**. If it dies on a `ValueError` raised deep inside
the library's own setup, it never reached its assertion — it is red, but it is not
reproducing anything.

    valid reproduction  ⇔  base fails with AssertionError
                        ∨  base fails with the exception the ISSUE ITSELF NAMES
                           (some bugs *are* crashes: "this raises TypeError")

Both halves are mechanical and — crucially — **available at solve time**, where gold is not.
This is the PoC-vs-PoC+ distinction (arXiv 2603.06858, 42.3% false-discovery rate for naive
test validation) carried one step further:

    naive PoC      "it fails on base"                                    <- 42.3% FDR
    our old PoC+   "it fails on base, asserting an expected output"      <- still 80% invalid
    this           "it fails on base FOR THE REASON THE ISSUE DESCRIBES"

GROUNDING
─────────
The astropy-13579 probe hardcoded `(49.5, 12.0, 0.0)`. The issue states
`(array(49.5), array(12.), array(2.44249065e-15))`. It quoted two values and **invented the
third** — partial grounding, which is worse than none because it looks credible.

So the expected value must be QUOTED, and the quote is checked mechanically against the
issue text. A model that cannot quote must abstain, and abstention is a correct answer.

REPAIR
──────
A `test_defect` (broken as code) earns one repair attempt: we hand back the real traceback
and ask for a fix. That is legitimate at solve time — we are repairing the TEST, never
peeking at the fix.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

from mini_ork.runtime import ExecOutcome

# Callable[[str], str]  — prompt -> text. Default = default_dispatch (lazy import to
# avoid an import cycle when llm.py imports the dispatcher).
DispatchFn = Callable[[str], str]


def _default_dispatch() -> DispatchFn:
    from mini_ork.certify.llm import default_dispatch

    return default_dispatch


# ── prompt block extraction ──────────────────────────────────────────────────
def _block(txt: str, lang: str = "python") -> str:
    m = re.search(rf"```(?:{lang})?\s*\n(.*?)```", txt, re.S)
    return (m.group(1) if m else "").strip()


def _json(txt: str) -> dict | None:
    m = re.search(r"```(?:json)?\s*\n(.*?)```", txt, re.S) or re.search(r"(\{.*\})", txt, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except Exception:
        return None


# ── Step 1: GROUND the expected behaviour in the issue, or abstain ───────────
GROUND_PROMPT = """Read this bug report and extract, VERBATIM, the passage where the reporter
states what the CORRECT behaviour should be (the expected output/value/result).

ISSUE:
{issue}

Reply with ONLY a JSON object:

{{
  "states_expected_behaviour": true | false,
  "quote": "<the EXACT text from the issue, copied character-for-character, showing the expected result>",
  "expected_summary": "<one sentence: what SHOULD happen>",
  "reported_exception": "<if the bug is that an exception is RAISED, its type name, e.g. TypeError; else empty>"
}}

RULES — these matter more than being helpful:
  - "quote" must be COPIED from the issue. Do not paraphrase, do not round numbers, do not
    tidy formatting. It will be checked character-for-character against the issue text.
  - If the issue never says what the correct result should be, set
    states_expected_behaviour=false and leave quote empty. That is a CORRECT answer.
    Do NOT guess. A fabricated expectation produces a test that rejects even the correct fix."""


def ground(issue: str, dispatch: DispatchFn | None = None) -> dict | None:
    """Return the grounded expectation, or None to abstain.

    The quote is verified MECHANICALLY against the issue. A model that half-quotes and
    half-invents (as ours did: it copied 49.5 and 12., then replaced 2.44249065e-15 with
    0.0) is caught here, not three container runs later.
    """
    d_fn = dispatch or _default_dispatch()
    g = _json(d_fn(GROUND_PROMPT.format(issue=issue[:6000])))
    if not g or not g.get("states_expected_behaviour"):
        return None
    q = (g.get("quote") or "").strip()
    if not q:
        return None

    # mechanical citation check — the quote must actually appear in the issue
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    if norm(q) not in norm(issue):
        # allow a slightly lenient match on long quotes (line-wrapping in the report)
        lines = [norm(x) for x in q.splitlines() if len(norm(x)) > 12]
        if not lines or not all(x in norm(issue) for x in lines):
            return None          # FABRICATED — abstain rather than test against a lie
    return g


# ── Step 2: write the probe FROM the grounded quote ──────────────────────────
POC_PROMPT = """Write ONE pytest test that reproduces this bug.

ISSUE:
{issue}

THE REPORTER'S OWN STATEMENT OF THE CORRECT BEHAVIOUR (verbatim from the issue):
{quote}

Your test must:
  - run what the reporter ran,
  - ASSERT the expected result EXACTLY as quoted above — do not round, do not approximate,
    do not substitute a "cleaner" number,
  - therefore FAIL on the current buggy code with an **AssertionError**,
  - and PASS once the bug is properly fixed.

Hard requirements:
  - IMPORT EVERY NAME YOU USE. (A previous probe used `exp_polar` without importing it and
    died with a NameError — which was then misread as "the bug is not fixed".)
  - No network access. The sandbox is OFFLINE — anything that downloads will fail.
  - Keep the setup minimal. If your setup crashes, the test proves nothing.

Output ONLY the test in a ```python``` block. No prose."""


def write(issue: str, g: dict, dispatch: DispatchFn | None = None) -> str | None:
    d_fn = dispatch or _default_dispatch()
    code = _block(d_fn(POC_PROMPT.format(issue=issue[:3500], quote=g["quote"][:1200])))
    return code if "def test" in code else None


# ── Step 3: the REPRODUCTION GATE (solve-time; no gold needed) ───────────────
def reproduces(base: ExecOutcome, issue: str, g: dict) -> tuple[bool, str]:
    """Did the probe fail ON BASE for the reason the ISSUE describes?

    This is the gate the old oracle lacked. It accepted any red test, so a probe that
    crashed in its own setup was read as a reproduction — and then, when the patched run
    crashed the same way, as proof the bug was not fixed.
    """
    if base.status == "test_defect":
        return False, f"the TEST is broken ({base.exc}) — repair it; it says nothing about the code"
    if base.status != "failed":
        return False, f"probe is not red on base (outcome={base.status}) — it reproduces nothing"

    # A probe that asserts a value must fail on the ASSERTION.
    if base.exc in ("AssertionError", "Failed"):
        return True, "reproduces: the asserted expected value does not hold on the buggy code"

    # ...unless the bug IS a crash, and the issue says so.
    named = (g.get("reported_exception") or "").strip()
    if named and base.exc == named:
        return True, f"reproduces: raises {base.exc}, exactly as the issue reports"
    if base.exc and re.search(rf"\b{re.escape(base.exc)}\b", issue):
        return True, f"reproduces: raises {base.exc}, which the issue names"

    # Red, but for a reason nobody reported. It never reached its assertion.
    return False, (f"probe fails with {base.exc or 'an unexpected error'}, which the issue never "
                   f"mentions — it broke before testing anything, so it is NOT a reproduction")


# ── Step 4: repair a test that is broken as CODE ─────────────────────────────
REPAIR_PROMPT = """Your pytest test is BROKEN. It did not fail because of the bug — it failed
because the test itself is wrong.

THE TEST:
```python
{poc}
```

WHAT ACTUALLY HAPPENED:
```
{failure}
```

Fix the TEST. Do not change what it asserts — the assertion is correct. Fix the defect
(missing import, wrong name, bad setup, network access).

The sandbox is OFFLINE: no downloads.

Output ONLY the corrected test in a ```python``` block."""


def repair(poc: str, o: ExecOutcome, dispatch: DispatchFn | None = None) -> str | None:
    d_fn = dispatch or _default_dispatch()
    code = _block(d_fn(REPAIR_PROMPT.format(poc=poc[:2500], failure=o.output[-1200:])))
    return code if "def test" in code else None


# ── the whole thing ──────────────────────────────────────────────────────────
def build(
    issue: str,
    run: Callable[[str], ExecOutcome],
    *,
    dispatch: DispatchFn | None = None,
    max_repairs: int = 1,
    tries: int = 2,
) -> tuple[str | None, str]:
    """Return (poc_plus, reason). `run(src) -> ExecOutcome` executes on the BUGGY code.

    Abstention is a first-class result — but only after we have genuinely TRIED. Probe
    generation is stochastic: measured, the model wrote no usable probe for sympy-13091 and
    a probe that crashed for the wrong reason on sympy-12419, yet BOTH issues plainly state
    the expected behaviour (`NotImplemented not False`; sum `== n not 0`). Those are flaky
    generations, not honest abstentions, and giving up after one shot throws away real
    recall. So we retry the whole ground->write->reproduce loop before abstaining.

    We do NOT retry when the issue itself states no expected behaviour: that abstention is
    correct and no amount of retrying changes it.
    """
    g = ground(issue, dispatch=dispatch)
    if not g:
        return None, "the issue does not state the expected behaviour (or the model could not quote it) -> abstain"

    last = "could not write a probe"
    for _ in range(tries):
        poc = write(issue, g, dispatch=dispatch)
        if not poc:
            last = "could not write a probe"
            continue
        # inner loop repairs a probe that is broken as CODE (test_defect)
        for attempt in range(max_repairs + 1):
            o = run(poc)
            ok, why = reproduces(o, issue, g)
            if ok:
                return poc, why
            last = why
            if o.status != "test_defect" or attempt == max_repairs:
                break            # not a code defect (or out of repairs) -> regenerate from scratch
            fixed = repair(poc, o, dispatch=dispatch)
            if not fixed:
                break
            poc = fixed

    return None, last