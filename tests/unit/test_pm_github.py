"""Hermetic tests for ``mini_ork.pm.providers.github``.

No network: ``mini_ork.pm.http.request_json`` is replaced, so what is pinned is
what *this adapter* asks for and how it bends GitHub's payloads onto the model.
The credential seam is real, though — the vault's default chain is
environment-then-file, and ``MINI_ORK_HOME`` is pinned at ``tmp_path`` so the
file half cannot pick up whatever the developer actually has.

The order follows the module docstring, because the disagreements are the
interesting part:

* a pull request is not an issue
* ``open``/``closed`` plus ``state_reason``, including the two ways a closed
  issue must never land in an open column
* there is no priority, so a ``priority:`` label survives verbatim
* the three filters GitHub cannot honour are refused, not ignored
* a cursor is a page number, and ``has_more`` errs toward one extra fetch
* read-only: every write refuses through ``Capabilities``
"""
from __future__ import annotations

import sys
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
from mini_ork.pm.providers import github as github_module  # noqa: E402
from mini_ork.pm.providers import skipped_sources  # noqa: E402

TOKEN = "ghp_notarealtoken"


class _Transport:
    """A stand-in for the HTTP layer: what was sent, and what to answer."""

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

    def parameter(self, name: str) -> str | None:
        """One query parameter of the last request, or ``None``."""
        from urllib.parse import parse_qs, urlsplit

        values = parse_qs(urlsplit(self.url).query).get(name)
        return values[0] if values else None


@pytest.fixture
def transport(monkeypatch) -> _Transport:
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)
    return transport


def _config(token_env: str | None = "GITHUB_TOKEN", **settings) -> pm_config.SourceConfig:
    return pm_config.SourceConfig(
        id="github",
        enabled=True,
        token_env=token_env,
        settings={"owner": "acme", "repo": "app", **settings},
    )


@pytest.fixture
def source(monkeypatch, tmp_path) -> github_module.GithubProvider:
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    return github_module.GithubProvider(_config())


def _issue(number: int, **fields) -> dict:
    payload = {
        "number": number,
        "title": f"Issue {number}",
        "body": f"body {number}",
        "state": "open",
        "state_reason": None,
        "html_url": f"https://github.com/acme/app/issues/{number}",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-09T00:00:00Z",
        "labels": [],
        "assignee": None,
        "user": {"login": "octocat"},
    }
    payload.update(fields)
    return payload


def _pull_request(number: int) -> dict:
    return _issue(number, title="A PR", pull_request={"url": "https://api.github.com/x"})


# ── mapping ────────────────────────────────────────────────────────────────


def test_an_issue_maps_onto_the_model(source, transport):
    transport.answer(
        [
            _issue(
                7,
                labels=[{"name": "bug"}, {"name": "priority: high"}],
                assignee={"login": "amir"},
            )
        ]
    )
    page = source.list_issues(Query())
    assert len(page.issues) == 1
    issue = page.issues[0]
    assert issue.uid == "github:acme/app#7"
    assert issue.title == "Issue 7"
    assert issue.description == "body 7"
    assert issue.native_state == "open"
    assert issue.assignee == "amir"
    assert issue.labels == ("bug", "priority: high")
    assert issue.identity.url == "https://github.com/acme/app/issues/7"
    assert issue.identity.project == "acme/app"
    assert issue.native["number"] == 7
    assert issue.native["user"] == "octocat"
    assert issue.native["is_pull_request"] is False


def test_an_assigned_open_issue_is_todo_and_an_unassigned_one_is_backlog(source, transport):
    transport.answer([_issue(1, assignee={"login": "amir"}), _issue(2, assignee=None)])
    states = {issue.identity.key: issue.state for issue in source.list_issues(Query()).issues}
    assert states["acme/app#1"] is IssueState.TODO
    assert states["acme/app#2"] is IssueState.BACKLOG


def test_a_pull_request_is_not_an_issue(source, transport):
    transport.answer([_issue(1), _pull_request(2), _issue(3)])
    keys = [issue.identity.key for issue in source.list_issues(Query()).issues]
    assert keys == ["acme/app#1", "acme/app#3"]


def test_a_completed_close_is_done_and_a_not_planned_close_is_canceled(source, transport):
    transport.answer(
        [
            _issue(1, state="closed", state_reason="completed"),
            _issue(2, state="closed", state_reason="not_planned"),
            _issue(3, state="closed", state_reason="duplicate"),
        ]
    )
    states = {issue.identity.key: issue.state for issue in source.list_issues(Query()).issues}
    assert states["acme/app#1"] is IssueState.DONE
    assert states["acme/app#2"] is IssueState.CANCELED
    assert states["acme/app#3"] is IssueState.CANCELED


def test_a_closed_issue_never_lands_in_an_open_column(source, transport):
    """No reason, and a reason from a newer API — both read as DONE.

    Backlog is the fallback for a state we do not understand, and it would put
    a closed issue in an open column, which is worse than claiming it was done.
    """
    transport.answer([_issue(1, state="closed"), _issue(2, state="closed", state_reason="obsolete")])
    states = {issue.identity.key: issue.state for issue in source.list_issues(Query()).issues}
    assert states["acme/app#1"] is IssueState.DONE
    assert states["acme/app#2"] is IssueState.DONE


def test_an_unrecognized_state_lands_in_backlog_with_its_word_kept(source, transport):
    transport.answer([_issue(1, state="queued")])
    issue = source.list_issues(Query()).issues[0]
    assert issue.state is IssueState.BACKLOG
    assert issue.native_state == "queued"


def test_there_is_no_priority_so_a_priority_label_survives_verbatim(source, transport):
    transport.answer([_issue(1, labels=[{"name": "priority: urgent"}])])
    issue = source.list_issues(Query()).issues[0]
    assert issue.priority is Priority.NONE
    assert issue.native_priority is None
    assert "priority: urgent" in issue.labels


# ── the request ────────────────────────────────────────────────────────────


def test_the_default_query_asks_for_open_work_newest_first(source, transport):
    transport.answer([])
    source.list_issues(Query())
    assert transport.url.startswith("https://api.github.com/repos/acme/app/issues?")
    assert transport.parameter("state") == "open"
    assert transport.parameter("sort") == "updated"
    assert transport.parameter("direction") == "desc"
    assert transport.parameter("page") == "1"


def test_labels_and_assignee_are_sent_as_github_filters(source, transport):
    transport.answer([])
    source.list_issues(Query(labels=("bug", "p1"), assignee="amir", limit=20))
    assert transport.parameter("labels") == "bug,p1"
    assert transport.parameter("assignee") == "amir"
    assert transport.parameter("per_page") == "20"


def test_a_limit_above_github_ceiling_is_clamped(source, transport):
    """Asking for 500 would be clamped by the API anyway, which would make
    ``has_more`` a lie."""
    transport.answer([])
    source.list_issues(Query(limit=500))
    assert transport.parameter("per_page") == str(github_module.MAX_PER_PAGE)


def test_mixed_states_ask_for_all_and_are_narrowed_afterwards(source, transport):
    """GitHub cannot separate ``completed`` from ``not_planned`` server-side."""
    transport.answer(
        [
            _issue(1, assignee={"login": "amir"}),  # TODO
            _issue(2, state="closed", state_reason="completed"),  # DONE
            _issue(3, state="closed", state_reason="not_planned"),  # CANCELED
        ]
    )
    page = source.list_issues(Query(states=(IssueState.TODO, IssueState.CANCELED)))
    assert transport.parameter("state") == "all"
    assert [issue.identity.key for issue in page.issues] == ["acme/app#1", "acme/app#3"]


def test_a_state_github_cannot_produce_yields_nothing_rather_than_everything(source, transport):
    transport.answer([_issue(1, assignee={"login": "amir"}), _issue(2)])
    page = source.list_issues(Query(states=(IssueState.IN_REVIEW,)))
    assert transport.parameter("state") == "open"
    assert page.issues == ()


def test_a_cursor_is_the_next_page_number(source, transport):
    transport.answer([_issue(1), _issue(2)])
    page = source.list_issues(Query(cursor="3", limit=2))
    assert transport.parameter("page") == "3"
    assert page.has_more is True
    assert page.cursor == "4"


def test_a_short_page_ends_the_pagination(source, transport):
    transport.answer([_issue(1)])
    page = source.list_issues(Query(limit=2))
    assert page.has_more is False
    assert page.cursor is None


def test_an_unparseable_cursor_starts_over_rather_than_raising(source, transport):
    transport.answer([])
    source.list_issues(Query(cursor="not-a-page"))
    assert transport.parameter("page") == "1"


def test_a_filter_github_cannot_honour_is_refused_not_ignored(source, transport):
    for query, operation in (
        (Query(text="login"), "text search"),
        (Query(priorities=(Priority.URGENT,)), "priority"),
        (Query(project="acme/other"), "project"),
    ):
        with pytest.raises(Unsupported) as caught:
            source.list_issues(query)
        assert operation in str(caught.value)
    assert transport.calls == []


# ── get and comments ───────────────────────────────────────────────────────


def test_get_reads_the_repository_from_the_uid(source, transport):
    transport.answer(_issue(7))
    issue = source.get("github:other/thing#7")
    assert transport.url.endswith("/repos/other/thing/issues/7")
    assert issue.identity.key == "acme/app#7"  # the source's own repo


def test_get_refuses_a_pull_request_a_foreign_uid_and_a_malformed_one(source, transport):
    transport.answer(_pull_request(7))
    with pytest.raises(KeyError):
        source.get("github:acme/app#7")
    with pytest.raises(KeyError):
        source.get("local:acme/app#7")
    with pytest.raises(KeyError):
        source.get("github:acme/app")
    assert len(transport.calls) == 1  # only the pull request got as far as a call


def test_comments_map_onto_the_model(source, transport):
    transport.answer(
        [
            {
                "id": 991,
                "body": "Still reproducing",
                "created_at": "2026-10-02T00:00:00Z",
                "html_url": "https://github.com/acme/app/issues/7#issuecomment-991",
                "user": {"login": "amir"},
            }
        ]
    )
    comments = source.comments("github:acme/app#7")
    assert transport.url.startswith("https://api.github.com/repos/acme/app/issues/7/comments?")
    assert transport.parameter("per_page") == str(github_module.MAX_PER_PAGE)
    assert len(comments) == 1
    assert comments[0].issue_uid == "github:acme/app#7"
    assert comments[0].author == "amir"
    assert comments[0].body == "Still reproducing"
    assert comments[0].native_id == "991"


# ── read-only ──────────────────────────────────────────────────────────────


def test_every_write_refuses_and_no_capability_claims_one(source, transport):
    assert source.capabilities.read_only is True
    assert source.capabilities.writes == ()
    with pytest.raises(Unsupported):
        source.create(Draft(title="x"))
    with pytest.raises(Unsupported):
        source.update("github:acme/app#7", Patch(title="x"))
    with pytest.raises(Unsupported):
        source.transition("github:acme/app#7", IssueState.DONE)
    with pytest.raises(Unsupported):
        source.comment("github:acme/app#7", "hello")
    with pytest.raises(Unsupported):
        source.link("github:acme/app#7", "github:acme/app#8", LinkKind.BLOCKS)
    assert transport.calls == []


# ── the credential ─────────────────────────────────────────────────────────


def test_the_token_comes_from_the_vault_by_key_name(source, transport):
    transport.answer([])
    source.list_issues(Query())
    assert transport.headers["Authorization"] == f"Bearer {TOKEN}"


def test_the_token_is_resolved_at_the_first_call_not_at_construction(monkeypatch, tmp_path):
    """``import mini_ork.pm`` must not need a token; using the source must."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)

    source = github_module.GithubProvider(_config())  # constructing is fine
    with pytest.raises(ProviderError) as caught:
        source.list_issues(Query())
    assert "GITHUB_TOKEN" in str(caught.value)
    assert transport.calls == []  # nothing was sent without a credential


def test_a_missing_token_never_reads_as_an_empty_string(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    source = github_module.GithubProvider(_config())
    with pytest.raises(ProviderError):
        assert source._token()  # noqa: SLF001 - the seam this test exists for


def test_a_source_with_no_token_key_is_refused_at_construction():
    with pytest.raises(pm_config.PmConfigError) as caught:
        github_module.GithubProvider(_config(token_env=None))
    assert "token_env" in str(caught.value)


def test_a_source_missing_owner_or_repo_is_refused_at_construction():
    config = pm_config.SourceConfig(id="github", enabled=True, token_env="GITHUB_TOKEN", settings={})
    with pytest.raises(pm_config.PmConfigError):
        github_module.GithubProvider(config)


def test_an_enterprise_api_url_replaces_the_public_one(source, transport, monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    source = github_module.GithubProvider(_config(api_url="https://github.acme.dev/api/v3/"))
    transport.answer([])
    source.list_issues(Query())
    assert transport.url.startswith("https://github.acme.dev/api/v3/repos/acme/app/issues?")


# ── the mirror ─────────────────────────────────────────────────────────────


def test_a_github_source_fills_the_mirror(source, transport, tmp_path):
    """What this adapter is *for*: the board shows GitHub issues.

    The board reads ``pm_issues``, never the adapter, so a source that maps
    correctly and does not fill the mirror has not delivered anything.
    """
    from mini_ork.pm import store as pm_store

    transport.answer([_issue(1, assignee={"login": "amir"}), _issue(2)])
    db = str(tmp_path / "mirror.db")
    assert pm_store.sync(source, Query(limit=50), db=db) == 2

    rows = {row.uid: row for row in pm_store.query_rows("github", db=db)}
    assert set(rows) == {"github:acme/app#1", "github:acme/app#2"}
    assert rows["github:acme/app#1"].title == "Issue 1"
    assert rows["github:acme/app#1"].state is IssueState.TODO
    assert rows["github:acme/app#2"].state is IssueState.BACKLOG


def test_a_mirror_row_can_be_linked_to_the_run_that_works_it(source, transport, tmp_path):
    """``pm_links`` keys by uid, so the uid minted here is the one intake and
    the scheduler will hand back. A shape mismatch would surface as an edge
    that never resolves."""
    from mini_ork.pm import store as pm_store
    from mini_ork.pm.model import LinkKind

    transport.answer([_issue(1)])
    db = str(tmp_path / "mirror.db")
    pm_store.sync(source, Query(limit=50), db=db)
    pm_store.link("github:acme/app#1", "run:github-acme-app-1", LinkKind.RUN, db=db)

    edges = pm_store.links_for("github:acme/app#1", db=db)
    assert [(edge.to_uid, LinkKind(edge.kind)) for edge in edges] == [
        ("run:github-acme-app-1", LinkKind.RUN)
    ]


# ── registration ───────────────────────────────────────────────────────────


def _register(monkeypatch, *sources) -> None:
    from mini_ork.pm import providers as providers_module

    configuration = pm_config.PmConfig(sources={source.id: source for source in sources})
    monkeypatch.setattr(pm_config, "load_config", lambda *a, **k: configuration)
    monkeypatch.setattr(providers_module, "_SKIPPED", [])
    providers_module._register_configured()  # noqa: SLF001


def test_the_shipped_config_needs_no_credential():
    """The committed ``config/pm.yaml`` enables exactly ``local``.

    This is the property that lets ``import mini_ork.pm`` work on a checkout
    nobody has configured. Asserted against the file rather than the ambient
    environment, so a developer's own home config cannot change the answer.
    """
    configuration = pm_config.load_config(path=REPO / "config" / "pm.yaml")
    assert [source.id for source in configuration.enabled()] == ["local"]


def test_an_enabled_source_with_no_adapter_registers_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    _register(monkeypatch, pm_config.SourceConfig(id="local", enabled=True))
    assert "local" in registry.provider_ids()
    assert "github" not in registry.provider_ids()


def test_a_configured_source_registers_and_an_unconfigured_one_does_not(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    try:
        _register(monkeypatch, _config())
        assert registry.provider_ids() == ("github", "local")
        assert registry.get_provider("github").label == "GitHub Issues"
    finally:
        registry.unregister_provider("github")
    assert registry.provider_ids() == ("local",)


def test_a_source_that_cannot_be_built_is_skipped_with_a_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    from mini_ork.pm import providers as providers_module

    _register(monkeypatch, _config(token_env=None))
    assert "github" not in registry.provider_ids()
    skipped = providers_module.skipped_sources()
    assert skipped and skipped[0][0] == "github" and "token_env" in skipped[0][1]


def test_skipped_sources_is_a_tuple_at_import(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    assert isinstance(skipped_sources(), tuple)
