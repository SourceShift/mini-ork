"""The mandate: who admitted a ticket, under which rules — and the gate that says no.

Two things are pinned here.

**Admission is a gate, not a formality.** ``dispatch_issue`` evaluates
``intake_admission`` policies *before* ``service.deliver``; a DENY (or a policy
that raises, which the engine treats as a DENY when it guards admission) ends
the call with a ``skipped`` dispatch, no thread, no run and no launch — and one
``policy_decisions`` row that says why.

**Every arrival carries a mandate.** A granted ticket records the trigger rules
it was selected by and the ruleset hash; a refused one records the rules it was
tested against, which is what makes "no label" distinguishable from "no rules".
The record is written to the ledger and read back by ``threads show``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.intake import mandate as mandate_mod  # noqa: E402
from mini_ork.intake.dispatch import dispatch_issue, record_refusal  # noqa: E402
from mini_ork.intake.trigger import Trigger  # noqa: E402
from mini_ork.pm.model import Issue, IssueState, Priority, ProviderIdentity  # noqa: E402
from mini_ork.policies import engine  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store  # noqa: E402

UID = "github:acme/app#12"
RULES = Trigger(label="agent", recipe="code-fix")


class _Source:
    """The read-only slice of a provider ``dispatch_issue`` uses."""

    id = "github"
    label = "GitHub Issues"

    def __init__(self, *issues: Issue) -> None:
        self.issues = {issue.uid: issue for issue in issues}

    def get(self, uid: str) -> Issue:
        if uid not in self.issues:
            raise KeyError(uid)
        return self.issues[uid]

    def comments(self, uid: str):
        return ()


def _issue(*, updated_at: str = "2026-10-11T09:00:00Z", labels: tuple[str, ...] = ("agent",)) -> Issue:
    return Issue(
        identity=ProviderIdentity(
            provider="github",
            key="acme/app#12",
            project="acme/app",
            url="https://github.com/acme/app/issues/12",
        ),
        title="Retry loop drops 429s",
        state=IssueState.TODO,
        native_state="open",
        description="Retries drop 429s on the second attempt.",
        priority=Priority.NONE,
        labels=labels,
        assignee="amir",
        updated_at=updated_at,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    engine.clear_registry()
    yield
    engine.clear_registry()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture
def db(home: Path) -> str:
    return str(home / "state.db")


@pytest.fixture
def launches(monkeypatch) -> list[dict]:
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(home, recipe, kickoff, run_id=None):
        calls.append({"recipe": recipe, "kickoff": kickoff, "run_id": run_id})
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
            "log_path": str(Path(home) / "logs" / f"{run_id}.log"),
        }

    monkeypatch.setattr(control, "launch_run", fake)
    return calls


@pytest.fixture
def steers(monkeypatch) -> list[dict]:
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(db, task_run_id, message, **kwargs):
        calls.append({"run_id": task_run_id, "message": message, **kwargs})
        return {"ok": True, "steering_id": 7}

    monkeypatch.setattr(control, "steer_run", fake)
    return calls


def _deny(event, config=None):
    return {"result": "DENY", "reason": "not now", "policy_name": "deny_all"}


def _decisions(db: str) -> list[tuple]:
    import sqlite3

    con = sqlite3.connect(db)
    try:
        return list(con.execute(
            "SELECT event_type, policy_name, result FROM policy_decisions"
        ))
    finally:
        con.close()


# ── the gate ───────────────────────────────────────────────────────────────


def test_a_deny_never_reaches_deliver(home, db, launches):
    engine.register_policy("deny_all", _deny)
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), trigger=RULES, db=db)

    assert outcome.action == "refused"
    assert outcome.started is False
    assert outcome.run_id is None
    assert launches == [], "a refused ticket must not start a run"
    assert store.find_thread(store.ThreadOrigin.INTAKE, UID, db=db) is None

    rows = _decisions(db)
    assert len(rows) == 1
    assert rows[0][0] == "intake_admission"
    assert rows[0][1:] == ("deny_all", "DENY")


def test_a_denial_is_recorded_as_a_mandate(home, db, launches):
    engine.register_policy("deny_all", _deny)
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), trigger=RULES, db=db)

    event = store.intake_event("github", outcome.delivery_id, db=db)
    assert event is not None
    assert event.action == "refused"
    assert event.thread_id is None
    assert event.mandate is not None
    assert event.mandate["strength"] == "denied"
    assert event.mandate["decided_by"] == "deny_all"
    # The rules the ticket was judged by are part of the refusal: "no label
    # matched" and "no rule configured" are different facts.
    assert event.mandate["rule_ids"] == ["trigger_label:agent", "trigger_recipe:code-fix"]
    assert event.mandate["policy_version"].startswith(mandate_mod.VERSION_PREFIX)
    assert event.mandate["detail"] == "deny by policy deny_all: not now"


def test_a_raising_policy_fails_closed(home, db, launches):
    """An admission rule that cannot answer must not admit. The default
    LOG_ONLY on error exists for advisory policies; here it would start work
    nobody authorized."""
    def boom(event, config=None):
        raise RuntimeError("config unreadable")

    engine.register_policy("broken", boom)
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)

    assert outcome.action == "refused"
    assert launches == []
    assert "config unreadable" in (outcome.detail or "")
    assert _decisions(db) == [("intake_admission", "broken", "DENY")]


def test_require_approval_does_not_start_work(home, db, launches):
    """There is no approval queue at intake, so "wait for a person" and "no"
    are the same outcome: the ticket does not run."""
    engine.register_policy(
        "hold",
        lambda event, config=None: {
            "result": "REQUIRE_APPROVAL", "reason": "needs sign-off", "policy_name": "hold",
        },
    )
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)
    assert outcome.action == "refused"
    assert launches == []


def test_an_allow_flows_through_and_the_mandate_is_granted(home, db, launches):
    engine.register_policy(
        "allow_agent",
        lambda event, config=None: (
            {"result": "ALLOW", "reason": "label ok", "policy_name": "allow_agent"}
            if "trigger_label:agent" in event["data"]["rule_ids"] else None
        ),
    )
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), trigger=RULES, db=db)

    assert outcome.action == "launched"
    event = store.intake_event("github", outcome.delivery_id, db=db)
    assert event.mandate["strength"] == "granted"
    assert event.mandate["decided_by"] == "allow_agent"
    assert event.mandate["thread_id"] == outcome.thread_id
    assert event.mandate["scope_ref"] == UID
    assert event.mandate["source"] == "intake"
    assert _decisions(db) == [("intake_admission", "allow_agent", "ALLOW")]


def test_no_registered_policy_is_an_allowed_arrival(home, db, launches):
    """The simple tier has no policies and must not need any."""
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)
    assert outcome.action == "launched"
    event = store.intake_event("github", outcome.delivery_id, db=db)
    assert event.mandate["strength"] == "granted"
    assert event.mandate["decided_by"] == "<default>"


def test_the_mandate_without_a_trigger_says_so(home, db, launches):
    """A hand-dispatched ticket has no rules; an empty ruleset and an
    evaluated-but-empty one must not look alike."""
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)
    mandate = store.intake_event("github", outcome.delivery_id, db=db).mandate
    assert mandate["rule_ids"] == []
    assert mandate["policy_version"] == ""


# ── the rules are re-read on the ticket the source serves now ──────────────


def test_a_ticket_that_lost_its_label_is_refused_not_continued(home, db, launches):
    """The resumed-run hole: `deliver` decides from liveness alone, so without
    a re-check a label removed yesterday would keep the thread alive today."""
    first = dispatch_issue(home, _Source(_issue()), _issue(), trigger=RULES, db=db)
    assert first.action == "launched"
    store.settle_run(first.run_id, store.RunStatus.FINISHED, db=db)

    # The label was taken off between the two arrivals.
    relabelled = _issue(updated_at="2026-10-12T09:00:00Z", labels=())
    outcome = dispatch_issue(home, _Source(relabelled), relabelled, trigger=RULES, db=db)

    assert outcome.action == "refused"
    assert "no longer permitted" in (outcome.detail or "")
    assert launches == [launches[0]], "a refused resume must not start attempt 2"
    assert [run.attempt for run in store.runs_for(first.thread_id, db=db)] == [1]

    event = store.intake_event("github", outcome.delivery_id, db=db)
    assert event.action == "refused"
    assert event.mandate["strength"] == "refused"
    assert event.mandate["rule_ids"] == ["trigger_label:agent", "trigger_recipe:code-fix"]


def test_a_ticket_that_lost_its_label_is_not_steered(home, db, launches, steers):
    """A live run is not a licence to keep feeding it work either."""
    started = dispatch_issue(home, _Source(_issue()), _issue(), trigger=RULES, db=db)
    relabelled = _issue(updated_at="2026-10-12T09:00:00Z", labels=())
    outcome = dispatch_issue(home, _Source(relabelled), relabelled, trigger=RULES, db=db)

    assert started.action == "launched"
    assert outcome.action == "refused"
    assert steers == []


def test_a_refusal_on_revalidation_is_promoted_once_the_label_returns(home, db, launches):
    """Refused rows are not final: the same delivery arriving after the ticket
    is eligible again dispatches, and the ledger says so."""
    relabelled = _issue(labels=())
    first = dispatch_issue(home, _Source(relabelled), relabelled, trigger=RULES, db=db)
    assert first.action == "refused"

    again = dispatch_issue(
        home, _Source(_issue()), _issue(), trigger=RULES, delivery_id=first.delivery_id, db=db
    )
    assert again.action == "launched"
    assert store.intake_event("github", first.delivery_id, db=db).action == "launched"


# ── refusals, and their promotion ──────────────────────────────────────────


def test_a_refusal_records_the_rules_it_was_tested_against(home, db):
    record_refusal(
        provider_id="github",
        issue_uid=UID,
        delivery_id="d-1",
        trigger=Trigger(label="agent"),
        detail="labels do not match trigger_label",
        updated_at="2026-10-11T09:00:00Z",
        db=db,
    )
    event = store.intake_event("github", "d-1", db=db)
    assert event.action == "refused"
    assert event.mandate["strength"] == "refused"
    assert event.mandate["rule_ids"] == ["trigger_label:agent"]


def test_a_refusal_is_promoted_when_the_same_delivery_later_dispatches(home, db, launches):
    """A label can be added without an ``updated_at`` bump, so the same delivery
    id can arrive again after it became eligible. The refusal must not become a
    permanent duplicate."""
    record_refusal(
        provider_id="github",
        issue_uid=UID,
        delivery_id="d-1",
        trigger=RULES,
        detail="labels do not match",
        db=db,
    )
    outcome = dispatch_issue(
        home, _Source(_issue()), _issue(), trigger=RULES, delivery_id="d-1", db=db
    )
    assert outcome.action == "launched"

    event = store.intake_event("github", "d-1", db=db)
    assert event.action == "launched"
    assert event.thread_id == outcome.thread_id
    assert event.mandate["strength"] == "granted"


def test_a_dispatch_is_final_and_a_replay_folds_into_it(home, db, launches):
    first = dispatch_issue(home, _Source(_issue()), _issue(), trigger=RULES, db=db)
    again = dispatch_issue(home, _Source(_issue()), _issue(), trigger=RULES, db=db)
    assert again.delivery_id == first.delivery_id
    # The row still names the launch; a second identical arrival does not
    # rewrite it into anything else.
    event = store.intake_event("github", first.delivery_id, db=db)
    assert event.action == first.action


# ── the record itself ──────────────────────────────────────────────────────


def test_the_policy_version_tracks_the_rules_and_nothing_else():
    base = mandate_mod.policy_version(Trigger(label="agent", recipe="code-fix"))
    assert base == mandate_mod.policy_version(Trigger(label="agent", recipe="code-fix"))
    assert base.startswith(mandate_mod.VERSION_PREFIX)
    assert base != mandate_mod.policy_version(Trigger(label="bug", recipe="code-fix"))
    assert base != mandate_mod.policy_version(Trigger(label="agent", recipe="other"))


def test_a_mandate_needs_a_known_strength():
    with pytest.raises(ValueError):
        mandate_mod.build(
            actor="github", source="intake", scope_ref=UID, trigger=None, strength="maybe",
        )


def test_intake_events_for_thread_reads_the_history_in_order(home, db, launches):
    first = dispatch_issue(home, _Source(_issue()), _issue(), db=db)
    store.settle_run(first.run_id, store.RunStatus.FINISHED, db=db)
    dispatch_issue(home, _Source(_issue()), _issue(updated_at="2026-10-11T10:00:00Z"), db=db)

    events = store.intake_events_for_thread(first.thread_id, db=db)
    assert [e.action for e in events] == ["launched", "continued"]
    assert all(e.mandate["thread_id"] == first.thread_id for e in events)


def test_a_corrupt_mandate_reads_as_no_mandate(home, db):
    """A half-written row must not take the whole history down with it."""
    import sqlite3

    store.record_intake_event("github", "d-1", "h", issue_uid=UID, action="reported", db=db)
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE intake_events SET mandate_json = ? WHERE delivery_id = 'd-1'",
                    ("{not json",))
        con.commit()
    finally:
        con.close()
    assert store.intake_event("github", "d-1", db=db).mandate is None


def test_the_mandate_round_trips_through_json(home, db):
    mandate = mandate_mod.build(
        actor="github", source="intake-webhook", scope_ref=UID, trigger=RULES,
        strength="granted", thread_id="t1", decided_by="p", decision_id="pd-1",
    )
    store.record_intake_event("github", "d-1", "h", mandate=mandate, db=db)
    stored = store.intake_event("github", "d-1", db=db).mandate
    assert stored == json.loads(json.dumps(mandate))
