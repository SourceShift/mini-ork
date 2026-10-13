"""Unit tests for ``_cap_reflect_to_deadline``.

Hermetic and direct: the helper is pure (no I/O, no env reads, no
``deadline_budget`` call), so every case is one function call with three
numbers and no fixture at all. The ``deadline_budget.status`` lookup — the
only impure half of the cap — lives at the call site in
``_run_lifecycle_impl``, guarded and best-effort; it is deliberately not
exercised here.

Cases (a)-(e) mirror the verification contract:

  (a) no deadline armed (``deadline_seconds <= 0``)  -> bound and reason
      pass through untouched.
  (b) a remainder comfortably larger than the bound  -> passthrough too; a
      cap that would not shorten anything must not rewrite the reason.
  (c) a remainder smaller than the bound             -> the bound becomes
      ``remaining + _REFLECT_DEADLINE_GRACE``.
  (d) a spent or already-tripped deadline
      (``remaining <= 0`` / ``hit=True``)            -> exactly the grace,
      never negative.
  (e) whenever the cap bites, the reason names the rule.
"""
from __future__ import annotations

from mini_ork.cli.main import _REFLECT_DEADLINE_GRACE, _cap_reflect_to_deadline

BOUND = 5000
WHY = "3x the slowest of 20 recent pass(es) (1800s), ceiling 21600s"


def test_no_deadline_armed_is_passthrough():
    """(a) Without a --deadline there is no remainder to cap against."""
    assert _cap_reflect_to_deadline(
        BOUND, WHY, deadline_seconds=0, remaining_seconds=0, hit=False,
    ) == (BOUND, WHY)
    assert _cap_reflect_to_deadline(
        BOUND, WHY, deadline_seconds=-1, remaining_seconds=1234, hit=False,
    ) == (BOUND, WHY)


def test_roomy_remainder_leaves_bound_and_reason_alone():
    """(b) A cap above the bound is not a cap — do not touch the reason."""
    assert _cap_reflect_to_deadline(
        BOUND, WHY, deadline_seconds=36000, remaining_seconds=9000, hit=False,
    ) == (BOUND, WHY)


def test_equal_cap_is_not_applied():
    """A cap exactly equal to the bound changes nothing, so it stays out."""
    assert _cap_reflect_to_deadline(
        BOUND, WHY,
        deadline_seconds=600,
        remaining_seconds=BOUND - _REFLECT_DEADLINE_GRACE,
        hit=False,
    ) == (BOUND, WHY)


def test_tight_deadline_caps_to_remainder_plus_grace():
    """(c) The ordinary bite: bound = remaining + grace."""
    bound, why = _cap_reflect_to_deadline(
        BOUND, WHY, deadline_seconds=600, remaining_seconds=180, hit=False,
    )
    assert bound == 180 + _REFLECT_DEADLINE_GRACE
    assert bound < BOUND
    assert why.startswith("capped to the --deadline remainder")
    assert why.endswith(WHY), "the original derivation must stay readable"


def test_capped_reason_names_the_rule():
    """(e) The reason survives round-trip into reflect.log verbatim."""
    _, why = _cap_reflect_to_deadline(
        BOUND, WHY, deadline_seconds=600, remaining_seconds=1, hit=False,
    )
    assert "capped to the --deadline remainder" in why
    assert f"+{_REFLECT_DEADLINE_GRACE}s grace" in why
    assert why.endswith(WHY)


def test_spent_deadline_caps_at_grace_never_negative():
    """(d) remaining <= 0 must clamp to the grace, not go negative."""
    for remaining in (0, -1, -9999):
        bound, why = _cap_reflect_to_deadline(
            BOUND, WHY,
            deadline_seconds=600, remaining_seconds=remaining, hit=True,
        )
        assert bound == _REFLECT_DEADLINE_GRACE
        assert bound > 0
        assert "capped to the --deadline remainder" in why


def test_hit_flag_is_annotated_but_keeps_the_rule_name():
    """A tripped deadline is called out, and the capping rule still named."""
    _, why = _cap_reflect_to_deadline(
        BOUND, WHY, deadline_seconds=600, remaining_seconds=-5, hit=True,
    )
    assert "capped to the --deadline remainder" in why
    assert "deadline already hit" in why


def test_grace_is_the_documented_constant():
    """The 120 s grace is the one the plan pins; guard against drift."""
    assert _REFLECT_DEADLINE_GRACE == 120
