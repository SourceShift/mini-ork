"""IDE pages ``thread`` and the ``orch`` intake tab.

These are presentation over readers that are tested elsewhere
(``test_threads_store.py``, ``test_intake_poll.py``), so what is pinned here is
the presentation contract:

* the page key exists in ``PAGES`` and ``build_page`` resolves it;
* ``thread`` with no argument is the index, with ``args["thread"]`` is one
  thread — and an id that is not there is a callout, never an exception;
* the continuation composer runs the same CLI verb the terminal does, so the
  page adds no way to start work the CLI does not already have;
* every section that reaches the IDE carries a ``type`` the renderer knows;
* the intake tab renders its tables even when a home has never synced, which is
  the state a first-run user is in.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.ide_pages import PAGES, build_page  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store  # noqa: E402
from mini_ork.threads.model import MessageRole, RunStatus, ThreadOrigin  # noqa: E402

THREAD_ID = "github-acme-app-12"

#: Every section kind the IDE is known to draw. A page that emits anything else
#: renders as the "needs a newer mini-ork IDE" placeholder, which is a silent
#: downgrade rather than an error — so it is asserted, not hoped for.
SECTION_TYPES = frozenset(
    {
        "kv", "table", "list", "bars", "flow", "code", "markdown", "pills", "chips",
        "dag", "inspector", "hero", "triage", "callout", "story", "files", "findings",
        "checks", "agents", "composer", "columns",
    }
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path) -> None:
    """Two attempts on one thread: the shape the 409's replacement produces."""
    db = str(home / "state.db")
    store.open_thread(
        THREAD_ID,
        title="Retry loop drops 429s",
        origin=ThreadOrigin.INTAKE,
        origin_ref="github:acme/app#12",
        db=db,
    )
    store.append_message(THREAD_ID, role=MessageRole.USER, text="Ticket body.", db=db)
    store.append_message(THREAD_ID, role=MessageRole.ASSISTANT, text="APPROVE", turn=1, db=db)
    store.link_run(THREAD_ID, THREAD_ID, attempt=1, db=db)
    store.settle_run(THREAD_ID, RunStatus.FINISHED, db=db)
    store.append_message(THREAD_ID, role=MessageRole.USER, text="Also handle 503s.", db=db)
    store.link_run(THREAD_ID, f"{THREAD_ID}-t2", attempt=2, db=db)


def _sections(page: dict) -> list[dict]:
    found: list[dict] = []

    def walk(items) -> None:
        for section in items:
            if isinstance(section, list):
                walk(section)
                continue
            found.append(section)

    walk(page.get("sections") or [])
    return found


def _titles(page: dict) -> list[str]:
    return [str(section.get("title") or "") for section in _sections(page)]


# ── the page contract ─────────────────────────────────────────────────────


def test_the_thread_page_is_registered():
    assert PAGES.get("thread") == "Thread"


def test_the_page_resolves_through_build_page(home):
    page = build_page(home, "thread")
    assert page["ok"] is True
    assert page["key"] == "thread"
    assert page["label"] == "Thread"


def test_every_section_is_of_a_type_the_ide_draws(home):
    _seed(home)
    page = build_page(home, "thread", None, {"thread": THREAD_ID})
    types = {section.get("type") for section in _sections(page)}
    assert types <= SECTION_TYPES, types - SECTION_TYPES


# ── the index ─────────────────────────────────────────────────────────────


def test_an_empty_project_says_so_rather_than_showing_nothing(home):
    page = build_page(home, "thread")
    assert page["ok"] is True
    assert "No threads yet" in _titles(page)


def test_the_index_lists_the_threads(home):
    _seed(home)
    page = build_page(home, "thread")
    table = next(s for s in _sections(page) if s.get("type") == "table")
    assert table["title"] == "Threads"
    assert len(table["rows"]) == 1
    # A row opens the thread it names — the only navigation the page offers.
    assert table["rows"][0]["do"] == {"page": "thread", "args": {"thread": THREAD_ID}}


# ── one thread ────────────────────────────────────────────────────────────


def test_a_thread_shows_its_runs_and_its_transcript(home):
    _seed(home)
    page = build_page(home, "thread", None, {"thread": THREAD_ID})

    assert _titles(page) == ["Retry loop drops 429s", "Runs", "Transcript", "Continue"]
    runs = next(s for s in _sections(page) if s.get("title") == "Runs")
    # Both attempts, in order — a continuation is a second row in the same
    # thread, which is the whole point of the feature.
    assert [row["cells"][0]["t"] for row in runs["rows"]] == ["1", "2"]

    transcript = next(s for s in _sections(page) if s.get("title") == "Transcript")
    assert "### You · turn 1" in transcript["text"]
    assert "Also handle 503s." in transcript["text"]


def test_the_header_carries_the_identity_the_rest_of_the_system_uses(home):
    _seed(home)
    page = build_page(home, "thread", None, {"thread": THREAD_ID})
    header = next(s for s in _sections(page) if s.get("type") == "kv")
    facts = {item["k"]: item["v"] for item in header["items"]}
    assert facts["Id"] == THREAD_ID
    assert facts["Origin"] == "intake"
    assert facts["Turns"] == "2"
    assert facts["State"] == "running"


def test_the_composer_continues_the_thread_through_the_cli(home):
    """The page must not invent a second way to drive a thread. Typed text
    becomes the last argument of the same verb the terminal uses."""
    _seed(home)
    page = build_page(home, "thread", None, {"thread": THREAD_ID})
    composer = next(s for s in _sections(page) if s.get("type") == "composer")
    assert composer["cli"] == ["threads", "send", THREAD_ID, "--text"]


def test_a_thread_that_is_not_there_is_a_callout_not_an_exception(home):
    page = build_page(home, "thread", None, {"thread": "nope"})
    assert page["ok"] is True
    assert "No thread 'nope'" in _titles(page)
    assert page["errors"] == {}


def test_a_blank_thread_argument_is_the_index(home):
    """``--arg thread=`` is what a cleared query looks like; it must not be a
    lookup for the empty id."""
    _seed(home)
    page = build_page(home, "thread", None, {"thread": "   "})
    assert "Threads" in _titles(page)


# ── the orch intake tab ───────────────────────────────────────────────────


def test_the_intake_tab_is_declared_on_the_orchestrator_page(home):
    page = build_page(home, "orch")
    assert ("intake", "Intake") in [(t["key"], t["label"]) for t in page["tabs"]]


def test_the_intake_tab_renders_on_a_home_that_never_synced(home):
    """The first-run state: no mirror, no ledger, no configured source. The tab
    must still draw, and must say why nothing will start."""
    page = build_page(home, "orch", "intake")
    assert page["ok"] is True
    assert page["tab"] == "intake"
    assert page["errors"] == {}
    assert "Sources" in _titles(page)
    assert "Recent arrivals" in _titles(page)
    assert "Nothing will start" in _titles(page)


def test_the_intake_tab_offers_the_cli_verbs_and_a_confirmation(home):
    page = build_page(home, "orch", "intake")
    by_label = {action["label"]: action for action in page["actions"]}
    assert by_label["Preview a pass"]["do"]["cli"] == ["intake", "once", "--dry-run"]
    assert by_label["Poll now"]["do"]["cli"] == ["intake", "once"]
    # Starting runs is not something a stray click should do.
    assert by_label["Poll now"]["do"]["confirm"]
    assert "confirm" not in by_label["Preview a pass"]["do"]


def test_the_intake_tab_reads_the_ledger_it_is_given(home):
    db = str(home / "state.db")
    store.record_intake_event(
        "github", "d-1", "hash",
        issue_uid="github:acme/app#12", thread_id=THREAD_ID, action="launched", db=db,
    )
    page = build_page(home, "orch", "intake")
    arrivals = next(s for s in _sections(page) if s.get("title") == "Recent arrivals")
    assert len(arrivals["rows"]) == 1
    assert arrivals["rows"][0]["cells"][1]["t"] == "launched"
