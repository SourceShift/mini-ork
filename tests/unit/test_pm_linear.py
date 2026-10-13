"""Hermetic tests for ``mini_ork.pm.providers.linear``.

No network: ``mini_ork.pm.http.request_json`` is replaced, so what is pinned is
what *this adapter* asks Linear for and how it bends Linear's GraphQL payloads
onto the model. The credential seam is real, though — one test drives the true
``request_json`` (with ``urllib.request.urlopen`` replaced) so the scrubbing
that keeps a key out of an error message is exercised, not assumed. The vault's
default chain is environment-then-file, and ``MINI_ORK_HOME`` is pinned at
``tmp_path`` so the file half cannot pick up whatever the developer has.

The order follows the module docstring, because the disagreements are the
interesting part:

* workflow-state *types* map onto the model, and the workspace's own name
  survives in ``native_state`` — an unrecognized type lands in ``BACKLOG``
* priority is Linear's 1-urgent..4-low inversion, taken from the model
* the cursor is Linear's opaque token, handed back as ``after``
* team and state are filtered server-side; label, assignee and priority are
  narrowed here, so a filter is never silently dropped
* read-only: every write refuses, and ``link`` is among them
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.pm import config as pm_config  # noqa: E402
from mini_ork.pm import http as pm_http  # noqa: E402
from mini_ork.pm import registry  # noqa: E402
from mini_ork.pm.model import IssueState, LinkKind, Priority  # noqa: E402
from mini_ork.pm.provider import Draft, Patch, ProviderError, Query, Unsupported  # noqa: E402
from mini_ork.pm.providers import linear as linear_module  # noqa: E402
from mini_ork.pm.providers import skipped_sources  # noqa: E402

TOKEN = "lin_api_notarealkey"


class _Transport:
    """A stand-in for the GraphQL layer: what was sent, and what to answer."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queued: list = []

    def answer(self, *payloads) -> None:
        self.queued.extend(payloads)

    def __call__(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.queued:
            raise AssertionError(f"nothing queued for {url}")
        payload = self.queued.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return payload

    @property
    def url(self) -> str:
        return self.calls[-1]["url"]

    @property
    def headers(self) -> dict[str, str]:
        return self.calls[-1]["headers"]

    @property
    def body(self) -> dict:
        return self.calls[-1]["body"]

    @property
    def variables(self) -> dict:
        return self.body["variables"]


@pytest.fixture
def transport(monkeypatch) -> _Transport:
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)
    return transport


def _config(token_env: str | None = "LINEAR_API_KEY", **settings) -> pm_config.SourceConfig:
    return pm_config.SourceConfig(
        id="linear",
        enabled=True,
        token_env=token_env,
        settings={"team": "ENG", **settings},
    )


@pytest.fixture
def source(monkeypatch, tmp_path) -> linear_module.LinearProvider:
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", TOKEN)
    return linear_module.LinearProvider(_config())


def _node(identifier: str, **fields) -> dict:
    payload = {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"Issue {identifier}",
        "description": f"body {identifier}",
        "url": f"https://linear.app/acme/issue/{identifier}",
        "priority": 0,
        "priorityLabel": "No priority",
        "createdAt": "2026-10-01T00:00:00.000Z",
        "updatedAt": "2026-10-09T00:00:00.000Z",
        "state": {"name": "Todo", "type": "unstarted"},
        "assignee": None,
        "labels": {"nodes": []},
        "team": {"key": "ENG", "name": "Engineering"},
    }
    payload.update(fields)
    return payload


def _page(*nodes, has_more: bool = False, end_cursor: str | None = None) -> dict:
    """A GraphQL envelope holding one page of the ``issues`` connection."""
    return {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": has_more, "endCursor": end_cursor},
                "nodes": list(nodes),
            }
        }
    }


# ── mapping ────────────────────────────────────────────────────────────────


def test_an_issue_maps_onto_the_model(source, transport):
    transport.answer(
        _page(
            _node(
                "ENG-7",
                labels={"nodes": [{"name": "bug"}, {"name": "priority: high"}]},
                assignee={"name": "Amir", "displayName": "amir"},
            )
        )
    )
    page = source.list_issues(Query())
    assert len(page.issues) == 1
    issue = page.issues[0]
    assert issue.uid == "linear:ENG-7"
    assert issue.title == "Issue ENG-7"
    assert issue.description == "body ENG-7"
    assert issue.assignee == "Amir"
    assert issue.labels == ("bug", "priority: high")
    assert issue.identity.url == "https://linear.app/acme/issue/ENG-7"
    assert issue.identity.project == "ENG"
    assert issue.native["identifier"] == "ENG-7"
    assert issue.native["state_type"] == "unstarted"


def test_each_workflow_type_maps_to_a_normalized_state(source, transport):
    transport.answer(
        _page(
            _node("ENG-1", state={"name": "Backlog", "type": "backlog"}),
            _node("ENG-2", state={"name": "Todo", "type": "unstarted"}),
            _node("ENG-3", state={"name": "In Progress", "type": "started"}),
            _node("ENG-4", state={"name": "Done", "type": "completed"}),
            _node("ENG-5", state={"name": "Canceled", "type": "canceled"}),
            # Triage is a real Linear type: not yet promised to anyone.
            _node("ENG-6", state={"name": "Triage", "type": "triage"}),
        )
    )
    states = {issue.identity.key: issue.state for issue in source.list_issues(Query()).issues}
    assert states == {
        "ENG-1": IssueState.BACKLOG,
        "ENG-2": IssueState.TODO,
        "ENG-3": IssueState.IN_PROGRESS,
        "ENG-4": IssueState.DONE,
        "ENG-5": IssueState.CANCELED,
        "ENG-6": IssueState.BACKLOG,
    }


def test_an_unmapped_state_lands_in_backlog_with_its_word_kept(source, transport):
    """The invariant the model exists to hold: normalizing is never lossy."""
    transport.answer(_page(_node("ENG-1", state={"name": "Iceboxed", "type": "queued"})))
    issue = source.list_issues(Query()).issues[0]
    assert issue.state is IssueState.BACKLOG
    assert issue.native_state == "Iceboxed"


def test_state_name_is_kept_even_when_the_type_maps_cleanly(source, transport):
    transport.answer(_page(_node("ENG-1", state={"name": "In Review", "type": "started"})))
    issue = source.list_issues(Query()).issues[0]
    assert issue.state is IssueState.IN_PROGRESS
    assert issue.native_state == "In Review"


def test_priority_uses_the_existing_linear_inversion(source, transport):
    """1 is urgent and 4 is low in Linear; the model counts up. Both ends."""
    transport.answer(
        _page(
            _node("ENG-1", priority=1),
            _node("ENG-2", priority=4),
            _node("ENG-3", priority=0),
        )
    )
    issues = {issue.identity.key: issue for issue in source.list_issues(Query()).issues}
    assert issues["ENG-1"].priority is Priority.URGENT
    assert issues["ENG-1"].native_priority == 1
    assert issues["ENG-2"].priority is Priority.LOW
    assert issues["ENG-2"].native_priority == 4
    assert issues["ENG-3"].priority is Priority.NONE
    # The inversion is applied, not restated: the model function is authority.
    assert issues["ENG-1"].priority is Priority.from_linear(1)
    assert issues["ENG-2"].priority is Priority.from_linear(4)


# ── the request ────────────────────────────────────────────────────────────


def test_the_read_is_a_graphql_post_scoped_to_the_team(source, transport):
    transport.answer(_page())
    source.list_issues(Query())
    assert transport.url == linear_module.API_URL
    assert transport.calls[-1]["method"] == "POST"
    assert transport.variables["filter"]["team"] == {"key": {"eq": "ENG"}}
    # A query document is sent, not a REST path.
    assert "issues(" in transport.body["query"]


def test_a_limit_above_linear_ceiling_is_clamped(source, transport):
    transport.answer(_page())
    source.list_issues(Query(limit=500))
    assert transport.variables["first"] == linear_module.MAX_PER_PAGE


def test_a_state_filter_is_sent_as_the_workflow_types(source, transport):
    transport.answer(_page())
    source.list_issues(Query(states=(IssueState.BACKLOG, IssueState.TODO)))
    assert transport.variables["filter"]["state"] == {
        "type": {"in": ["backlog", "triage", "unstarted"]}
    }


def test_a_state_linear_cannot_produce_yields_nothing_rather_than_everything(source, transport):
    """An empty ``in`` would match everything server-side; return nothing."""
    page = source.list_issues(Query(states=(IssueState.IN_REVIEW,)))
    assert page.issues == ()
    assert transport.calls == []


def test_the_cursor_is_handed_back_verbatim_as_after(source, transport):
    transport.answer(_page(_node("ENG-1"), has_more=True, end_cursor="tok-1"))
    page = source.list_issues(Query(limit=1))
    assert page.has_more is True
    assert page.cursor == "tok-1"
    assert transport.variables["after"] is None

    transport.answer(_page(_node("ENG-2")))
    second = source.list_issues(Query(cursor=page.cursor))
    assert transport.variables["after"] == "tok-1"
    assert second.has_more is False
    assert second.cursor is None


def test_a_short_last_page_has_no_cursor(source, transport):
    transport.answer(_page(_node("ENG-1")))
    page = source.list_issues(Query(limit=50))
    assert page.has_more is False
    assert page.cursor is None


def test_has_more_follows_the_server_not_the_row_count(source, transport):
    """A page narrowed here can come back short with more to fetch."""
    transport.answer(
        _page(_node("ENG-1"), has_more=True, end_cursor="tok-9"),
    )
    page = source.list_issues(Query(labels=("absent",)))
    assert page.issues == ()
    assert page.has_more is True
    assert page.cursor == "tok-9"


def test_text_search_and_project_filtering_are_refused_not_ignored(source, transport):
    for query, operation in (
        (Query(text="login"), "text search"),
        (Query(project="Launch"), "project"),
    ):
        with pytest.raises(Unsupported) as caught:
            source.list_issues(query)
        assert operation in str(caught.value)
    assert transport.calls == []


def test_a_label_filter_is_an_all_of_match_narrowed_here(source, transport):
    transport.answer(
        _page(
            _node("ENG-1", labels={"nodes": [{"name": "bug"}, {"name": "p1"}]}),
            _node("ENG-2", labels={"nodes": [{"name": "bug"}]}),
            _node("ENG-3", labels={"nodes": []}),
        )
    )
    keys = [issue.identity.key for issue in source.list_issues(Query(labels=("bug", "p1"))).issues]
    assert keys == ["ENG-1"]


def test_an_assignee_filter_matches_the_name_linear_maps(source, transport):
    transport.answer(_page(_node("ENG-1", assignee={"name": "Amir"}), _node("ENG-2")))
    keys = [issue.identity.key for issue in source.list_issues(Query(assignee="Amir")).issues]
    assert keys == ["ENG-1"]


def test_a_priority_filter_is_narrowed_here(source, transport):
    transport.answer(_page(_node("ENG-1", priority=1), _node("ENG-2", priority=4)))
    keys = [
        issue.identity.key
        for issue in source.list_issues(Query(priorities=(Priority.URGENT,))).issues
    ]
    assert keys == ["ENG-1"]


def test_a_graphql_error_arrives_with_http_200_and_is_raised(source, transport):
    transport.answer({"errors": [{"message": "Cannot query field 'nope'"}]})
    with pytest.raises(ProviderError) as caught:
        source.list_issues(Query())
    assert "Cannot query field" in str(caught.value)


def test_a_response_without_data_is_a_provider_error(source, transport):
    transport.answer({"data": None})
    with pytest.raises(ProviderError):
        source.list_issues(Query())


# ── get and comments ───────────────────────────────────────────────────────


def test_get_reads_the_team_and_number_from_the_uid(source, transport):
    transport.answer(_page(_node("OPS-7", team={"key": "OPS", "name": "Ops"})))
    issue = source.get("linear:OPS-7")
    assert transport.variables["filter"] == {
        "team": {"key": {"eq": "OPS"}},
        "number": {"eq": 7},
    }
    assert issue.identity.key == "OPS-7"


def test_get_refuses_a_foreign_uid_and_a_malformed_one(source, transport):
    with pytest.raises(KeyError):
        source.get("github:ENG-1")
    with pytest.raises(KeyError):
        source.get("linear:ENG")
    assert transport.calls == []


def test_get_raises_key_error_when_linear_returns_nothing(source, transport):
    transport.answer(_page())
    with pytest.raises(KeyError):
        source.get("linear:ENG-9")


def test_comments_map_onto_the_model(source, transport):
    transport.answer(
        {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "c1",
                                        "body": "Still reproducing",
                                        "createdAt": "2026-10-02T00:00:00.000Z",
                                        "url": "https://linear.app/acme/issue/ENG-7#c1",
                                        "user": {"name": "Amir", "displayName": "amir"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }
    )
    comments = source.comments("linear:ENG-7")
    assert transport.variables["filter"] == {
        "team": {"key": {"eq": "ENG"}},
        "number": {"eq": 7},
    }
    assert len(comments) == 1
    assert comments[0].issue_uid == "linear:ENG-7"
    assert comments[0].author == "Amir"
    assert comments[0].body == "Still reproducing"
    assert comments[0].native_id == "c1"


def test_comments_on_an_unknown_issue_raise_key_error(source, transport):
    transport.answer({"data": {"issues": {"nodes": []}}})
    with pytest.raises(KeyError):
        source.comments("linear:ENG-404")


# ── read-only ──────────────────────────────────────────────────────────────


def test_every_write_refuses_and_no_capability_claims_one(source, transport):
    assert source.capabilities.read_only is True
    assert source.capabilities.writes == ()
    with pytest.raises(Unsupported):
        source.create(Draft(title="x"))
    with pytest.raises(Unsupported):
        source.update("linear:ENG-1", Patch(title="x"))
    with pytest.raises(Unsupported):
        source.transition("linear:ENG-1", IssueState.DONE)
    with pytest.raises(Unsupported):
        source.comment("linear:ENG-1", "hello")
    with pytest.raises(Unsupported) as caught:
        source.link("linear:ENG-1", "linear:ENG-2", LinkKind.BLOCKS)
    assert "linear" in str(caught.value)
    assert transport.calls == []


# ── the credential ─────────────────────────────────────────────────────────


def test_the_api_key_comes_from_the_vault_by_key_name(source, transport):
    transport.answer(_page())
    source.list_issues(Query())
    # Linear takes a personal API key as the header value itself, no Bearer.
    assert transport.headers["Authorization"] == TOKEN


def test_the_api_key_is_resolved_at_the_first_call_not_at_construction(monkeypatch, tmp_path):
    """``import mini_ork.pm`` must not need a key; using the source must."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)

    source = linear_module.LinearProvider(_config())  # constructing is fine
    with pytest.raises(ProviderError) as caught:
        source.list_issues(Query())
    assert "LINEAR_API_KEY" in str(caught.value)
    assert transport.calls == []  # nothing was sent without a credential


def test_a_missing_api_key_never_reads_as_an_empty_string(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    source = linear_module.LinearProvider(_config())
    with pytest.raises(ProviderError):
        assert source._token()  # noqa: SLF001 - the seam this test exists for


def test_a_401_does_not_leak_the_api_key(monkeypatch, tmp_path):
    """The failing service echoes the key back; the message must not.

    Unlike the rest of this file this drives the true ``request_json``, because
    the scrub lives there — replacing it would pin everything except the one
    property the test is about.
    """
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", TOKEN)
    body = json.dumps(
        {
            "errors": [{"message": f"Authentication required for key {TOKEN}"}],
            "request_headers": {"Authorization": TOKEN},
        }
    ).encode("utf-8")

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            linear_module.API_URL, 401, "Unauthorized", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    source = linear_module.LinearProvider(_config())
    with pytest.raises(ProviderError) as caught:
        source.list_issues(Query())
    message = str(caught.value)
    assert TOKEN not in message
    assert "***" in message
    assert "Authentication required" in message


def test_a_source_with_no_token_key_is_refused_at_construction():
    with pytest.raises(pm_config.PmConfigError) as caught:
        linear_module.LinearProvider(_config(token_env=None))
    assert "token_env" in str(caught.value)


def test_a_source_without_a_team_is_refused_at_construction():
    config = pm_config.SourceConfig(
        id="linear", enabled=True, token_env="LINEAR_API_KEY", settings={}
    )
    with pytest.raises(pm_config.PmConfigError) as caught:
        linear_module.LinearProvider(config)
    assert "team" in str(caught.value)


def test_an_api_url_override_replaces_the_public_one(source, transport, monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", TOKEN)
    source = linear_module.LinearProvider(
        _config(api_url="https://linear.acme.dev/graphql")
    )
    transport.answer(_page())
    source.list_issues(Query())
    assert transport.url == "https://linear.acme.dev/graphql"


# ── the mirror ─────────────────────────────────────────────────────────────


def test_a_linear_source_fills_the_mirror(source, transport, tmp_path):
    """What this adapter is *for*: the board shows Linear issues."""
    from mini_ork.pm import store as pm_store

    transport.answer(
        _page(
            _node("ENG-1", state={"name": "Todo", "type": "unstarted"}),
            _node("ENG-2", state={"name": "Backlog", "type": "backlog"}),
        )
    )
    db = str(tmp_path / "mirror.db")
    assert pm_store.sync(source, Query(limit=50), db=db) == 2

    rows = {row.uid: row for row in pm_store.query_rows("linear", db=db)}
    assert set(rows) == {"linear:ENG-1", "linear:ENG-2"}
    assert rows["linear:ENG-1"].state is IssueState.TODO
    assert rows["linear:ENG-2"].state is IssueState.BACKLOG


# ── registration ───────────────────────────────────────────────────────────


def _register(monkeypatch, *sources) -> None:
    from mini_ork.pm import providers as providers_module

    configuration = pm_config.PmConfig(sources={source.id: source for source in sources})
    monkeypatch.setattr(pm_config, "load_config", lambda *a, **k: configuration)
    monkeypatch.setattr(providers_module, "_SKIPPED", [])
    providers_module._register_configured()  # noqa: SLF001


def test_a_configured_linear_source_registers(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", TOKEN)
    try:
        _register(monkeypatch, _config())
        assert "linear" in registry.provider_ids()
        assert registry.get_provider("linear").label == "Linear"
    finally:
        registry.unregister_provider("linear")
    assert "linear" not in registry.provider_ids()


def test_a_linear_source_that_cannot_be_built_is_skipped_with_a_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    from mini_ork.pm import providers as providers_module

    _register(monkeypatch, _config(token_env=None))
    assert "linear" not in registry.provider_ids()
    skipped = providers_module.skipped_sources()
    assert skipped and skipped[0][0] == "linear" and "token_env" in skipped[0][1]


def test_importing_the_package_degrades_to_local_only():
    """An undeclared or broken Linear never breaks ``import mini_ork.pm``."""
    import mini_ork.pm  # noqa: F401

    assert isinstance(skipped_sources(), tuple)
    assert "local" in registry.provider_ids()
    assert registry.get_provider("local").label == "mini-ork epics"


def test_the_shipped_config_declares_linear_disabled():
    """The committed ``config/pm.yaml`` enables exactly ``local`` and documents
    the Linear source as a switched-off example, so a checkout that has not
    configured a key still imports and still shows its own epics."""
    configuration = pm_config.load_config(path=REPO / "config" / "pm.yaml")
    assert [source.id for source in configuration.enabled()] == ["local"]
    linear = configuration.source("linear")
    assert linear.enabled is False
    assert linear.settings.get("team") == "ENG"
