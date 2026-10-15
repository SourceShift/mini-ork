"""Tests for ``mini_ork.intake.report`` — the run → ticket return leg.

Offline throughout: a temp home with a real migrated ``state.db``, a fake
write-capable source registered under a real id, and a ``config/pm.yaml`` the
test owns. Every store call is handed the temp ``db`` explicitly, so nothing
reads a developer's shell or the repo's shipped config.

What is pinned:

* a finished linked run is reported **once** — one comment, one ledger row, the
  thread settled — and a second pass posts nothing;
* write-back off is ``skipped``, not recorded, **still settles** the thread, and
  makes **no provider call**; enabling it afterwards posts on the next pass;
* a source that raises ``ProviderError`` is ``error``, unrecorded, retried;
* the capability gate (a source that cannot comment) is the same ``skipped``;
* a run still going is left alone;
* ``dry_run`` writes nothing at all — no settle, no comment, no event;
* the comment carries the verdict, the run id, the recipe and the published
  commit when the run dir records one;
* the pass rides ``poll_once``, and ``mini-ork intake report`` has a JSON form.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli import intake_cmd  # noqa: E402
from mini_ork.intake import poll as intake_poll  # noqa: E402
from mini_ork.intake import report as intake_report  # noqa: E402
from mini_ork.intake.trigger import Trigger  # noqa: E402
from mini_ork.pm import registry  # noqa: E402
from mini_ork.pm import store as pm_store  # noqa: E402
from mini_ork.pm.model import Comment, LinkKind  # noqa: E402
from mini_ork.pm.provider import Capabilities, Page, ProviderError  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store as thread_store  # noqa: E402

SOURCE = "acme"
ISSUE = "acme:ENG-1"
RUN = "run-1000000000-aaaaaa"
THREAD = "acme-eng-1"


class Board:
    """A source that reads nothing and writes a comment.

    Everything ``writeback.comment`` touches — ``capabilities`` and
    ``comment`` — plus the three reads ``pm_store.sync`` makes, so the same
    object can drive a real ``poll_once``.
    """

    id = SOURCE
    label = "Acme Issues"

    def __init__(self, *, can_comment: bool = True, fail: Exception | None = None) -> None:
        self.capabilities = Capabilities(comment=can_comment)
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def list_issues(self, query):
        return Page(issues=(), has_more=False)

    def get(self, uid: str):
        raise KeyError(uid)

    def comments(self, uid: str):
        return ()

    def comment(self, uid: str, body: str) -> Comment:
        if self._fail is not None:
            raise self._fail
        self.calls.append((uid, body))
        return Comment(issue_uid=uid, author="mini-ork", body=body)


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


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Point the write-back gate at a ``config/pm.yaml`` this test owns.

    Rewriting the file between calls is how "enabling it afterwards" is
    expressed: the gate rereads the live config each call, which is exactly the
    behaviour under test.
    """

    path = tmp_path / "pm.yaml"

    def write(*, write_back: bool | None = None) -> Path:
        lines = ["sources:", f"  {SOURCE}:", "    enabled: true"]
        if write_back is not None:
            lines.append(f"    write_back: {'true' if write_back else 'false'}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv("MINI_ORK_PM", str(path))
        return path

    write()  # default: declared, no write_back line ⇒ writes are off
    return write


def _link_run(
    db: str,
    *,
    issue_uid: str = ISSUE,
    run_id: str = RUN,
    status: str = "published",
    recipe: str = "code-fix",
    thread_id: str = THREAD,
) -> None:
    """A ticket worked by a run whose thread is live — the state the pass reads."""
    pm_store.link(issue_uid, f"run:{run_id}", LinkKind.RUN, db=db)
    now = int(time.time())
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, "
            "cost_usd, duration_ms, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, "code_fix", recipe, "", status, 0.0, 0, now, now),
        )
        con.commit()
    finally:
        con.close()
    thread_store.open_thread(thread_id, title="Ticket", recipe=recipe, db=db)
    thread_store.link_run(thread_id, run_id, attempt=1, recipe=recipe, db=db)


def _thread_status(thread_id: str, db: str) -> str:
    thread = thread_store.get_thread(thread_id, db=db)
    assert thread is not None
    return str(thread.status)


def _reported(db: str, run_id: str = RUN) -> bool:
    return thread_store.intake_event(SOURCE, f"report:{run_id}", db=db) is not None


# ── the pass ──────────────────────────────────────────────────────────────


def test_a_finished_run_is_reported_once(home, db, registered, write_config):
    """One comment, one ledger row, the thread settled — and then silence."""
    write_config(write_back=True)
    board = registered(Board())
    _link_run(db)

    first = intake_report.report_pass(home, db=db)

    assert [outcome.action for outcome in first] == ["reported"]
    assert board.calls == [(ISSUE, board.calls[0][1])]
    assert RUN in board.calls[0][1]
    assert _reported(db)
    assert _thread_status(THREAD, db) == "finished"

    second = intake_report.report_pass(home, db=db)
    assert second == []
    assert len(board.calls) == 1  # still one comment, not two


def test_the_comment_carries_the_verdict_recipe_and_commit(home, db, registered, write_config):
    write_config(write_back=True)
    board = registered(Board())
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "verdict.json").write_text('{"pass": true}', encoding="utf-8")
    (run_dir / "execute.log").write_text(
        "noise\n[publish] committed 2 file(s): abc1234\n", encoding="utf-8"
    )
    _link_run(db, recipe="code-fix")

    intake_report.report_pass(home, db=db)

    body = board.calls[0][1]
    assert "passed" in body
    assert RUN in body
    assert "code-fix" in body
    assert "abc1234" in body


def test_write_back_off_skips_settles_and_catches_up(home, db, registered, write_config):
    """Zero provider calls, ``skipped``, the thread still settled, nothing
    recorded — and enabling it afterwards posts on the next pass."""
    board = registered(Board())
    _link_run(db)

    first = intake_report.report_pass(home, db=db)

    assert [outcome.action for outcome in first] == ["skipped"]
    assert "write_back" in first[0].reason
    assert board.calls == []  # the gate refused before the adapter was reached
    assert _thread_status(THREAD, db) == "finished"  # settled regardless
    assert not _reported(db)  # not recorded, so it can catch up

    write_config(write_back=True)
    second = intake_report.report_pass(home, db=db)

    assert [outcome.action for outcome in second] == ["reported"]
    assert len(board.calls) == 1
    assert _reported(db)


def test_an_incapable_source_is_skipped_like_write_back_off(home, db, registered, write_config):
    """The gate's second check: ``write_back`` on, but the source cannot
    comment. Same ``skipped``, same zero calls, same no-record."""
    write_config(write_back=True)
    board = registered(Board(can_comment=False))
    _link_run(db)

    outcomes = intake_report.report_pass(home, db=db)

    assert [outcome.action for outcome in outcomes] == ["skipped"]
    assert board.calls == []
    assert not _reported(db)


def test_a_provider_error_is_retried_next_pass(home, db, registered, write_config):
    write_config(write_back=True)
    canary = ProviderError("acme: 503 the source is down")
    board = registered(Board(fail=canary))
    _link_run(db)

    first = intake_report.report_pass(home, db=db)

    assert [outcome.action for outcome in first] == ["error"]
    assert "503" in first[0].reason
    assert not _reported(db)  # unrecorded, so the next pass tries again

    board._fail = None
    second = intake_report.report_pass(home, db=db)

    assert [outcome.action for outcome in second] == ["reported"]
    assert len(board.calls) == 1
    assert _reported(db)


def test_a_pass_scoped_to_one_source_reports_only_its_tickets(home, db, registered, write_config):
    """poll_once runs once per source; each sweep must cover its own tickets only."""
    write_config(write_back=True)
    board = registered(Board())
    _link_run(db)

    assert intake_report.report_pass(home, db=db, source="other") == []
    assert board.calls == []
    assert [o.action for o in intake_report.report_pass(home, db=db, source=SOURCE)] == ["reported"]


def test_a_running_run_is_left_alone(home, db, registered, write_config):
    write_config(write_back=True)
    board = registered(Board())
    _link_run(db, status="executing")

    outcomes = intake_report.report_pass(home, db=db)

    assert outcomes == []
    assert board.calls == []
    assert _thread_status(THREAD, db) == "running"  # not settled
    assert not _reported(db)


def test_a_dry_run_writes_nothing(home, db, registered, write_config):
    write_config(write_back=True)
    board = registered(Board())
    _link_run(db)

    outcomes = intake_report.report_pass(home, db=db, dry_run=True)

    assert [outcome.action for outcome in outcomes] == ["would-report"]
    assert board.calls == []
    assert _thread_status(THREAD, db) == "running"  # no settle
    assert not _reported(db)  # no event


def test_a_run_link_with_no_terminal_status_is_ignored(home, db, registered, write_config):
    """A link to a run that has no ``task_runs`` row at all — an intake that
    never launched, or a wiped table. Nothing to report, nothing raised."""
    write_config(write_back=True)
    board = registered(Board())
    pm_store.link(ISSUE, "run:run-ghost", LinkKind.RUN, db=db)

    outcomes = intake_report.report_pass(home, db=db)

    assert outcomes == []
    assert board.calls == []


# ── wiring ────────────────────────────────────────────────────────────────


def test_poll_once_reports_a_finished_run(home, db, registered, write_config):
    """The simple tier: the pass that dispatches also reports. No new process."""
    write_config(write_back=True)
    board = registered(Board())
    _link_run(db)

    report = intake_poll.poll_once(home, SOURCE, db=db, provider=board, trigger=Trigger())

    assert [outcome.action for outcome in report.reported] == ["reported"]
    assert len(board.calls) == 1
    # The dispatch side is untouched: no ticket matched a (nonexistent) rule.
    assert report.started == 0


def test_the_poll_pass_keeps_its_shape(home, db, registered, write_config):
    """``PollReport`` gained a field, and the existing ones still mean what they
    did — a report starts nothing, so it must not move ``started``."""
    board = registered(Board())
    report = intake_poll.poll_once(home, SOURCE, db=db, provider=board, trigger=Trigger())
    payload = report.to_dict()
    assert payload["started"] == 0
    assert payload["reported"] == []
    assert set(payload) >= {"source", "mirrored", "considered", "outcomes", "started"}


# ── the CLI ───────────────────────────────────────────────────────────────


def test_the_cli_report_verb_has_a_json_form(home, db, registered, write_config, capsys):
    write_config(write_back=True)
    registered(Board())
    _link_run(db)

    rc = intake_cmd.main(
        ["report", "--home", str(home), "--db", db, "--dry-run", "--json"]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["action"] == "would-report"
    assert payload[0]["run_id"] == RUN


def test_the_cli_report_verb_posts_for_real(home, db, registered, write_config, capsys):
    write_config(write_back=True)
    board = registered(Board())
    _link_run(db)

    rc = intake_cmd.main(["report", "--home", str(home), "--db", db])

    assert rc == 0
    assert board.calls  # one comment posted
    assert "reported" in capsys.readouterr().out


def test_the_reported_verb_resolves_in_the_intake_parser():
    """``test_ide_pages_actions`` walks every ``intake`` action; the new verb
    must parse like the ones beside it."""
    parser = intake_cmd.build_parser()
    parser.parse_known_args(["report", "--dry-run", "--json", "--home", "/tmp/x"])
