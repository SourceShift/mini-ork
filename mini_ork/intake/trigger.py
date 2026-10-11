"""When a ticket becomes work — one rule set, and one safe default.

The intake poller can start a run from a mirror row. Pointed at a real board
that is a loaded gun: a source with no rules would walk the whole backlog and
launch an agent per ticket. So the rules are **all opt-in**, and with none of
them configured :func:`matches_trigger` refuses — ``(False, "no trigger
configured")``. A half-configured source starts *nothing* rather than
everything, and the refusal is a value the caller reports, not an exception it
swallows. Pinned by ``tests/unit/test_intake_trigger.py``.

Four settings, all namespaced under the source they belong to in
``config/pm.yaml``:

``trigger_label``
    The ticket must carry this label. The one rule that works on every source
    that has labels, and the one a team uses: ``agent``, ``ai-ready``.
``trigger_assignee``
    The ticket must be assigned to this login. "I assigned it to the bot" is a
    clearer act than applying a label, and every source has it.
``trigger_states``
    Normalized states the ticket must be in, comma-separated. Rarely needed —
    the candidate query already reads open work only — but it is the rule that
    says ``todo`` and not ``backlog``, i.e. "promised" rather than "known".
``trigger_recipe``
    Not a rule: the recipe the launched run should use. It selects nothing, so
    setting it alone still leaves the source unconfigured.

Rules are ANDed: configuring a label and an assignee means both must hold.

The rules are read from *any* object carrying ``state``, ``labels`` and
``assignee`` — a :class:`~mini_ork.pm.store.IssueRow` from the mirror as well as
a full :class:`~mini_ork.pm.model.Issue`. That is not incidental: it lets the
poller filter the mirror without a network call per ticket, so a full board
costs one HTTP request (the sync) rather than one per row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from mini_ork.pm.config import PmConfigError
from mini_ork.pm.model import IssueState

#: What :func:`matches_trigger` says when the source declares no rule at all.
#: A named constant because the test, the CLI and the dry-run report all quote
#: the same sentence, and a test that compares a copy of a string is a test
#: that passes after the string changes.
NO_TRIGGER = "no trigger configured"

#: The keys :meth:`Trigger.from_settings` reads, for the config comment and for
#: anyone wondering what a source may declare.
SETTING_KEYS = (
    "trigger_label",
    "trigger_assignee",
    "trigger_states",
    "trigger_recipe",
)


class Triggerable(Protocol):
    """The three fields a rule reads. Both the mirror row and the issue have them."""

    state: IssueState
    labels: Sequence[str]
    assignee: str | None


@dataclass(frozen=True, slots=True)
class Trigger:
    """One source's rules. All ``None``/empty is the safe, inert default."""

    label: str | None = None
    assignee: str | None = None
    #: A tuple, not a set, and deliberately: ``IssueState`` is a ``StrEnum``
    #: whose members hash by *name*, so a set lookup against a plain string
    #: silently misses. A tuple compares by ``==``, which is the equality the
    #: members actually have. See the same trap in
    #: :func:`mini_ork.pm.providers.github._requested_states`.
    states: tuple[IssueState, ...] = ()
    recipe: str | None = None

    @property
    def configured(self) -> bool:
        """Whether any rule selects work — ``recipe`` alone does not."""
        return self.label is not None or self.assignee is not None or bool(self.states)

    def to_dict(self) -> dict[str, Any]:
        """For the CLI's ``--json`` and the status view."""
        return {
            "label": self.label,
            "assignee": self.assignee,
            "states": [str(state) for state in self.states],
            "recipe": self.recipe,
            "configured": self.configured,
        }

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> Trigger:
        """Read the four keys out of a source's ``settings`` block.

        Raises on a state name that is not a state: a typo is a rule the author
        believes is in force. Dropping it would silently widen the trigger —
        the one direction this file exists to prevent.
        """
        return cls(
            label=_text(settings.get("trigger_label")),
            assignee=_text(settings.get("trigger_assignee")),
            states=_states(settings.get("trigger_states")),
            recipe=_text(settings.get("trigger_recipe")),
        )


def _text(value: Any) -> str | None:
    """A setting as a trimmed string, with blank meaning unset."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _states(value: Any) -> tuple[IssueState, ...]:
    """``"todo, in_review"`` (or a list) → ``(IssueState.TODO, ...)``."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [str(part) for part in value]
    else:
        raise PmConfigError(
            f"trigger_states must be a comma-separated string or a list, not {type(value).__name__}"
        )
    wanted: list[IssueState] = []
    for part in parts:
        name = part.strip().lower()
        if not name:
            continue
        try:
            state = IssueState(name)
        except ValueError:
            known = ", ".join(sorted(str(state) for state in IssueState))
            raise PmConfigError(
                f"trigger_states names {part.strip()!r}, which is not a state ({known})"
            ) from None
        if state not in wanted:
            wanted.append(state)
    return tuple(wanted)


def matches_trigger(issue: Triggerable, trigger: Trigger) -> tuple[bool, str]:
    """``(matched, why)`` — and ``why`` is always populated, matched or not.

    The reason string is the product here as much as the boolean: a dry run
    over a real board has to be able to say *why* a ticket was passed over,
    or the only way to find out is to read this file.
    """
    if not trigger.configured:
        return False, NO_TRIGGER

    matched: list[str] = []
    if trigger.label is not None:
        if trigger.label not in tuple(issue.labels):
            return False, f"no {trigger.label!r} label"
        matched.append(f"label {trigger.label!r}")
    if trigger.assignee is not None:
        actual = issue.assignee or ""
        if actual != trigger.assignee:
            return False, f"assigned to {actual or 'nobody'}, not {trigger.assignee!r}"
        matched.append(f"assignee {trigger.assignee!r}")
    if trigger.states:
        if IssueState(issue.state) not in trigger.states:
            return False, f"state {str(issue.state)!r} is not a trigger state"
        matched.append(f"state {str(issue.state)!r}")
    return True, "matched by " + ", ".join(matched)
