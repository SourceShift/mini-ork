"""Tests for ``mini_ork.intake.poll`` and ``mini-ork intake``.

The pass and its CLI share a file because they share the thing under test: the
CLI is a presentation layer over one pass, and the stub source that drives the
pass is the stub that answers the CLI. Splitting them would mean duplicating the
stub and then keeping two copies honest.

``launch_run``/``steer_run`` are monkeypatched; the source is a stub; the sync
is the real one against that stub, so the mirror, the trigger filter and the
dispatch decision are all exercised for real.

What is pinned:

* an unconfigured source dispatches **nothing**, however many tickets match
* a dry run decides without starting
* ``max_runs`` bounds a pass, and a ticket already in the ledger does not spend
  the budget
* the sync asks for the whole board, not just the open column — otherwise a
  ticket closed between two polls keeps a stale ``todo`` row and gets started
* a failed sync raises out of one pass and is survived by the loop
* ``status`` explains a source that is not doing anything, in both the
  "no trigger" and the "no adapter" directions
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli import intake_cmd  # noqa: E402
from mini_ork.intake import poll as intake_poll  # noqa: E402
from mini_ork.intake.trigger import NO_TRIGGER, Trigger  # noqa: E402
from mini_ork.pm import registry  # noqa: E402
from mini_ork.pm.config import PmConfig, SourceConfig  # noqa: E402
from mini_ork.pm.model import (  # noqa: E402
    OPEN_STATES,
    Issue,
    IssueState,
    ProviderIdentity,
)
from mini_ork.pm.provider import Capabilities, Page, ProviderError  # noqa: E402
from mini_ork.pm.registry import UnknownProvider  # noqa: E402
from mini_ork.pm import store as pm_store  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store  # noqa: E402

AT = "2026-10-11T09:00:00Z"


class _Source:
    """A source that answers from a list: the three reads intake uses."""

    id = "acme"
    label = "Acme Issues"
    capabilities = Capabilities(labels=True, assignees=True)

    def __init__(self, *issues: Issue, fail_once: Exception | None = None) -> None:
        self.issues = {issue.uid: issue for issue in issues}
        self.queries = []
        self.get_calls: list[str] = []
        # Uids the source still lists but can no longer read — a ticket deleted
        # between the sync and the dispatch. See `_issue`.
        self.unservable: set[str] = set()
        self._fail_once = fail_once

    def list_issues(self, query):
        self.queries.append(query)
        if self._fail_once is not None:
            failure, self._fail_once = self._fail_once, None
            raise failure
        return Page(issues=tuple(self.issues.values()), has_more=False)

    def get(self, uid: str) -> Issue:
        self.get_calls.append(uid)
        if uid in self.unservable:
            raise KeyError(uid)
        return self.issues[uid]

    def comments(self, uid: str):
        return ()


def _issue(
    number: int,
    *,
    labels: tuple[str, ...] = ("agent",),
    state: IssueState = IssueState.TODO,
    updated_at: str = AT,
) -> Issue:
    return Issue(
        identity=ProviderIdentity(provider="acme", key=f"ENG-{number}", project="ENG"),
        title=f"Ticket {number}",
        state=state,
        native_state=str(state),
        description=f"Body of ticket {number}.",
        labels=labels,
        assignee="amir",
        updated_at=updated_at,
    )


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
        calls.append({"recipe": recipe, "run_id": run_id, "kickoff": kickoff})
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
    monkeypatch.setattr(
        control, "steer_run", lambda db, task_run_id, message, **kw: calls.append(
            {"run_id": task_run_id, "message": message}
        ) or {"ok": True, "steering_id": 7}
    )
    return calls


@pytest.fixture
def configured(monkeypatch):
    """Point the trigger lookup at a config the test owns."""

    def apply(sources: dict[str, SourceConfig]) -> PmConfig:
        configuration = PmConfig(sources=sources, path=None)
        monkeypatch.setattr(intake_poll.pm_config, "load_config", lambda: configuration)
        return configuration

    return apply


@pytest.fixture
def registered():
    """Register a stub source under a real id, and take it out afterwards."""
    added: list[str] = []

    def add(provider):
        registry.register_provider(provider, replace=True)
        added.append(provider.id)
        return provider

    yield add
    for source_id in added:
        with contextlib.suppress(KeyError):
            registry.unregister_provider(source_id)


# ── the pass ──────────────────────────────────────────────────────────────


def test_an_unconfigured_source_dispatches_nothing(home, db, launches):
    """The safety property, end to end: three tickets match nothing because no
    rule is set, and the pass starts nothing."""
    source = _Source(_issue(1), _issue(2), _issue(3))
    report = intake_poll.poll_once(home, source.id, db=db, provider=source)

    assert report.started == 0
    assert report.considered == 3
    assert report.mirrored == 3
    assert {outcome.reason for outcome in report.outcomes} == {NO_TRIGGER}
    assert launches == []
    # And nothing was even read in full: the filter is the mirror's job.
    assert source.get_calls == []


def test_a_configured_source_starts_the_matching_ticket(home, db, launches):
    source = _Source(_issue(1, labels=("agent",)), _issue(2, labels=("bug",)))
    report = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=Trigger(label="agent")
    )

    assert report.started == 1
    assert [outcome.action for outcome in report.outcomes] == ["launched", "skipped"]
    assert len(launches) == 1
    assert source.get_calls == ["acme:ENG-1"]


def test_a_dry_run_decides_without_starting(home, db, launches):
    source = _Source(_issue(1))
    report = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=Trigger(label="agent"), dry_run=True
    )

    assert report.dry_run is True
    assert [outcome.action for outcome in report.outcomes] == ["would-start"]
    assert report.started == 0
    assert launches == []
    assert source.get_calls == []
    # Nothing was claimed either, so the real pass still has work to do.
    assert store.intake_event("acme", f"acme:ENG-1@{AT}", db=db) is None


def test_max_runs_bounds_a_pass(home, db, launches):
    source = _Source(_issue(1), _issue(2), _issue(3))
    report = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=Trigger(label="agent"), max_runs=1
    )

    assert report.started == 1
    assert report.deferred == 2
    assert len(launches) == 1


def test_a_ticket_already_handled_does_not_spend_the_budget(home, db, launches):
    """``max_runs`` bounds *dispatches*. A folded arrival was never going to
    act, so counting it would let one stale row block the pass."""
    source = _Source(_issue(1), _issue(2))
    rule = Trigger(label="agent")
    intake_poll.poll_once(home, source.id, db=db, provider=source, trigger=rule, max_runs=1)
    second = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=rule, max_runs=1
    )

    # ENG-1 folds (already launched); ENG-2 was over the budget the first time
    # and is dispatched now.
    assert [outcome.action for outcome in second.outcomes] == ["duplicate", "launched"]
    assert second.started == 1
    assert len(launches) == 2


def test_a_second_pass_over_the_same_tickets_starts_nothing(home, db, launches):
    source = _Source(_issue(1), _issue(2))
    rule = Trigger(label="agent")
    first = intake_poll.poll_once(home, source.id, db=db, provider=source, trigger=rule, max_runs=9)
    second = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=rule, max_runs=9
    )

    assert first.started == 2
    assert second.started == 0
    assert {outcome.action for outcome in second.outcomes} == {"duplicate"}
    assert len(launches) == 2


def test_an_updated_ticket_is_a_new_arrival(home, db, launches):
    """A label change bumps ``updated_at``; the ledger keys on the arrival, so
    the follow-up reaches the thread instead of being folded away."""
    source = _Source(_issue(1))
    rule = Trigger(label="agent")
    first = intake_poll.poll_once(home, source.id, db=db, provider=source, trigger=rule)
    source.issues["acme:ENG-1"] = _issue(1, updated_at="2026-10-11T11:00:00Z")
    second = intake_poll.poll_once(home, source.id, db=db, provider=source, trigger=rule)

    assert first.started == 1
    assert [outcome.action for outcome in second.outcomes] == ["steered"]
    assert len(launches) == 1


def test_a_closed_ticket_is_not_a_candidate(home, db, launches):
    source = _Source(_issue(1, state=IssueState.DONE), _issue(2))
    report = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=Trigger(label="agent")
    )
    assert report.considered == 1
    assert [outcome.issue_uid for outcome in report.outcomes] == ["acme:ENG-2"]


def test_the_sync_asks_for_finished_work_but_not_for_canceled(home, db):
    """An open-only sync leaves a ticket closed between two polls sitting in the
    mirror as ``todo`` — and the trigger would start work on it. So ``done`` is
    asked for too.

    ``canceled`` is not, and this pins that: intake never starts canceled work,
    and ``local`` spells that state as the ``archived_at`` flag, so asking for
    it beside a status filter composes into a clause that matches nothing else.
    """
    source = _Source(_issue(1))
    intake_poll.poll_once(home, source.id, db=db, provider=source)
    asked = set(source.queries[0].states)
    assert asked >= OPEN_STATES | {IssueState.DONE}
    assert IssueState.CANCELED not in asked


def test_a_ticket_closed_since_the_sync_is_not_dispatched(home, db, launches):
    """The mirror is a cache and the source is the authority. A row synced while
    the ticket was open, read after it closed, must not start a run — the one
    read the dispatch already makes is where that is caught."""
    source = _Source(_issue(1))  # the sync sees it open, so the mirror says open
    # ... and by the time the ticket is read in full, it has been closed.
    source.get = lambda uid: _issue(1, state=IssueState.DONE)
    report = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=Trigger(label="agent")
    )
    assert report.started == 0
    assert [outcome.action for outcome in report.outcomes] == ["skipped"]
    assert "now reports it as done" in report.outcomes[0].reason
    assert launches == []


def test_a_failed_sync_raises_and_is_recorded(home, db):
    source = _Source(_issue(1), fail_once=ProviderError("acme: 401 Bad credentials"))
    with pytest.raises(ProviderError):
        intake_poll.poll_once(home, source.id, db=db, provider=source)
    state = pm_store.mirror_state("acme", db=db)
    assert state and "401" in (state[0]["error"] or "")


def test_a_rejected_row_is_reported_not_fatal(home, db, launches):
    """One ticket the source no longer serves must not stop the others."""
    source = _Source(_issue(1), _issue(2))
    source.unservable.add("acme:ENG-1")
    report = intake_poll.poll_once(
        home, source.id, db=db, provider=source, trigger=Trigger(label="agent")
    )
    assert [outcome.action for outcome in report.outcomes] == ["failed", "launched"]
    assert report.started == 1


def test_an_unknown_source_is_the_registry_error(tmp_path):
    db = str(tmp_path / "state.db")
    with pytest.raises(UnknownProvider):
        intake_poll.poll_once(tmp_path, "nope", db=db)


# ── the loop ──────────────────────────────────────────────────────────────


def test_the_loop_survives_a_failed_pass(home, db):
    source = _Source(_issue(1), fail_once=ProviderError("acme: connection refused"))
    slept: list[float] = []
    reports = intake_poll.poll_loop(
        home, source.id, interval=30, db=db, provider=source,
        trigger=Trigger(label="agent"), passes=2, sleep=slept.append,
    )
    assert "connection refused" in (reports[0].error or "")
    assert reports[1].error is None
    assert reports[1].started == 1
    assert slept == [30]


def test_the_loop_stops_when_on_pass_says_so(home, db, launches):
    source = _Source(_issue(1))
    reports = intake_poll.poll_loop(
        home, source.id, interval=0, db=db, provider=source,
        trigger=Trigger(label="agent"), sleep=lambda seconds: None,
        on_pass=lambda report: False,
    )
    assert len(reports) == 1


# ── status ────────────────────────────────────────────────────────────────


def test_status_reads_the_trigger_out_of_the_config(db, configured):
    configured(
        {
            "acme": SourceConfig(
                id="acme", enabled=True, settings={"trigger_label": "agent", "trigger_recipe": "book"}
            ),
            "ghost": SourceConfig(id="ghost", enabled=True),
        }
    )
    statuses = {status.source: status for status in intake_poll.source_statuses(db)}

    acme = statuses["acme"]
    assert acme.enabled is True
    assert acme.registered is False  # no adapter was built for it in this process
    assert acme.trigger.label == "agent"
    assert acme.trigger.recipe == "book"
    # Declared, enabled, and nothing to build it — which `status` says by
    # showing it unregistered with no skip reason.
    assert statuses["ghost"].registered is False
    assert statuses["ghost"].skipped_reason is None


def test_status_reports_a_source_that_is_not_configured(db):
    statuses = {status.source: status for status in intake_poll.source_statuses(db)}
    assert statuses["local"].registered is True
    assert statuses["local"].trigger.configured is False


# ── the CLI ───────────────────────────────────────────────────────────────


def _triggered(label: str = "agent") -> PmConfig:
    return PmConfig(
        sources={"acme": SourceConfig(id="acme", enabled=True, settings={"trigger_label": label})},
        path=None,
    )


def test_the_cli_status_verb_explains_an_idle_setup(home, capsys):
    rc = intake_cmd.main(["status", "--home", str(home)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "local" in out
    assert NO_TRIGGER in out
    assert "nothing will start" in out


def test_the_cli_status_verb_has_a_json_form(home, capsys, monkeypatch, tmp_path):
    import json

    # Pin the config: without this the status read falls through to the repo's
    # shipped config/pm.yaml, whose disabled linear/jira examples are listed too.
    # The shape of the JSON is under test here, not which sources the repo ships.
    pm_yaml = tmp_path / "pm.yaml"
    pm_yaml.write_text("sources:\n  local:\n    enabled: true\n")
    monkeypatch.setenv("MINI_ORK_PM", str(pm_yaml))

    rc = intake_cmd.main(["status", "--home", str(home), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["source"] for entry in payload] == ["local"]


def test_the_cli_once_runs_a_pass_over_every_configured_source(
    home, db, launches, configured, registered, capsys
):
    """The plan's end-to-end, minus the token: `intake once` with no --source."""
    registered(_Source(_issue(1), _issue(2, labels=("bug",))))
    configured(_triggered().sources)

    rc = intake_cmd.main(["once", "--home", str(home), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would-start" in out
    assert "acme:ENG-1" in out

    rc = intake_cmd.main(["once", "--home", str(home)])
    assert rc == 0
    assert len(launches) == 1
    assert launches[0]["run_id"] == "acme-eng-1"


def test_the_cli_reports_a_sync_failure_and_exits_nonzero(
    home, configured, registered, capsys
):
    registered(_Source(_issue(1), fail_once=ProviderError("acme: 401 Bad credentials")))
    configured(_triggered().sources)
    rc = intake_cmd.main(["once", "--home", str(home)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "401" in captured.err


def test_the_cli_says_so_when_there_is_nothing_to_poll(home, capsys):
    """The shipped checkout has no configured source: `once` must explain that
    rather than exiting 0 having done nothing."""
    rc = intake_cmd.main(["once", "--home", str(home)])
    assert rc == 1
    assert "no source to poll" in capsys.readouterr().err


def test_the_cli_poll_bounds_itself_with_passes(home, configured, registered, capsys):
    registered(_Source(_issue(1)))
    configured(_triggered().sources)
    rc = intake_cmd.main(["poll", "--home", str(home), "--passes", "2", "--interval", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("intake acme:") == 2


def test_the_cli_refuses_an_unknown_verb(home, capsys):
    assert intake_cmd.main(["nonsense", "--home", str(home)]) == 2
    assert "unknown verb" in capsys.readouterr().err
