"""Hermetic tests for ``mini_ork.pm.providers.jira``.

No network: ``mini_ork.pm.http.request_json`` is replaced, so what is pinned is
what *this adapter* asks Jira for and how it bends Jira's payloads onto the
model. The credential seam is real, though — one test drives the true
``request_json`` (with ``urllib.request.urlopen`` replaced) so the scrubbing
that keeps the pair out of an error message is exercised, not assumed. That
matters more here than for Linear: Jira uses HTTP Basic, so the shared scrubber
only ever saw base64 and the raw email and token would leak without this
adapter's own scrub. The vault's default chain is environment-then-file, and
``MINI_ORK_HOME`` is pinned at ``tmp_path`` so the file half cannot pick up
whatever the developer has.

The order follows the module docstring, because the disagreements are the
interesting part:

* ADF becomes markdown in one place, for the description and for comment bodies
  — headings, ordered/unordered/nested lists, fenced code with a language, and
  links.
* status *categories* map onto the model, and the workspace's own status name
  survives in ``native_state`` — an unrecognized category lands in ``BACKLOG``
* priority is Jira's named ladder, taken from the adapter's own table
* the cursor is Jira's opaque ``nextPageToken``, handed back as-is
* the project is scoped server-side by JQL; state, label, assignee and priority
  are narrowed here, so a filter is never silently dropped
* comment and transition are implemented and declared; create, update and
  ``link`` still refuse, and the capability flags say exactly that
"""
from __future__ import annotations

import base64
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
from mini_ork.pm.providers import jira as jira_module  # noqa: E402
from mini_ork.pm.providers import skipped_sources  # noqa: E402

SITE = "https://acme.atlassian.net"
API = f"{SITE}/rest/api/3"
TOKEN = "ATATT3xFfGF0notarealtoken0abcdef1234567890"
EMAIL = "amir@acme.dev"


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

    @property
    def body(self) -> dict:
        return self.calls[-1]["body"]


@pytest.fixture
def transport(monkeypatch) -> _Transport:
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)
    return transport


def _config(token_env: str | None = "JIRA_API_TOKEN", **settings) -> pm_config.SourceConfig:
    merged = {"site": SITE, "email_env": "JIRA_EMAIL", "project": "ENG", **settings}
    return pm_config.SourceConfig(
        id="jira", enabled=True, token_env=token_env, settings=merged
    )


@pytest.fixture
def source(monkeypatch, tmp_path) -> jira_module.JiraProvider:
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    return jira_module.JiraProvider(_config())


def _status(name: str, category: str) -> dict:
    return {"name": name, "statusCategory": {"key": category, "name": name}}


def _node(key: str, **fields) -> dict:
    payload = {
        "id": f"1000{key}",
        "key": key,
        "fields": {
            "summary": f"Issue {key}",
            "description": None,
            "status": _status("To Do", "new"),
            "priority": {"id": "3", "name": "Medium"},
            "labels": [],
            "assignee": None,
            "created": "2026-10-01T00:00:00.000+0000",
            "updated": "2026-10-09T00:00:00.000+0000",
            "project": {"key": "ENG", "name": "Engineering"},
            "parent": None,
        },
    }
    payload["fields"].update(fields)
    return payload


def _page(*nodes, is_last: bool = True, token: str | None = None) -> dict:
    """One page of the enhanced-search envelope."""
    return {"issues": list(nodes), "isLast": is_last, "nextPageToken": token}


# ── ADF → markdown ─────────────────────────────────────────────────────────


#: A captured description in the shape Jira Cloud's REST API v3 returns: a
#: versioned ``doc`` whose children are the block nodes a person typed. It is
#: checked in whole so the converter's behaviour is pinned by a document rather
#: than by a paraphrase of one.
ADF_DESCRIPTION = {
    "version": 1,
    "type": "doc",
    "content": [
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "Summary"}],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Reproduced on "},
                {"type": "text", "text": "staging", "marks": [{"type": "strong"}]},
                {"type": "text", "text": " with "},
                {"type": "text", "text": "checkout", "marks": [{"type": "code"}]},
                {"type": "text", "text": " and a "},
                {
                    "type": "text",
                    "text": "link",
                    "marks": [
                        {
                            "type": "link",
                            "attrs": {"href": f"{SITE}/browse/ENG-1"},
                        }
                    ],
                },
                {"type": "text", "text": "."},
            ],
        },
        {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [{"type": "text", "text": "def boom():\n    raise ValueError()"}],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "second"}]},
                        {
                            "type": "bulletList",
                            "content": [
                                {
                                    "type": "listItem",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "nested"}],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
        },
        {
            "type": "orderedList",
            "attrs": {"order": 1},
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}
                    ],
                },
            ],
        },
    ],
}

ADF_MARKDOWN = "\n\n".join(
    [
        "## Summary",
        "Reproduced on **staging** with `checkout` and a "
        f"[link]({SITE}/browse/ENG-1).",
        "```python\ndef boom():\n    raise ValueError()\n```",
        "- first\n- second\n  - nested",
        "1. one\n2. two",
    ]
)


def test_a_captured_adf_document_converts_to_the_expected_markdown():
    assert jira_module.adf_to_markdown(ADF_DESCRIPTION) == ADF_MARKDOWN


def test_headings_lists_and_code_blocks_convert_independently():
    """The pieces the fixture covers, each pinned on its own so a regression
    names which node kind moved."""
    assert jira_module.adf_to_markdown(
        {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 3},
                 "content": [{"type": "text", "text": "Deep"}]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "em", "marks": [{"type": "em"}]},
                    {"type": "text", "text": " and "},
                    {"type": "text", "text": "gone", "marks": [{"type": "strike"}]},
                ]},
            ],
        }
    ) == "### Deep\n\n*em* and ~~gone~~"

    assert jira_module.adf_to_markdown(
        {"type": "doc", "content": [
            {"type": "codeBlock", "content": [{"type": "text", "text": "plain"}]}]}
    ) == "```\nplain\n```"

    assert jira_module.adf_to_markdown(
        {"type": "doc", "content": [
            {"type": "orderedList", "attrs": {"order": 3}, "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "third"}]}]},
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "fourth"}]}]},
            ]}]}
    ) == "3. third\n4. fourth"


def test_a_plain_string_description_is_passed_through():
    """Not every install answers in v3; wiki markup is better than nothing."""
    assert jira_module.adf_to_markdown("Just text") == "Just text"
    assert jira_module.adf_to_markdown(None) == ""
    assert jira_module.adf_to_markdown({"type": "doc", "content": []}) == ""


# ── mapping ────────────────────────────────────────────────────────────────


def test_an_issue_maps_onto_the_model(source, transport):
    transport.answer(
        _page(
            _node(
                "ENG-7",
                description=ADF_DESCRIPTION,
                labels=["bug", "priority: high"],
                assignee={"displayName": "Amir", "accountId": "acct-1"},
                priority={"id": "2", "name": "High"},
            )
        )
    )
    page = source.list_issues(Query())
    assert len(page.issues) == 1
    issue = page.issues[0]
    assert issue.uid == "jira:ENG-7"
    assert issue.title == "Issue ENG-7"
    assert issue.description == ADF_MARKDOWN
    assert issue.assignee == "Amir"
    assert issue.labels == ("bug", "priority: high")
    assert issue.identity.url == f"{SITE}/browse/ENG-7"
    assert issue.identity.project == "ENG"
    assert issue.priority is Priority.HIGH
    assert issue.native_priority == 2
    assert issue.native["key"] == "ENG-7"
    assert issue.native["status_category"] == "new"
    assert issue.native["priority_name"] == "High"


def test_each_status_category_maps_to_a_normalized_state(source, transport):
    transport.answer(
        _page(
            _node("ENG-1", status=_status("To Do", "new")),
            _node("ENG-2", status=_status("In Progress", "indeterminate")),
            _node("ENG-3", status=_status("Done", "done")),
            _node("ENG-4", status=_status("No Category", "undefined")),
        )
    )
    states = {issue.identity.key: issue.state for issue in source.list_issues(Query()).issues}
    assert states == {
        "ENG-1": IssueState.TODO,
        "ENG-2": IssueState.IN_PROGRESS,
        "ENG-3": IssueState.DONE,
        "ENG-4": IssueState.BACKLOG,
    }


def test_an_unmapped_status_lands_in_backlog_with_its_word_kept(source, transport):
    """The invariant the model exists to hold: normalizing is never lossy."""
    transport.answer(_page(_node("ENG-1", status=_status("Queued", "queued"))))
    issue = source.list_issues(Query()).issues[0]
    assert issue.state is IssueState.BACKLOG
    assert issue.native_state == "Queued"


def test_a_status_with_no_category_at_all_lands_in_backlog(source, transport):
    transport.answer(_page(_node("ENG-1", status={"name": "Waiting"})))
    issue = source.list_issues(Query()).issues[0]
    assert issue.state is IssueState.BACKLOG
    assert issue.native_state == "Waiting"


def test_state_name_is_kept_even_when_the_category_maps_cleanly(source, transport):
    transport.answer(_page(_node("ENG-1", status=_status("In Review", "indeterminate"))))
    issue = source.list_issues(Query()).issues[0]
    assert issue.state is IssueState.IN_PROGRESS
    assert issue.native_state == "In Review"


def test_priority_maps_from_the_jira_name_not_linear_numbering(source, transport):
    """Jira names its priorities; Linear's 1-urgent..4-low inversion must not
    be reused."""
    transport.answer(
        _page(
            _node("ENG-1", priority={"id": "1", "name": "Highest"}),
            _node("ENG-2", priority={"id": "5", "name": "Lowest"}),
            _node("ENG-3", priority={"id": "3", "name": "Medium"}),
            _node("ENG-4", priority={"name": "Unusual"}),
        )
    )
    issues = {issue.identity.key: issue for issue in source.list_issues(Query()).issues}
    assert issues["ENG-1"].priority is Priority.URGENT
    assert issues["ENG-1"].native_priority == 1
    assert issues["ENG-2"].priority is Priority.LOW
    assert issues["ENG-2"].native_priority == 5
    assert issues["ENG-3"].priority is Priority.MEDIUM
    # A name the table does not know claims nothing rather than guessing.
    assert issues["ENG-4"].priority is Priority.NONE
    assert issues["ENG-4"].native_priority is None


def test_an_issue_with_no_description_reads_as_empty(source, transport):
    transport.answer(_page(_node("ENG-1")))
    issue = source.list_issues(Query()).issues[0]
    assert issue.description == ""


# ── the request ────────────────────────────────────────────────────────────


def test_the_read_is_a_post_to_the_enhanced_search_endpoint(source, transport):
    transport.answer(_page())
    source.list_issues(Query())
    assert transport.url == f"{API}/search/jql"
    assert transport.calls[-1]["method"] == "POST"
    # The project is the server-side scope; the named fields keep a page small.
    assert 'project = "ENG"' in transport.body["jql"]
    assert set(transport.body["fields"]) == set(jira_module.FIELDS)


def test_a_limit_above_jiras_ceiling_is_clamped(source, transport):
    transport.answer(_page())
    source.list_issues(Query(limit=500))
    assert transport.body["maxResults"] == jira_module.MAX_PER_PAGE


def test_a_state_jira_cannot_produce_yields_nothing_rather_than_everything(source, transport):
    """No status category maps to IN_REVIEW; an empty match would be everything."""
    page = source.list_issues(Query(states=(IssueState.IN_REVIEW,)))
    assert page.issues == ()
    assert transport.calls == []


def test_the_cursor_is_handed_back_verbatim_as_next_page_token(source, transport):
    transport.answer(_page(_node("ENG-1"), is_last=False, token="tok-1"))
    page = source.list_issues(Query(limit=1))
    assert page.has_more is True
    assert page.cursor == "tok-1"
    assert "nextPageToken" not in transport.body

    transport.answer(_page(_node("ENG-2")))
    second = source.list_issues(Query(cursor=page.cursor))
    assert transport.body["nextPageToken"] == "tok-1"
    assert second.has_more is False
    assert second.cursor is None


def test_a_last_page_has_no_cursor(source, transport):
    transport.answer(_page(_node("ENG-1")))
    page = source.list_issues(Query(limit=50))
    assert page.has_more is False
    assert page.cursor is None


def test_has_more_follows_the_server_not_the_row_count(source, transport):
    """A page narrowed here can come back short with more to fetch."""
    transport.answer(_page(_node("ENG-1"), is_last=False, token="tok-9"))
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
            _node("ENG-1", labels=["bug", "p1"]),
            _node("ENG-2", labels=["bug"]),
            _node("ENG-3", labels=[]),
        )
    )
    keys = [issue.identity.key for issue in source.list_issues(Query(labels=("bug", "p1"))).issues]
    assert keys == ["ENG-1"]


def test_an_assignee_filter_matches_the_display_name(source, transport):
    transport.answer(
        _page(_node("ENG-1", assignee={"displayName": "Amir"}), _node("ENG-2"))
    )
    keys = [issue.identity.key for issue in source.list_issues(Query(assignee="Amir")).issues]
    assert keys == ["ENG-1"]


def test_a_priority_filter_is_narrowed_here(source, transport):
    transport.answer(
        _page(
            _node("ENG-1", priority={"id": "1", "name": "Highest"}),
            _node("ENG-2", priority={"id": "5", "name": "Lowest"}),
        )
    )
    keys = [
        issue.identity.key
        for issue in source.list_issues(Query(priorities=(Priority.URGENT,))).issues
    ]
    assert keys == ["ENG-1"]


# ── get and comments ───────────────────────────────────────────────────────


def test_get_reads_the_issue_by_key_from_the_rest_api(source, transport):
    transport.answer(_node("ENG-7", status=_status("In Progress", "indeterminate")))
    issue = source.get("jira:ENG-7")
    assert transport.url.startswith(f"{API}/issue/ENG-7?")
    assert transport.calls[-1]["method"] == "GET"
    assert issue.identity.key == "ENG-7"
    assert issue.state is IssueState.IN_PROGRESS


def test_get_refuses_a_foreign_uid_and_a_malformed_one(source, transport):
    with pytest.raises(KeyError):
        source.get("linear:ENG-1")
    with pytest.raises(KeyError):
        source.get("jira:ENG")
    assert transport.calls == []


@pytest.mark.parametrize("uid", [
    "jira:../../myself-1",
    "jira:ENG/../../myself-1",
    "jira:ENG-1/comment",
    "jira:ENG-1?x=1",
    "jira:-1",
])
def test_a_key_that_could_re_aim_the_request_is_refused_before_any_call(source, transport, uid):
    """A mirror uid is data. Only a real ``PROJECT-123`` key may become a URL path
    segment, so no uid can point an authenticated request at another endpoint."""
    with pytest.raises(KeyError):
        source.get(uid)
    with pytest.raises(KeyError):
        source.comments(uid)
    assert transport.calls == []


def test_get_raises_key_error_when_jira_answers_404(source, transport):
    transport.answer(
        ProviderError(
            f"jira get ENG-404 failed: HTTP 404 from {API}/issue/ENG-404: "
            '{"errorMessages": ["Issue does not exist"]}'
        )
    )
    with pytest.raises(KeyError):
        source.get("jira:ENG-404")


def test_comments_convert_each_adf_body_to_markdown(source, transport):
    transport.answer(
        {
            "comments": [
                {
                    "id": "c1",
                    "author": {"displayName": "Amir", "accountId": "acct-1"},
                    "created": "2026-10-02T00:00:00.000+0000",
                    "body": {
                        "version": 1,
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Still "},
                                    {
                                        "type": "text",
                                        "text": "reproducing",
                                        "marks": [{"type": "em"}],
                                    },
                                ],
                            }
                        ],
                    },
                }
            ]
        }
    )
    comments = source.comments("jira:ENG-7")
    assert transport.url.startswith(f"{API}/issue/ENG-7/comment?")
    assert len(comments) == 1
    assert comments[0].issue_uid == "jira:ENG-7"
    assert comments[0].author == "Amir"
    assert comments[0].body == "Still *reproducing*"
    assert comments[0].native_id == "c1"
    assert comments[0].url == f"{SITE}/browse/ENG-7"


def test_comments_on_an_unknown_issue_raise_key_error(source, transport):
    transport.answer(
        ProviderError(f"jira comments for ENG-404 failed: HTTP 404 from {API}/issue/ENG-404/comment")
    )
    with pytest.raises(KeyError):
        source.comments("jira:ENG-404")


# ── the writes this adapter declares and the ones it refuses ────────────────


def test_the_declared_writes_are_comment_and_transition(source):
    assert source.capabilities.read_only is False
    assert source.capabilities.writes == ("transition", "comment")


def test_create_update_and_link_still_refuse_without_a_call(source, transport):
    with pytest.raises(Unsupported):
        source.create(Draft(title="x"))
    with pytest.raises(Unsupported):
        source.update("jira:ENG-1", Patch(title="x"))
    with pytest.raises(Unsupported) as caught:
        source.link("jira:ENG-1", "jira:ENG-2", LinkKind.BLOCKS)
    assert "jira" in str(caught.value)
    assert transport.calls == []


# ── the credential ─────────────────────────────────────────────────────────


def test_the_credential_is_the_email_and_token_as_http_basic(source, transport):
    transport.answer(_page())
    source.list_issues(Query())
    expected = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode("ascii")
    assert transport.headers["Authorization"] == expected


def test_the_credential_is_resolved_at_the_first_call_not_at_construction(monkeypatch, tmp_path):
    """``import mini_ork.pm`` must not need a credential; using the source must."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)

    source = jira_module.JiraProvider(_config())  # constructing is fine
    with pytest.raises(ProviderError) as caught:
        source.list_issues(Query())
    assert "JIRA_API_TOKEN" in str(caught.value)
    assert transport.calls == []  # nothing was sent without a credential


def test_a_missing_email_value_fails_closed_naming_the_key(monkeypatch, tmp_path):
    """An unresolvable email key is an error naming the key, never an empty
    string read as "no credential needed"."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)

    source = jira_module.JiraProvider(_config())
    with pytest.raises(ProviderError) as caught:
        source.list_issues(Query())
    assert "JIRA_EMAIL" in str(caught.value)
    assert transport.calls == []


def test_a_missing_credential_never_reads_as_an_empty_string(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    source = jira_module.JiraProvider(_config())
    with pytest.raises(ProviderError):
        assert source._token()  # noqa: SLF001 - the seam this test exists for
    with pytest.raises(ProviderError):
        assert source._email()  # noqa: SLF001


def test_a_401_does_not_leak_the_token_or_the_email(monkeypatch, tmp_path):
    """The failing service echoes both halves back; the message must not.

    Unlike the rest of this file this drives the true ``request_json``, because
    the second half of the scrub lives in this adapter: Basic auth means the
    shared scrubber only ever saw base64, so without the adapter's own scrub the
    raw email and token would survive.
    """
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    echoed = f"{API}/search/jql?email={EMAIL}&api_token={TOKEN}"
    body = json.dumps(
        {
            "errorMessages": [
                f"Client must be authenticated to access this resource: {TOKEN}"
            ],
            "self": echoed,
        }
    ).encode("utf-8")

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(echoed, 401, "Unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    source = jira_module.JiraProvider(_config())
    with pytest.raises(ProviderError) as caught:
        source.list_issues(Query())
    message = str(caught.value)
    assert TOKEN not in message
    assert EMAIL not in message
    assert "***" in message
    assert "authenticated" in message


def test_a_source_with_no_token_key_is_refused_at_construction():
    with pytest.raises(pm_config.PmConfigError) as caught:
        jira_module.JiraProvider(_config(token_env=None))
    assert "token_env" in str(caught.value)


def test_a_source_without_an_email_key_is_refused_at_construction():
    config = pm_config.SourceConfig(
        id="jira", enabled=True, token_env="JIRA_API_TOKEN", settings={"site": SITE, "project": "ENG"}
    )
    with pytest.raises(pm_config.PmConfigError) as caught:
        jira_module.JiraProvider(config)
    assert "email_env" in str(caught.value)


def test_a_source_without_a_site_is_refused_at_construction():
    config = pm_config.SourceConfig(
        id="jira",
        enabled=True,
        token_env="JIRA_API_TOKEN",
        settings={"email_env": "JIRA_EMAIL", "project": "ENG"},
    )
    with pytest.raises(pm_config.PmConfigError) as caught:
        jira_module.JiraProvider(config)
    assert "site" in str(caught.value)


def test_an_api_url_override_replaces_the_default_rest_base(
    source, transport, monkeypatch, tmp_path
):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    source = jira_module.JiraProvider(_config(api_url="https://jira.acme.dev/rest/api/3"))
    transport.answer(_page())
    source.list_issues(Query())
    assert transport.url == "https://jira.acme.dev/rest/api/3/search/jql"


# ── the mirror ─────────────────────────────────────────────────────────────


def test_a_jira_source_fills_the_mirror(source, transport, tmp_path):
    """What this adapter is *for*: the board shows Jira issues."""
    from mini_ork.pm import store as pm_store

    transport.answer(
        _page(
            _node("ENG-1", status=_status("To Do", "new")),
            _node("ENG-2", status=_status("No Category", "undefined")),
        )
    )
    db = str(tmp_path / "mirror.db")
    assert pm_store.sync(source, Query(limit=50), db=db) == 2

    rows = {row.uid: row for row in pm_store.query_rows("jira", db=db)}
    assert set(rows) == {"jira:ENG-1", "jira:ENG-2"}
    assert rows["jira:ENG-1"].state is IssueState.TODO
    assert rows["jira:ENG-2"].state is IssueState.BACKLOG


# ── registration ───────────────────────────────────────────────────────────


def _register(monkeypatch, *sources) -> None:
    from mini_ork.pm import providers as providers_module

    configuration = pm_config.PmConfig(sources={source.id: source for source in sources})
    monkeypatch.setattr(pm_config, "load_config", lambda *a, **k: configuration)
    monkeypatch.setattr(providers_module, "_SKIPPED", [])
    providers_module._register_configured()  # noqa: SLF001


def test_a_configured_jira_source_registers(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    try:
        _register(monkeypatch, _config())
        assert "jira" in registry.provider_ids()
        assert registry.get_provider("jira").label == "Jira"
    finally:
        registry.unregister_provider("jira")
    assert "jira" not in registry.provider_ids()


def test_a_jira_source_that_cannot_be_built_is_skipped_with_a_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    from mini_ork.pm import providers as providers_module

    _register(monkeypatch, _config(token_env=None))
    assert "jira" not in registry.provider_ids()
    skipped = providers_module.skipped_sources()
    assert skipped and skipped[0][0] == "jira" and "token_env" in skipped[0][1]


def test_the_jira_module_imports_with_no_config_and_no_secrets(tmp_path, monkeypatch):
    """``python3.11 -c "import mini_ork.pm.providers.jira"`` is the acceptance
    command: it must succeed with no config file and no vault at all."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)

    import importlib

    module = importlib.import_module("mini_ork.pm.providers.jira")
    assert module.JiraProvider.label == "Jira"
    assert module.JiraProvider(_config()).id == "jira"


def test_the_shipped_config_declares_jira_disabled():
    """The committed ``config/pm.yaml`` enables exactly ``local`` and documents
    the Jira source as a switched-off worked example: every setting a person
    needs is filled in, both credential halves are key *names*, and the source
    stays off so a checkout that has not configured a token is unaffected."""
    configuration = pm_config.load_config(path=REPO / "config" / "pm.yaml")
    assert [source.id for source in configuration.enabled()] == ["local"]
    jira = configuration.source("jira")
    assert jira.enabled is False
    assert jira.token_env == "JIRA_API_TOKEN"
    assert jira.settings.get("email_env") == "JIRA_EMAIL"
    assert jira.settings.get("site") == SITE
    assert jira.settings.get("project") == "ENG"


def test_importing_the_package_degrades_to_local_only():
    """An undeclared or broken Jira never breaks ``import mini_ork.pm``."""
    import mini_ork.pm  # noqa: F401

    assert isinstance(skipped_sources(), tuple)
    assert "local" in registry.provider_ids()
    assert registry.get_provider("local").label == "mini-ork epics"
