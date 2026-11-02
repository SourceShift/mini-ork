"""The mandate: who let this ticket become work, under which rules.

An intake arrival used to leave a row with a payload hash, an action and a run
id — enough to say *that* something happened, not *why it was allowed*. Two
states that must never look alike were therefore indistinguishable: "no trigger
is configured, so nothing matched" and "the poller has not run yet" both showed
up as silence. This module builds the missing half.

A mandate is a small, JSON-shaped record:

``actor``
    The intake identity that admitted the ticket — the provider id
    (``github``), which is the machine principal the run descends from.
``source``
    The channel (``intake`` poll or ``intake-webhook``), because the same
    ticket can arrive both ways and the operator needs to know which one ran.
``scope_ref``
    The ticket uid. Scope is the thing a rule grants, so it is recorded next to
    the rules rather than inferred from a foreign key.
``rule_ids``
    The trigger rules that were in force, as ``key:value`` strings. For a
    refusal these are the rules the ticket was *tested against*, which is what
    makes "no label" different from "no rules".
``policy_version``
    A content hash over the source's trigger settings only — not the whole
    config — so an unrelated change (a different source's interval, a budget)
    does not perturb it while a rule edit does.
``strength``
    ``granted`` (dispatched), ``refused`` (trigger did not select it) or
    ``denied`` (a registered admission policy said no).
``decided_by`` / ``decision_id``
    The policy that decided and the ``policy_decisions`` row it wrote, so the
    ledger entry can be followed into the audit trail.
``at``
    When this arrival was decided.

The record is written to ``intake_events.mandate_json``. Nothing reads it to
*decide* anything — it is a record, and the admission decision itself is made
once, in :func:`mini_ork.intake.dispatch.dispatch_issue`.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from mini_ork.intake.trigger import Trigger

#: Prefix on :func:`policy_version` output, so a hash in a ledger row is
#: recognisable as a trigger-settings hash and not mistaken for a run id.
VERSION_PREFIX = "tv-"


def rule_ids(trigger: Trigger) -> list[str]:
    """The rules in force, as ``key:value`` — empty when none are configured.

    An empty list is a *statement*: this source has no rule that could match,
    which is why every ticket was refused with :data:`~.trigger.NO_TRIGGER`.
    """
    rules: list[str] = []
    if trigger.label is not None:
        rules.append(f"trigger_label:{trigger.label}")
    if trigger.assignee is not None:
        rules.append(f"trigger_assignee:{trigger.assignee}")
    if trigger.states:
        rules.append("trigger_states:" + ",".join(str(state) for state in trigger.states))
    if trigger.recipe is not None:
        rules.append(f"trigger_recipe:{trigger.recipe}")
    return rules


def policy_version(trigger: Trigger) -> str:
    """A stable hash over the trigger settings this ticket was judged by.

    ``recipe`` is included even though it selects nothing: it decides which
    recipe a granted ticket runs, so changing it changes the authority's
    effect. ``configured`` is a derived property and is left out — it is a
    function of the others and would only duplicate them.
    """
    payload = {
        "label": trigger.label,
        "assignee": trigger.assignee,
        "states": [str(state) for state in trigger.states],
        "recipe": trigger.recipe,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return VERSION_PREFIX + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def build(
    *,
    actor: str,
    source: str,
    scope_ref: str,
    trigger: Trigger | None,
    strength: str,
    detail: str = "",
    thread_id: str = "",
    decided_by: str = "",
    decision_id: str = "",
    at: int | None = None,
) -> dict[str, Any]:
    """One mandate record. ``strength`` is the caller's assertion, not a guess."""
    if strength not in {"granted", "refused", "denied"}:
        raise ValueError(f"mandate strength must be granted/refused/denied, got {strength!r}")
    rules = rule_ids(trigger) if trigger is not None else []
    return {
        "actor": actor,
        "source": source,
        "scope_ref": scope_ref,
        "thread_id": thread_id,
        "rule_ids": rules,
        # ``unconfigured`` is honest about a missing Trigger rather than
        # pretending the empty rule set was evaluated and matched.
        "policy_version": policy_version(trigger) if trigger is not None else "",
        "strength": strength,
        "detail": detail,
        "decided_by": decided_by,
        "decision_id": decision_id,
        "at": int(time.time()) if at is None else int(at),
    }
