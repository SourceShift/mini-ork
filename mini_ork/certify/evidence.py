"""Coherence predicate for execution evidence.

The oracle reads ``ExecOutcome.status == "passed"`` as proof. A runner that
*honestly* reports a pass is corroborated by its own fields: it ran, its
process exited 0, and (when it records any) its attempt/apply/execution
bookkeeping agrees. A runner that reports ``passed`` while its own fields say
otherwise has handed over self-contradictory evidence — a harness fact, never
proof about the patch.

This module names that contradiction. It is deliberately small and defensive:

* It only speaks about a *claimed* pass. For any other status it returns
  ``None``: a failed/error/apply_fail outcome is already handled by the judge
  and must never be re-labelled here.
* It reads only fields the caller actually populated. A real ``ExecOutcome``
  sets no ``meta`` and uses ``returncode=0`` on a pass, so an honest pass is
  never flagged. ``ExecOutcome.ran`` defaults to ``False``, so an outcome that
  populated NO run evidence (no returncode, empty meta) cannot contradict a
  pass merely by carrying that default — the ``ran`` rule fires only when a
  returncode or meta says the runner actually measured something.
* Missing attributes, a non-mapping ``meta``, absent keys, and unknown values
  never raise. A predicate that crashes on malformed evidence would turn a
  judge bug into an outage; an unreadable field simply cannot contradict the
  status.

The caller decides what a defect means. In the oracle an incoherent pass is an
*abstention* (UNVERIFIED), never REFUTED — a harness fact must not blame the
patch.

``defect(outcome)`` returns a short reason string (or ``None`` when coherent);
``is_trustworthy_pass(outcome)`` is the boolean mirror ``defect(outcome) is None``.

The one non-passed case that also belongs here is a *broken probe*.
``broken_probe(outcome)`` names a failure that measured nothing — the runner
reported the test itself as defective — so the judge abstains instead of blaming
the patch. It speaks only about non-passed outcomes; a pass stays ``defect``'s
question.

Import-time contract: stdlib only, mirroring ``mini_ork.gates.verifier_mutation``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["broken_probe", "defect", "is_trustworthy_pass"]

#: Exact success words a corroborating field may carry. A near miss ("ok.",
#: "successful") is NOT a success word: an unknown value cannot support the
#: aggregate claim, so it is a defect.
_SUCCESS_WORDS = frozenset(
    {"ok", "passed", "pass", "applied", "clean", "success", "succeeded"}
)

#: Distinguishes "key absent" from "key present with a falsy/None value".
_MISSING = object()


def _field(outcome, name, default=None):
    """``getattr`` that never raises, even for a property that raises."""
    try:
        return getattr(outcome, name, default)
    except Exception:  # noqa: BLE001 - a broken attribute is unreadable, not fatal
        return default


def _meta_of(outcome) -> Mapping:
    """The outcome's ``meta`` when it is a mapping, else an empty mapping."""
    meta = _field(outcome, "meta")
    return meta if isinstance(meta, Mapping) else {}


def _is_success(value) -> bool:
    """True iff ``value`` is one of the exact success words (strings only)."""
    return isinstance(value, str) and value in _SUCCESS_WORDS


def _meta_get(meta: Mapping, key: str):
    """``meta.get(key)`` that returns ``_MISSING`` instead of raising."""
    try:
        return meta.get(key, _MISSING)
    except Exception:  # noqa: BLE001 - an unreadable mapping says nothing
        return _MISSING


def _truthy(value) -> bool:
    """``bool(value)`` that never raises on a misbehaving object."""
    try:
        return bool(value)
    except Exception:  # noqa: BLE001 - an unreadable value cannot be trusted true
        return False


def _attempt_items(value):
    """The entries of an ``attempts`` sequence, or ``None`` when it is not one.

    Strings/bytes are sequences of characters and are not attempt records; an
    object that is not a sequence is unreadable here and says nothing.
    """
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return None
    try:
        return list(value)
    except Exception:  # noqa: BLE001 - a broken sequence is unreadable
        return None


def defect(outcome) -> str | None:
    """Return why a claimed ``passed`` outcome contradicts itself, else ``None``.

    Only an outcome whose status is exactly ``"passed"`` is examined. Each rule
    is independent: the first contradiction found is returned, so the reason
    names one concrete field rather than a summary.
    """
    if _field(outcome, "status") != "passed":
        # A non-passed status is already the judge's concern — do not relabel.
        return None

    meta = _meta_of(outcome)
    returncode = _field(outcome, "returncode")

    # Rule 1: it says passed, but it also says it never ran. ``ExecOutcome``
    # defaults ``ran`` to False, so only a caller that actually POPULATED run
    # evidence (a returncode, any meta) can contradict the claim. A bare
    # ``ExecOutcome(status="passed")`` carries no execution fields to dispute.
    if _field(outcome, "ran") is False and (
        returncode is not None or _truthy(_field(outcome, "meta"))
    ):
        return "the outcome claims passed but ran is False"

    # Rule 2: the process exited non-zero. ``None`` means unrecorded, not failure.
    if returncode is not None and returncode != 0:
        return f"the outcome claims passed but returncode is {returncode!r}"

    # Rule 3: the patch-application bookkeeping is not a success word.
    apply_status = _meta_get(meta, "apply_status")
    if apply_status is not _MISSING and not _is_success(apply_status):
        return (
            "the outcome claims passed but meta['apply_status'] is "
            f"{apply_status!r}, not a success word"
        )

    # Rule 4: the final retry attempt's status is not a success word.
    final_attempt = _meta_get(meta, "final_attempt_status")
    if final_attempt is not _MISSING and not _is_success(final_attempt):
        return (
            "the outcome claims passed but meta['final_attempt_status'] is "
            f"{final_attempt!r}, not a success word"
        )

    # Rule 5: attempts were recorded, none of them succeeded, yet the aggregate
    # says passed. An empty (or absent) sequence says nothing about the aggregate.
    attempts = _attempt_items(_meta_get(meta, "attempts"))
    if attempts and not any(_is_success(entry) for entry in attempts):
        return "the outcome claims passed but no attempt in meta['attempts'] succeeded"

    # Rule 6: zero tests executed yet the aggregate says passed.
    executed = _meta_get(meta, "executed")
    if executed is not _MISSING and isinstance(executed, int) and executed == 0:
        return "the outcome claims passed but meta['executed'] is 0"

    # Rule 7: the harness itself flagged the test as defective.
    test_defect = _meta_get(meta, "test_defect")
    if test_defect is not _MISSING and _truthy(test_defect):
        return "the outcome claims passed but meta['test_defect'] is truthy"

    # Rule 8: the recorded evidence was computed for a different input and
    # replayed. Both sides must be present, non-empty strings to compare.
    input_digest = _meta_get(meta, "input_digest")
    candidate_digest = _meta_get(meta, "candidate_input_digest")
    if (
        isinstance(input_digest, str)
        and input_digest
        and isinstance(candidate_digest, str)
        and candidate_digest
        and input_digest != candidate_digest
    ):
        return (
            "the outcome claims passed but the evidence was computed for a "
            "different input digest"
        )

    return None


def broken_probe(outcome) -> str | None:
    """Return why a non-``passed`` outcome measured nothing, else ``None``.

    A probe that broke measures neither the base nor the patch, so its failure
    is a harness fact and must not refute a patch — the right verdict is an
    abstention. Two producers report that same fact under different status
    words, and the judge must honour both:

    * ``mini_ork.runtime.engine`` emits ``status="test_defect"`` — the harness
      itself decided the test is defective;
    * a runner whose spec raised before it could assert (a ``NameError``, a
      missing fixture) emits ``status="failed"`` — the process really did fail —
      and flags ``meta["test_defect"]``, because the status word alone cannot
      tell "the bug reproduced" from "the test is broken".

    Only the second channel needs the metadata: a plain ``failed`` with no flag
    is a reproduction and stays the judge's evidence against the patch. A
    ``passed`` outcome is never inspected here; that is :func:`defect`'s job.
    """
    status = _field(outcome, "status")
    if status == "passed":
        return None
    if status == "test_defect":
        return "the runner reported a defective test"
    flag = _meta_get(_meta_of(outcome), "test_defect")
    # ``_MISSING`` is an object, so it is truthy: an absent flag must be tested
    # for identity, never for truthiness.
    if flag is not _MISSING and _truthy(flag):
        return "the outcome is flagged meta['test_defect']"
    return None


def is_trustworthy_pass(outcome) -> bool:
    """True iff ``outcome`` claims a pass and no field contradicts the claim."""
    return defect(outcome) is None
