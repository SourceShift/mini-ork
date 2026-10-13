"""Hermetic tests for the write-back gate, ``mini_ork.pm.writeback``.

No network: ``mini_ork.pm.http.request_json`` is replaced, so what is pinned is
what each *adapter* asks its source for — and, just as important, that a
refused write asks for nothing at all. The gate is the thing under test:

* with ``write_back`` unset — or explicitly false, or the string ``"true"`` —
  every source and every operation refuses with :class:`Unsupported` and zero
  HTTP calls, because the opt-in is the first check
* with ``write_back: true`` on a Linear and a Jira source, a comment and a
  transition each reach the source once, with the expected mutation or endpoint
  and body
* a GitHub source opted in still refuses on ``Capabilities`` — it is the
  incapable-adapter case — and sends nothing
* ``Patch(description="")`` and ``Patch()`` forward different values, so
  "clear the field" stays distinguishable from "leave it alone"
* a 401 on a write does not leak the credential

The two refusal reasons are distinct strings ("write_back ..." vs
"... capability"), and the tests match on the reason rather than the class, so
a check silently moving from one gate to the other cannot pass unnoticed.
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
from mini_ork.pm import writeback  # noqa: E402
from mini_ork.pm.model import Issue, IssueState, ProviderIdentity  # noqa: E402
from mini_ork.pm.provider import Capabilities, Patch, ProviderError, Unsupported  # noqa: E402
from mini_ork.pm.providers import github as github_module  # noqa: E402
from mini_ork.pm.providers import jira as jira_module  # noqa: E402
from mini_ork.pm.providers import linear as linear_module  # noqa: E402

LINEAR_TOKEN = "lin_api_notarealkey"
JIRA_TOKEN = "ATATT3xFfGF0notarealtoken0abcdef1234567890"
JIRA_EMAIL = "amir@acme.dev"
GITHUB_TOKEN = "ghp_notarealtoken"
JIRA_SITE = "https://acme.atlassian.net"
JIRA_API = f"{JIRA_SITE}/rest/api/3"

#: The sources this file registers and must unregister afterwards.
SOURCES = ("linear", "jira", "github", "stub")
#: Every source the gate can be pointed at, ``local`` included: the un-opted-in
#: refusal must hold for a source this file never touches, too.
GATE_SOURCES = ("linear", "jira", "github", "local", "stub")
#: The three operations the gate exposes.
OPERATIONS = ("comment", "transition", "update")


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
    def body(self) -> dict:
        return self.calls[-1]["body"]

    @property
    def variables(self) -> dict:
        return self.body["variables"]


class _StubSource:
    """An adapter that can update and remembers what it was handed.

    The one source in the tree with ``update=True`` is ``local``, whose write
    goes to mini-ork's own database; a stub keeps the patch-distinctness proof
    offline and independent of ``trg_epics_no_revert_done``.
    """

    id = "stub"
    label = "Stub"
    capabilities = Capabilities(update=True)

    def __init__(self) -> None:
        self.patches: list[Patch] = []

    def update(self, uid: str, patch: Patch) -> Issue:
        self.patches.append(patch)
        return Issue(
            identity=ProviderIdentity(provider=self.id, key="1"),
            title="x",
            state=IssueState.TODO,
            native_state="Todo",
        )


@pytest.fixture
def transport(monkeypatch) -> _Transport:
    transport = _Transport()
    monkeypatch.setattr(pm_http, "request_json", transport)
    return transport


def _pin_config(tmp_path, monkeypatch, *, write_back: str | None = None) -> Path:
    """Pin ``MINI_ORK_PM`` at a config where every test source is enabled.

    ``write_back`` is the literal YAML value written under each source; ``None``
    omits the key entirely, which is the un-opted-in case the gate must read as
    off. Passing the raw text lets a test hand in ``"false"`` or the string
    ``'"true"'`` as well as the bare ``true``.
    """
    lines = ["sources:"]
    for source_id in (*SOURCES, "local"):
        lines.append(f"  {source_id}:")
        lines.append("    enabled: true")
        if write_back is not None:
            lines.append(f"    write_back: {write_back}")
    path = tmp_path / "pm.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PM", str(path))
    return path


@pytest.fixture
def opted_out(tmp_path, monkeypatch) -> Path:
    return _pin_config(tmp_path, monkeypatch, write_back=None)


@pytest.fixture
def opted_in(tmp_path, monkeypatch) -> Path:
    return _pin_config(tmp_path, monkeypatch, write_back="true")


@pytest.fixture
def sources(monkeypatch, tmp_path):
    """Register one test instance of each adapter the gate can reach.

    The default checkout leaves linear, jira and github disabled, so nothing is
    registered for them; these replace whatever a developer's own config built,
    so the tests pin *this* file's adapters. The registry is restored afterwards.
    """
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", LINEAR_TOKEN)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)

    built = {
        "linear": linear_module.LinearProvider(
            pm_config.SourceConfig(
                id="linear",
                enabled=True,
                token_env="LINEAR_API_KEY",
                settings={"team": "ENG"},
            )
        ),
        "jira": jira_module.JiraProvider(
            pm_config.SourceConfig(
                id="jira",
                enabled=True,
                token_env="JIRA_API_TOKEN",
                settings={"site": JIRA_SITE, "email_env": "JIRA_EMAIL", "project": "ENG"},
            )
        ),
        "github": github_module.GithubProvider(
            pm_config.SourceConfig(
                id="github",
                enabled=True,
                token_env="GITHUB_TOKEN",
                settings={"owner": "acme", "repo": "app"},
            )
        ),
        "stub": _StubSource(),
    }
    for provider in built.values():
        registry.register_provider(provider, replace=True)
    try:
        yield built
    finally:
        for source_id in (*SOURCES,):
            registry.unregister_provider(source_id)


def _call(operation: str, uid: str):
    """Invoke one gate entry point by name, with a valid argument for each."""
    if operation == "comment":
        return writeback.comment(uid, "hello")
    if operation == "transition":
        return writeback.transition(uid, IssueState.DONE)
    return writeback.update(uid, Patch(description=""))


def _linear_node(identifier: str = "ENG-7", *, state: dict | None = None) -> dict:
    """A Linear issue node in the shape ``_issue`` maps."""
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"Issue {identifier}",
        "description": "body",
        "url": f"https://linear.app/acme/issue/{identifier}",
        "priority": 0,
        "priorityLabel": "No priority",
        "createdAt": "2026-10-01T00:00:00.000Z",
        "updatedAt": "2026-10-09T00:00:00.000Z",
        "state": state or {"name": "Todo", "type": "unstarted"},
        "assignee": None,
        "labels": {"nodes": []},
        "team": {"key": "ENG", "name": "Engineering"},
    }


def _jira_node(key: str = "ENG-7", *, category: str = "new") -> dict:
    """A Jira issue payload in the shape ``_issue`` maps."""
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": f"Issue {key}",
            "description": None,
            "status": {"name": "To Do", "statusCategory": {"key": category}},
            "priority": {"id": "3", "name": "Medium"},
            "labels": [],
            "assignee": None,
            "created": "2026-10-01T00:00:00.000+0000",
            "updated": "2026-10-09T00:00:00.000+0000",
            "project": {"key": "ENG"},
            "parent": None,
        },
    }


# ── the opt-in gate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("source_id", GATE_SOURCES)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_without_write_back_every_write_refuses_before_any_call(
    sources, transport, opted_out, source_id, operation
):
    with pytest.raises(Unsupported) as caught:
        _call(operation, f"{source_id}:ENG-1")
    message = str(caught.value)
    assert source_id in message
    assert operation in message
    assert "write_back" in message
    assert "capability" not in message
    assert transport.calls == []


def test_write_back_false_is_off(sources, transport, tmp_path, monkeypatch):
    _pin_config(tmp_path, monkeypatch, write_back="false")
    with pytest.raises(Unsupported) as caught:
        writeback.comment("linear:ENG-1", "hello")
    assert "write_back" in str(caught.value)
    assert transport.calls == []


def test_only_a_real_true_opts_in_a_string_does_not(
    sources, transport, tmp_path, monkeypatch
):
    """A quoted "true" is a typo, and a typo must fail closed, not enable writes."""
    _pin_config(tmp_path, monkeypatch, write_back='"true"')
    with pytest.raises(Unsupported):
        writeback.comment("linear:ENG-1", "hello")
    assert transport.calls == []


def test_an_unconfigured_source_is_not_opted_in(sources, transport, tmp_path, monkeypatch):
    """A source the config never declares has no flag; absence is not consent."""
    path = tmp_path / "pm.yaml"
    path.write_text("sources:\n  local:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PM", str(path))
    with pytest.raises(Unsupported) as caught:
        writeback.transition("linear:ENG-1", IssueState.DONE)
    assert "write_back" in str(caught.value)
    assert transport.calls == []


# ── the capability gate ─────────────────────────────────────────────────────


@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_opted_in_but_incapable_source_refuses_on_capability(
    sources, transport, opted_in, operation
):
    with pytest.raises(Unsupported) as caught:
        _call(operation, "github:acme/app#7")
    message = str(caught.value)
    assert "github" in message
    assert operation in message
    assert "capability" in message
    assert "write_back" not in message
    assert transport.calls == []


# ── Linear: comment and transition ──────────────────────────────────────────


def test_an_opted_in_linear_comment_posts_the_comment_mutation(sources, transport, opted_in):
    transport.answer(
        {
            "data": {
                "commentCreate": {
                    "success": True,
                    "comment": {
                        "id": "c1",
                        "body": "hello",
                        "createdAt": "2026-10-02T00:00:00.000Z",
                        "url": "https://linear.app/acme/issue/ENG-7#c1",
                        "user": {"name": "Amir", "displayName": "amir"},
                    },
                }
            }
        }
    )
    comment = writeback.comment("linear:ENG-7", "hello")
    assert len(transport.calls) == 1
    assert transport.url == linear_module.API_URL
    assert "commentCreate" in transport.body["query"]
    assert transport.variables == {"issueId": "ENG-7", "body": "hello"}
    assert comment.issue_uid == "linear:ENG-7"
    assert comment.author == "Amir"
    assert comment.body == "hello"
    assert comment.native_id == "c1"


def test_an_opted_in_linear_transition_reads_states_then_updates(sources, transport, opted_in):
    transport.answer(
        {
            "data": {
                "workflowStates": {
                    "nodes": [
                        {"id": "s-todo", "name": "Todo", "type": "unstarted"},
                        {"id": "s-done", "name": "Done", "type": "completed"},
                    ]
                }
            }
        },
        {
            "data": {
                "issueUpdate": {
                    "success": True,
                    "issue": _linear_node(state={"name": "Done", "type": "completed"}),
                }
            }
        },
    )
    issue = writeback.transition("linear:ENG-7", IssueState.DONE)
    assert len(transport.calls) == 2
    assert "workflowStates" in transport.calls[0]["body"]["query"]
    assert transport.calls[0]["body"]["variables"] == {"teamKey": "ENG"}
    assert "issueUpdate" in transport.calls[1]["body"]["query"]
    assert transport.calls[1]["body"]["variables"] == {"id": "ENG-7", "stateId": "s-done"}
    assert issue.uid == "linear:ENG-7"
    assert issue.state is IssueState.DONE


def test_a_linear_transition_to_an_unmappable_state_refuses_after_the_lookup(
    sources, transport, opted_in
):
    """``IN_REVIEW`` is not a Linear workflow-state type; the lookup finds none."""
    transport.answer(
        {"data": {"workflowStates": {"nodes": [{"id": "s-todo", "name": "Todo", "type": "unstarted"}]}}}
    )
    with pytest.raises(Unsupported) as caught:
        writeback.transition("linear:ENG-7", IssueState.IN_REVIEW)
    assert "linear" in str(caught.value)
    assert len(transport.calls) == 1  # the lookup; no mutation follows


def test_a_linear_comment_on_a_declined_mutation_raises(sources, transport, opted_in):
    transport.answer({"data": {"commentCreate": {"success": False, "comment": None}}})
    with pytest.raises(ProviderError) as caught:
        writeback.comment("linear:ENG-7", "hello")
    assert "was not accepted" in str(caught.value)


# ── Jira: comment and transition ────────────────────────────────────────────


def test_an_opted_in_jira_comment_posts_an_adf_body(sources, transport, opted_in):
    transport.answer(
        {
            "id": "10001",
            "author": {"displayName": "Amir"},
            "created": "2026-10-02T00:00:00.000+0000",
            "body": {
                "version": 1,
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
            },
        }
    )
    comment = writeback.comment("jira:ENG-7", "hello")
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"] == f"{JIRA_API}/issue/ENG-7/comment"
    # The body went as a document, not a string.
    assert transport.body["body"]["type"] == "doc"
    assert transport.body["body"]["content"][0]["content"][0]["text"] == "hello"
    assert comment.issue_uid == "jira:ENG-7"
    assert comment.author == "Amir"
    assert comment.body == "hello"


def test_an_opted_in_jira_transition_reads_transitions_then_posts(sources, transport, opted_in):
    transport.answer(
        {
            "transitions": [
                {
                    "id": "21",
                    "name": "Start",
                    "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                },
                {
                    "id": "31",
                    "name": "Done",
                    "to": {"name": "Done", "statusCategory": {"key": "done"}},
                },
            ]
        },
        None,  # the POST answers 204 No Content
        _jira_node(category="done"),
    )
    issue = writeback.transition("jira:ENG-7", IssueState.DONE)
    assert [call["method"] for call in transport.calls] == ["GET", "POST", "GET"]
    assert transport.calls[0]["url"] == f"{JIRA_API}/issue/ENG-7/transitions"
    assert transport.calls[1]["url"] == f"{JIRA_API}/issue/ENG-7/transitions"
    assert transport.calls[1]["body"] == {"transition": {"id": "31"}}
    assert issue.uid == "jira:ENG-7"
    assert issue.state is IssueState.DONE


def test_a_jira_transition_to_an_unreachable_state_refuses_after_the_lookup(
    sources, transport, opted_in
):
    transport.answer(
        {
            "transitions": [
                {
                    "id": "21",
                    "name": "Start",
                    "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                }
            ]
        }
    )
    with pytest.raises(Unsupported) as caught:
        writeback.transition("jira:ENG-7", IssueState.DONE)
    assert "jira" in str(caught.value)
    assert len(transport.calls) == 1  # the lookup; no POST follows


# ── Patch distinctness ──────────────────────────────────────────────────────


def test_update_forwards_the_patch_so_clearing_differs_from_leaving_alone(
    sources, transport, opted_in
):
    stub = sources["stub"]
    writeback.update("stub:1", Patch(description=""))
    writeback.update("stub:1", Patch())
    assert stub.patches[0].description == ""
    assert stub.patches[1].description is None
    assert stub.patches[0] != stub.patches[1]
    assert transport.calls == []


# ── the credential ──────────────────────────────────────────────────────────


def test_a_401_on_a_linear_write_does_not_leak_the_token(sources, monkeypatch, opted_in, tmp_path):
    """The failing service echoes the key back; the message must not.

    Drives the true ``request_json`` (with ``urllib.request.urlopen`` replaced)
    because the scrub lives there — replacing it would pin everything except the
    property this test is about.
    """
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", LINEAR_TOKEN)
    body = json.dumps(
        {
            "errors": [{"message": f"Authentication required for key {LINEAR_TOKEN}"}],
            "request_headers": {"Authorization": LINEAR_TOKEN},
        }
    ).encode("utf-8")

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            linear_module.API_URL, 401, "Unauthorized", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    with pytest.raises(ProviderError) as caught:
        writeback.comment("linear:ENG-7", "hello")
    message = str(caught.value)
    assert LINEAR_TOKEN not in message
    assert "***" in message
    assert "Authentication required" in message


def test_a_401_on_a_jira_write_scrubs_the_credential_pair(sources, monkeypatch, opted_in, tmp_path):
    """Basic auth base64-wraps the pair, so the shared scrubber cannot see it —
    the adapter's own scrub is what keeps both halves out of the message."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    body = json.dumps({"errorMessages": [f"{JIRA_EMAIL}:{JIRA_TOKEN} was rejected"]}).encode("utf-8")

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            f"{JIRA_API}/issue/ENG-7/comment", 401, "Unauthorized", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    with pytest.raises(ProviderError) as caught:
        writeback.comment("jira:ENG-7", "hello")
    message = str(caught.value)
    assert JIRA_TOKEN not in message
    assert JIRA_EMAIL not in message
