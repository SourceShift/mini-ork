"""Tests for ``mini_ork.intake.dispatch`` — the ticket → thread → run path.

``launch_run`` and ``steer_run`` are monkeypatched (the house pattern from
``tests/unit/test_threads_service.py``), and the source is a stub that answers
from a list, so nothing here spawns a process, opens a socket or needs a
credential. The state.db is a real migrated one because the continuation path
reads the thread tables and the PM mirror.

What is pinned is the plan's ticket → thread → run table, branch by branch:

* no run → the deterministic thread id, attempt 1, and **one** ``pm_links``
  ``kind='run'`` edge plus one ``intake_events`` row;
* a live run → steered, and the thread still has exactly one run;
* a finished run → *continued*, attempt 2, the same thread, a different run id —
  and the ticket re-stated in full, which is what makes a continuation
  self-contained rather than dependent on a transcript it cannot see;
* the same delivery twice → one row in the ledger, no second turn, no second run.

The failure modes are tested where they belong (``test_pm_*.py`` for the
adapters, ``test_intake_poll.py`` for the pass); this file is about the decision
and its evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.intake.dispatch import (  # noqa: E402
    MAX_BODY_CHARS,
    dispatch_issue,
    thread_id_for,
    ticket_markdown,
)
from mini_ork.pm import store as pm_store  # noqa: E402
from mini_ork.pm.model import (  # noqa: E402
    Comment,
    Issue,
    IssueState,
    LinkKind,
    Priority,
    ProviderIdentity,
)
from mini_ork.pm.provider import Unsupported  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store  # noqa: E402
from mini_ork.threads.model import RunStatus, ThreadOrigin  # noqa: E402

UID = "github:acme/app#12"


class _Source:
    """A source that answers from a list. Reads only — intake needs nothing else."""

    id = "github"
    label = "GitHub Issues"

    def __init__(self, *issues: Issue, comments: tuple[Comment, ...] = ()) -> None:
        self.issues = {issue.uid: issue for issue in issues}
        self._comments = comments
        self.comment_calls: list[str] = []

    def get(self, uid: str) -> Issue:
        if uid not in self.issues:
            raise KeyError(uid)
        return self.issues[uid]

    def comments(self, uid: str):
        self.comment_calls.append(uid)
        return self._comments


class _SilentSource(_Source):
    """A source that cannot list comments at all — ``local`` does exactly this."""

    def comments(self, uid: str):
        raise Unsupported(self.id, "comments")


def _issue(
    *,
    state: IssueState = IssueState.TODO,
    updated_at: str = "2026-10-11T09:00:00Z",
    description: str = "Retries drop 429s on the second attempt.",
    labels: tuple[str, ...] = ("agent",),
    assignee: str | None = "amir",
    priority: Priority = Priority.NONE,
) -> Issue:
    return Issue(
        identity=ProviderIdentity(
            provider="github",
            key="acme/app#12",
            project="acme/app",
            url="https://github.com/acme/app/issues/12",
        ),
        title="Retry loop drops 429s",
        state=state,
        native_state="open",
        description=description,
        priority=priority,
        labels=labels,
        assignee=assignee,
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


# ── the ticket as the agent reads it ───────────────────────────────────────


def test_the_thread_id_is_derived_from_the_issue_uid():
    assert thread_id_for(UID) == "github-acme-app-12"
    # Deterministic: the same ticket lands on the same thread even if the
    # threads table was wiped, which is the only sense in which intake is
    # idempotent against a lost database.
    assert thread_id_for(UID) == thread_id_for(UID)
    assert thread_id_for("linear:ENG-123") == "linear-eng-123"


def test_ticket_markdown_carries_the_provenance_the_body_and_the_comments():
    comment = Comment(
        issue_uid=UID, author="bob", body="Happens behind the proxy.", created_at="2026-10-11"
    )
    text = ticket_markdown(_issue(), [comment])
    assert text.startswith(f"**{UID}** — Retry loop drops 429s")
    assert "State: todo (open)" in text
    assert "Priority: none" in text
    assert "Assignee: amir" in text
    assert "Labels: agent" in text
    assert "https://github.com/acme/app/issues/12" in text
    assert "Retries drop 429s on the second attempt." in text
    assert "**bob** (2026-10-11): Happens behind the proxy." in text


def test_a_long_body_is_truncated_and_says_so():
    """A pasted log file must not become the whole kickoff — and must not
    vanish silently either."""
    text = ticket_markdown(_issue(description="x" * (MAX_BODY_CHARS + 500)))
    assert "_(description truncated: 500 characters not shown)_" in text
    assert len(text) < MAX_BODY_CHARS + 500


def test_an_empty_body_says_so_rather_than_rendering_nothing():
    assert "_(no description)_" in ticket_markdown(_issue(description=""))


def test_comments_that_could_not_be_read_are_reported_in_the_ticket():
    """Best-effort is not the same as silent: the ticket carries the reason."""
    text = ticket_markdown(_issue(), comments_error="github: comments are unsupported")
    assert "_(comments unavailable: github: comments are unsupported)_" in text


# ── the three branches ─────────────────────────────────────────────────────


def test_the_first_arrival_launches_attempt_one(home, db, launches):
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)

    assert outcome.action == "launched"
    assert outcome.started is True
    assert outcome.thread_id == "github-acme-app-12"
    # Attempt 1 uses the thread id as the run id — the identity the whole plan
    # rests on, and which create_conversation already relied on.
    assert outcome.run_id == outcome.thread_id
    assert len(launches) == 1

    thread = store.get_thread(outcome.thread_id, db=db)
    assert thread is not None
    assert thread.origin is ThreadOrigin.INTAKE
    assert thread.origin_ref == UID
    assert thread.title == "Retry loop drops 429s"

    runs = store.runs_for(outcome.thread_id, db=db)
    assert [run.attempt for run in runs] == [1]
    # `launch_run`'s third argument is the composed kickoff *text*, and the run
    # row keeps the path the launcher wrote it to.
    assert "Retry loop drops 429s" in launches[0]["kickoff"]
    assert UID in launches[0]["kickoff"]
    assert runs[0].kickoff_path == Path(home, "runs-inbox", f"{outcome.run_id}.md").as_posix()


def test_the_issue_is_linked_to_the_run_it_started(home, db, launches):
    """``kind='run'`` in ``pm_links`` is how "has a run ever worked this?" is
    answered — the link and the run are written by one call."""
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)
    links = pm_store.links_for(UID, db=db)
    assert [(link.kind, link.to_uid) for link in links] == [
        (LinkKind.RUN, f"run:{outcome.run_id}")
    ]


def test_the_arrival_is_recorded_in_the_ledger(home, db, launches):
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)
    event = store.intake_event("github", outcome.delivery_id, db=db)
    assert event is not None
    assert event.issue_uid == UID
    assert event.action == "launched"
    assert event.thread_id == outcome.thread_id
    assert len(event.payload_hash) == 64


def test_a_live_run_is_steered_and_never_doubled(home, db, launches, steers):
    """The whole point of the live branch: a second arrival while the agent is
    working is a message to that agent, not a second agent."""
    source = _Source(_issue())
    first = dispatch_issue(home, source, _issue(), db=db)
    # A different arrival — the ticket was updated — so the ledger does not
    # fold it and the decision really is made afresh.
    second = dispatch_issue(home, source, _issue(updated_at="2026-10-11T10:00:00Z"), db=db)

    assert first.action == "launched"
    assert second.action == "steered"
    assert second.thread_id == first.thread_id
    assert len(launches) == 1
    assert len(store.runs_for(first.thread_id, db=db)) == 1
    assert steers and steers[0]["run_id"] == first.run_id


def test_a_finished_run_continues_the_same_thread(home, db, launches):
    source = _Source(_issue())
    first = dispatch_issue(home, source, _issue(), db=db)
    store.settle_run(first.run_id, RunStatus.FINISHED, db=db)

    second = dispatch_issue(home, source, _issue(updated_at="2026-10-11T10:00:00Z"), db=db)
    assert second.action == "continued"
    assert second.thread_id == first.thread_id
    assert second.run_id != first.run_id
    assert [run.attempt for run in store.runs_for(first.thread_id, db=db)] == [1, 2]


def test_a_continuation_restates_the_whole_ticket(home, db, launches):
    """A continuation must not depend on a transcript it cannot see: the ticket
    *is* the message, so it is carried again in full, under ``## Your task``."""
    source = _Source(_issue())
    first = dispatch_issue(home, source, _issue(), db=db)
    store.settle_run(first.run_id, RunStatus.FINISHED, db=db)
    second = dispatch_issue(home, source, _issue(updated_at="2026-10-11T10:00:00Z"), db=db)

    assert len(launches) == 2
    kickoff = launches[-1]["kickoff"]
    assert UID in kickoff
    assert "Retries drop 429s on the second attempt." in kickoff
    # And it knows what came before: the composer carries the prior attempt by
    # name — which is the run id the *previous* delivery minted, since the new
    # one does not exist until the launch it describes has happened.
    assert "## Prior run" in kickoff
    assert first.run_id in kickoff
    assert second.run_id not in kickoff


def test_the_configured_recipe_reaches_the_launch(home, db, launches):
    dispatch_issue(home, _Source(_issue()), _issue(), recipe="book-campaign", db=db)
    assert launches[0]["recipe"] == "book-campaign"


def test_the_same_delivery_twice_is_one_ledger_row(home, db, launches):
    source = _Source(_issue())
    first = dispatch_issue(home, source, _issue(), db=db)
    again = dispatch_issue(home, source, _issue(), db=db)
    assert again.delivery_id == first.delivery_id
    events = store.intake_events_for(UID, db=db)
    assert len(events) == 1
    assert events[0].action == "launched"


# ── the live-run join ──────────────────────────────────────────────────────


def test_a_live_run_for_an_issue_is_found_through_the_link(home, db, launches):
    outcome = dispatch_issue(home, _Source(_issue()), _issue(), db=db)
    live = store.live_run_for_issue(UID, db=db)
    assert live is not None
    assert live.run_id == outcome.run_id

    store.settle_run(outcome.run_id, RunStatus.FINISHED, db=db)
    assert store.live_run_for_issue(UID, db=db) is None


def test_a_home_that_never_synced_has_no_live_run(tmp_path):
    """``pm_links`` belongs to the PM mirror's own migration. A home that has
    never synced has no such table, and None — not an exception — is the honest
    answer to "is a run working this?"."""
    db = str(tmp_path / "bare.db")
    store.ensure_schema(db)
    assert store.live_run_for_issue(UID, db=db) is None
