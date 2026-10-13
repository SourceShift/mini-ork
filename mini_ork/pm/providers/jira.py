"""Jira, seen through the PM model.

Jira is the third network source and the heaviest to authenticate: an install
URL, the account's email and an API token, sent as HTTP Basic. That shape
decides the three things this module does that Linear's does not:

* **The credential is a pair, and neither half is a value in this repo.** Both
  come from the vault by *key name* — ``token_env`` for the token, ``email_env``
  for the email — and are encoded together at the last moment. Refusing at
  construction when either key is unnamed is what keeps ``import mini_ork.pm``
  working with no vault at all (see ``providers/__init__.py``).

* **Basic auth hides the secret from the shared scrubber.** ``pm/http.py``
  scrubs the credential it was handed *as a header value*, which for GitHub and
  Linear is the token itself. Basic wraps ``email:token`` in base64 first, so
  the raw token and the raw email are no longer recognisable in what was sent —
  and a failing Jira that echoes them back would leak both. The scrub is
  therefore extended here: every message this adapter raises is passed through
  :meth:`JiraProvider._scrub`, which removes the resolved email and token. See
  the module's tests for the 401 that proves it.

* **The description and the comments are documents, not strings.** Jira's REST
  API v3 carries both as Atlassian Document Format — a recursive JSON tree of
  ``paragraph``/``heading``/``list``/``codeBlock`` nodes. :func:`adf_to_markdown`
  is the one place that flattens it, called for a description and for every
  comment body, so the two never drift apart; :func:`markdown_to_adf` is its
  inverse for the one document this adapter *writes*, a comment body.

Where Jira and the model disagree, resolved here and nowhere else:

* **A status has a stable *category* and a workspace-chosen *name*.** The
  ``statusCategory.key`` — ``new``, ``indeterminate``, ``done``, ``undefined`` —
  is Jira's own invariant across every workspace; the ``status.name`` ("In
  Review", "Won't Do") is not. So :data:`STATE_BY_CATEGORY` keys on the
  category and the name survives verbatim in ``native_state``. A status whose
  category maps to nothing — Jira's ``undefined``, or a category this build has
  not seen — lands in ``BACKLOG``, the one state that claims nothing.

* **Priority is named, not numbered.** Jira says "Highest".."Lowest", where
  Linear counts 1..4 and the model counts up. The name→state table lives in
  :data:`PRIORITY_BY_NAME`; the model's ``from_linear`` inversion does not apply
  and is not reused. Jira's numeric priority ``id`` is kept as
  ``native_priority`` when it is one, and the name goes into ``native``.

* **Search is JQL, and the filters still narrow here.** The read is scoped
  server-side by project; the state, label, assignee and priority matches are
  applied to the returned page, exactly as Linear's are, so the filter
  semantics live in this file — where they are tested — instead of in a remote
  query grammar.

**Writes are gated, not read-only.** Jira's API can create, update, link and
comment; this slice implements ``comment`` and ``transition`` and declares them
through ``Capabilities``, leaving ``create``/``update``/``link`` false because
carrying a capability with no working code behind it would make the board offer
a control it refuses. Every write goes through :mod:`mini_ork.pm.writeback`,
which refuses unless the operator opted the source in with ``write_back: true``
*and* the capability below is set; the adapter itself does no gating, because a
policy split across two files is a policy that drifts.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any, Mapping, Sequence

from mini_ork.pm import http as pm_http
from mini_ork.pm.config import PmConfigError, SourceConfig
from mini_ork.pm.model import (
    Comment,
    Issue,
    IssueState,
    Link,
    LinkKind,
    Priority,
    ProviderIdentity,
    split_uid,
)
from mini_ork.pm.provider import (
    Capabilities,
    Draft,
    Page,
    Patch,
    ProviderError,
    Query,
    Unsupported,
)

#: The public REST API version this adapter speaks. A self-hosted or proxied
#: install names its own base in ``api_url``.
API_VERSION = "/rest/api/3"

#: Jira's own ceiling on one page of results. Asking for more is clamped by the
#: server, which would make ``has_more`` a lie, so it is clamped here.
MAX_PER_PAGE = 100

#: The fields a read asks for. Naming them keeps a page small: an issue payload
#: carries every custom field the workspace has defined otherwise.
FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "status",
    "priority",
    "labels",
    "assignee",
    "created",
    "updated",
    "project",
    "parent",
)

#: Jira's ``statusCategory.key`` → the normalized state it means. The key is
#: stable across workspaces and languages; the ``name`` beside it is not, which
#: is why the mapping keys on the key and keeps the name only as
#: ``native_state``.
#:
#: ``new`` is Jira's To-Do column — work that exists and is queued, which is the
#: model's ``TODO``, not ``BACKLOG``. ``undefined`` is Jira's own "no category",
#: the least committal thing a status can be, so it reads ``BACKLOG`` exactly as
#: an unrecognized category does.
STATE_BY_CATEGORY: Mapping[str, IssueState] = {
    "new": IssueState.TODO,
    "indeterminate": IssueState.IN_PROGRESS,
    "done": IssueState.DONE,
    "undefined": IssueState.BACKLOG,
}

#: The inverse of :data:`STATE_BY_CATEGORY`: the categories that normalize to
#: each state. A state Jira cannot produce — ``IN_REVIEW``, ``BLOCKED``,
#: ``CANCELED``, whose statuses all sit inside one of the four categories —
#: inverts to no categories at all, which is what makes a filter for it return
#: nothing rather than everything.
CATEGORIES_BY_STATE: Mapping[IssueState, tuple[str, ...]] = {
    state: tuple(sorted(key for key, value in STATE_BY_CATEGORY.items() if value is state))
    for state in IssueState
}

#: Jira's priority *name* (lowercased) → the model's urgency. Linear's numeric
#: inversion does not apply here: Jira names its priorities, and a workspace may
#: rename or add its own, so an unrecognized name lands in ``NONE`` rather than
#: guessing.
PRIORITY_BY_NAME: Mapping[str, Priority] = {
    "highest": Priority.URGENT,
    "blocker": Priority.URGENT,
    "critical": Priority.URGENT,
    "high": Priority.HIGH,
    "medium": Priority.MEDIUM,
    "low": Priority.LOW,
    "lowest": Priority.LOW,
    "trivial": Priority.LOW,
}

#: One level of list nesting. Two spaces is what GitHub-flavoured markdown —
#: which is what the board renders — reads as a sub-list.
_INDENT = "  "


# ── ADF → markdown ─────────────────────────────────────────────────────────


def adf_to_markdown(document: Any) -> str:
    """An Atlassian Document Format tree as markdown.

    The one converter, called for a description and for every comment body. A
    value that is not an ADF document — ``None``, or a plain string from an
    install that answered in wiki markup — is passed through unchanged, so a
    source that is not quite v3 still renders something.
    """
    if isinstance(document, str):
        return document
    if not isinstance(document, Mapping):
        return ""
    return _blocks(document.get("content")).strip()


def markdown_to_adf(text: str) -> Mapping[str, Any]:
    """Plain text → a minimal ADF document, the inverse of :func:`adf_to_markdown`.

    The one converter in the other direction, used for the single document this
    adapter writes: a comment body. A body mini-ork posts is prose, so this
    emits ``paragraph`` nodes and nothing else — no headings, lists or marks —
    which is the smallest document Jira's v3 API accepts. Blank-line-separated
    blocks become paragraphs and a line break inside one becomes a
    ``hardBreak``, so a body survives a round trip through
    :func:`adf_to_markdown` unchanged.

    Kept beside the reader rather than in a module of its own: the two halves of
    the conversion are one format, and splitting them is how they drift.
    """
    blocks: list[str] = []
    buffer: list[str] = []
    for line in str(text or "").split("\n"):
        if line.strip():
            buffer.append(line)
        elif buffer:
            blocks.append("\n".join(buffer))
            buffer = []
    if buffer:
        blocks.append("\n".join(buffer))
    if not blocks:
        return {"version": 1, "type": "doc", "content": [{"type": "paragraph"}]}
    return {
        "version": 1,
        "type": "doc",
        "content": [_paragraph(block) for block in blocks],
    }


def _paragraph(block: str) -> Mapping[str, Any]:
    """One paragraph node: its lines as text, joined by ``hardBreak`` nodes."""
    content: list[dict[str, Any]] = []
    for index, line in enumerate(block.split("\n")):
        if index:
            content.append({"type": "hardBreak"})
        content.append({"type": "text", "text": line})
    return {"type": "paragraph", "content": content}


def _attr(node: Mapping[str, Any], name: str) -> Any:
    attrs = node.get("attrs")
    return attrs.get(name) if isinstance(attrs, Mapping) else None


def _blocks(nodes: Any) -> str:
    """A sequence of block nodes, rendered and separated by a blank line."""
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        return ""
    rendered = [_block(node) for node in nodes]
    return "\n\n".join(part for part in rendered if part)


def _block(node: Any) -> str:
    """One block node as markdown. Unknown kinds recurse rather than vanish."""
    if not isinstance(node, Mapping):
        return ""
    kind = node.get("type")
    if kind == "paragraph":
        return _inline(node.get("content"))
    if kind == "heading":
        level = _attr(node, "level")
        level = int(level) if isinstance(level, (int, float)) else 1
        level = max(1, min(6, level))
        return "#" * level + " " + _inline(node.get("content"))
    if kind == "codeBlock":
        language = str(_attr(node, "language") or "")
        code = "".join(
            str(child.get("text") or "")
            for child in node.get("content") or []
            if isinstance(child, Mapping)
        )
        return f"```{language}\n{code}\n```"
    if kind in ("bulletList", "orderedList"):
        return _list(node, depth=0)
    if kind == "blockquote":
        body = _blocks(node.get("content"))
        return "\n".join(f"> {line}" if line else ">" for line in body.split("\n"))
    if kind == "rule":
        return "---"
    if kind in ("mediaSingle", "mediaGroup", "media"):
        # An attachment the model has nowhere to put; a placeholder keeps the
        # document's shape rather than dropping a reference silently.
        return "<!-- media -->"
    if node.get("content") is not None:
        return _blocks(node.get("content"))
    return _inline(node.get("content"))


def _list(node: Mapping[str, Any], depth: int) -> str:
    """A bullet or ordered list, rendered at ``depth`` nesting levels."""
    ordered = node.get("type") == "orderedList"
    start = _attr(node, "order")
    start = int(start) if isinstance(start, (int, float)) else 1
    lines: list[str] = []
    for offset, item in enumerate(node.get("content") or []):
        if not isinstance(item, Mapping):
            continue
        marker = f"{start + offset}." if ordered else "-"
        lines.extend(_list_item(item, marker, depth))
    return "\n".join(lines)


def _list_item(item: Mapping[str, Any], marker: str, depth: int) -> list[str]:
    """One list item's lines: the marker on the first, nested lists below it."""
    pad = _INDENT * depth
    # Continuation lines and nested items clear the marker's own width.
    hang = " " * (len(marker) + 1)
    lines: list[str] = []
    first = True
    for child in item.get("content") or []:
        if not isinstance(child, Mapping):
            continue
        if child.get("type") in ("bulletList", "orderedList"):
            lines.extend(_list(child, depth + 1).split("\n"))
            continue
        text = _block(child)
        if not text:
            continue
        rows = text.split("\n")
        if first:
            lines.append(f"{pad}{marker} {rows[0]}")
            first = False
        else:
            lines.append(f"{pad}{hang}{rows[0]}")
        lines.extend(f"{pad}{hang}{row}" for row in rows[1:])
    if not lines:
        lines.append(f"{pad}{marker}")
    return lines


def _inline(nodes: Any) -> str:
    """A sequence of inline nodes, concatenated with no separator."""
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        return ""
    return "".join(_inline_node(node) for node in nodes)


def _inline_node(node: Any) -> str:
    if not isinstance(node, Mapping):
        return ""
    kind = node.get("type")
    if kind == "text":
        return _with_marks(str(node.get("text") or ""), node.get("marks"))
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return "@" + str(_attr(node, "text") or "")
    if kind == "emoji":
        return str(_attr(node, "text") or "")
    if kind in ("inlineCard", "card"):
        url = str(_attr(node, "url") or "")
        return f"[{url}]({url})" if url else ""
    if kind == "status":
        return str(_attr(node, "text") or "")
    if kind == "date":
        return str(_attr(node, "timestamp") or "")
    # An unknown inline node still carries its text somewhere below it.
    return _inline(node.get("content"))


def _with_marks(text: str, marks: Any) -> str:
    """``text`` with its ADF marks applied as markdown, in the order given."""
    if not text or not isinstance(marks, Sequence) or isinstance(marks, (str, bytes)):
        return text
    for mark in marks:
        if not isinstance(mark, Mapping):
            continue
        kind = mark.get("type")
        if kind == "strong":
            text = f"**{text}**"
        elif kind == "em":
            text = f"*{text}*"
        elif kind == "strike":
            text = f"~~{text}~~"
        elif kind == "code":
            text = f"`{text}`"
        elif kind == "underline":
            text = f"<u>{text}</u>"
        elif kind == "link":
            href = str(_attr(mark, "href") or "")
            if href:
                text = f"[{text}]({href})"
    return text


# ── payload mapping ────────────────────────────────────────────────────────


#: A Jira issue key: a project key (a letter, then letters, digits or
#: underscores) and a number. Anything else is refused before it reaches a URL
#: path, so a mirror uid such as ``jira:../../myself-1`` cannot re-aim an
#: authenticated request at another endpoint.
_ISSUE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _status_state(status: Mapping[str, Any]) -> IssueState:
    """The normalized state for one Jira status payload. See the module docstring."""
    category = _mapping(status.get("statusCategory"))
    return STATE_BY_CATEGORY.get(str(category.get("key") or ""), IssueState.BACKLOG)


def _native_state(status: Mapping[str, Any]) -> str:
    """Jira's own word for the status: the workspace's ``name``, else the category."""
    name = str(status.get("name") or "").strip()
    if name:
        return name
    return str(_mapping(status.get("statusCategory")).get("name") or "")


def _priority(priority: Mapping[str, Any]) -> Priority:
    name = str(priority.get("name") or "").strip().lower()
    return PRIORITY_BY_NAME.get(name, Priority.NONE)


def _native_priority(priority: Mapping[str, Any]) -> int | None:
    """Jira's numeric priority id, when it has one.

    Jira names priorities; the ``id`` is a per-instance string that is often a
    small number, and when it is, it is the closest thing to Linear's numeric
    ``native_priority`` and is kept. A non-numeric id is left to ``native``.
    """
    identifier = str(priority.get("id") or "").strip()
    return int(identifier) if identifier.isdigit() else None


def _display_name(person: Any) -> str:
    """A Jira user's name, from ``displayName``, else ``name``."""
    account = _mapping(person)
    return str(account.get("displayName") or account.get("name") or "")


def _requested_states(states: Sequence[IssueState]) -> frozenset[IssueState]:
    """Coerce to enum members.

    ``IssueState`` is a ``StrEnum``, whose members hash by *name*, so a plain
    ``"done"`` string in a set would silently match nothing.
    """
    return frozenset(IssueState(state) for state in states)


class JiraProvider:
    """One Jira project as a PM source.

    ``site`` is the install (``https://acme.atlassian.net``), ``email_env`` and
    ``token_env`` are vault key *names* for the two halves of the Basic
    credential, and ``project`` is the key the source reads. The registry id is
    the source id, so a second project is a second entry (``jira-ops``) with its
    own ``project`` and its own key names, and the uids stay unambiguous:
    ``jira:ENG-123``.
    """

    label = "Jira"
    capabilities = Capabilities(
        # comment and transition are implemented and gated by
        # mini_ork.pm.writeback; create/update/link are not, and saying so is
        # what keeps the board from offering a control this adapter refuses.
        create=False,
        update=False,
        transition=True,
        comment=True,
        link=False,
        search=False,
        states=True,
        priorities=True,
        labels=True,
        assignees=True,
        pagination=True,
    )

    def __init__(self, config: SourceConfig, *, timeout: float | None = None) -> None:
        self.id = config.id
        self._config = config
        self._timeout = pm_http.DEFAULT_TIMEOUT if timeout is None else timeout
        (site, email_env, project) = config.require("site", "email_env", "project")
        self._site = str(site).rstrip("/")
        self._email_env = str(email_env).strip()
        self._project = str(project)
        if not (config.token_env or "").strip():
            # Refused at construction rather than at the first call: a source
            # that means to authenticate and names no key cannot work at all,
            # and finding that out here names the setting instead of failing
            # one HTTP request later with no explanation.
            raise PmConfigError(
                f"pm source {config.id!r} needs token_env naming the vault key that "
                "holds its Jira API token"
            )
        self._api = str(config.settings.get("api_url") or f"{self._site}{API_VERSION}")
        self._api = self._api.rstrip("/")
        self._token_cache: str | None = None
        self._email_cache: str | None = None

    # ── credential ─────────────────────────────────────────────────────────

    def _issued(self, key: str) -> str:
        """Resolve one vault key name to its value. Raises; never returns ``""``."""
        from mini_ork.dispatch.secrets import SecretStoreError
        from mini_ork.dispatch.vault import vault_environment

        try:
            environment = vault_environment()
        except SecretStoreError as exc:
            raise ProviderError(f"{self.id}: the secret vault is unusable: {exc}") from exc
        value = environment.resolve(key)
        if not value:
            raise ProviderError(
                f"{self.id}: no value for {key} from {environment.describe()} — "
                f"add it to the vault, or set enabled: false in config/pm.yaml"
            )
        return value

    def _token(self) -> str:
        """The API token, from the vault by key name. Cached for the instance."""
        if self._token_cache is not None:
            return self._token_cache
        key = (self._config.token_env or "").strip()
        if not key:
            raise ProviderError(
                f"{self.id}: no token_env in config/pm.yaml, so there is no key to resolve"
            )
        self._token_cache = self._issued(key)
        return self._token_cache

    def _email(self) -> str:
        """The account email, from the vault by key name. Cached for the instance."""
        if self._email_cache is not None:
            return self._email_cache
        self._email_cache = self._issued(self._email_env)
        return self._email_cache

    def _headers(self) -> dict[str, str]:
        # Jira takes the pair as HTTP Basic: base64("email:token").
        pair = f"{self._email()}:{self._token()}".encode()
        return {
            "Authorization": "Basic " + base64.b64encode(pair).decode("ascii"),
            "Accept": "application/json",
        }

    def _secrets(self) -> tuple[str, ...]:
        """Every value that must not survive into a raised message, longest first."""
        found = {
            value
            for value in (self._email_cache, self._token_cache)
            if value
        }
        return tuple(sorted(found, key=len, reverse=True))

    def _scrub(self, exc: ProviderError) -> ProviderError:
        """``exc`` with the resolved credential removed from its message.

        The shared scrubber cannot do this one: it only recognises the values it
        was handed as headers, and Basic auth hands it base64, not the token.
        """
        message = str(exc)
        for secret in self._secrets():
            message = message.replace(secret, "***")
        return ProviderError(message)

    # ── transport ──────────────────────────────────────────────────────────

    def _transport(
        self, url: str, *, operation: str, method: str = "GET", body: Any = None
    ) -> Any:
        """One request, with the credential scrubbed from any failure it raises."""
        headers = self._headers()
        try:
            return pm_http.request_json(
                url,
                method=method,
                headers=headers,
                body=body,
                timeout=self._timeout,
                source=self.id,
                operation=operation,
            )
        except ProviderError as exc:
            raise self._scrub(exc) from None

    def _read(self, url: str, *, operation: str, missing: str) -> Any:
        """One read where a 404 means "no such issue", not "the source failed".

        ``get`` and ``comments`` share it, so an unknown key is a ``KeyError``
        from both rather than from one of them — the protocol's ``get`` contract
        and the thing Linear's ``comments`` already does.
        """
        try:
            return self._transport(url, operation=operation)
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                raise KeyError(missing) from None
            raise

    def _url(self, path: str, **params: Any) -> str:
        query = urllib.parse.urlencode(
            {key: value for key, value in params.items() if value is not None}
        )
        return f"{self._api}{path}" + (f"?{query}" if query else "")

    def _browse(self, key: str) -> str:
        return f"{self._site}/browse/{urllib.parse.quote(key)}"

    # ── mapping ────────────────────────────────────────────────────────────

    def _locate(self, uid: str) -> str:
        """``uid`` → the Jira issue key. Raises ``KeyError`` when foreign.

        The project is read from the uid rather than from the config, so a uid
        stays resolvable after a source is repointed at another project — and a
        uid naming a project this source does not serve is a plain miss, which
        is what the protocol says a ``get`` on an unknown issue is.
        """
        provider_id, key = split_uid(uid)
        if provider_id != self.id:
            raise KeyError(f"{uid} is not a {self.id} issue")
        if not _ISSUE_KEY.fullmatch(key):
            raise KeyError(
                f"{uid} is not a Jira issue reference (want '<PROJECT>-<number>')"
            )
        return key

    def _issue(self, node: Mapping[str, Any]) -> Issue:
        fields = _mapping(node.get("fields"))
        status = _mapping(fields.get("status"))
        priority = _mapping(fields.get("priority"))
        assignee = _mapping(fields.get("assignee"))
        project = _mapping(fields.get("project"))
        parent = _mapping(fields.get("parent"))
        key = str(node.get("key") or node.get("id") or "")
        priority_name = str(priority.get("name") or "") or None
        return Issue(
            identity=ProviderIdentity(
                provider=self.id,
                key=key,
                project=str(
                    project.get("key") or project.get("name") or self._project
                ) or None,
                url=self._browse(key) if key else None,
            ),
            title=str(fields.get("summary") or ""),
            state=_status_state(status),
            native_state=_native_state(status),
            description=adf_to_markdown(fields.get("description")),
            priority=_priority(priority),
            native_priority=_native_priority(priority),
            labels=tuple(str(label) for label in fields.get("labels") or []),
            assignee=_display_name(assignee) or None,
            # Jira may carry a parent link; the model has nowhere to put one yet,
            # so it is recorded in `native` rather than normalized into a uid.
            parent_uid=None,
            created_at=fields.get("created"),
            updated_at=fields.get("updated"),
            blocked_reason=None,
            native={
                "id": node.get("id"),
                "key": key,
                "status": str(status.get("name") or "") or None,
                "status_category": str(_mapping(status.get("statusCategory")).get("key") or "")
                or None,
                "priority_name": priority_name,
                "project": str(project.get("key") or "") or None,
                "parent": str(parent.get("key") or "") or None,
            },
        )

    # ── reads ──────────────────────────────────────────────────────────────

    def _jql(self) -> str:
        """This project's issues, newest first.

        The project clause is what keeps one source to one project's worth of
        work; everything else is narrowed on the returned page (see the module
        docstring). ``ORDER BY`` makes "most useful first" mean "most recently
        touched", which is what a board wants at the top.
        """
        project = self._project.replace('"', '\\"')
        return f'project = "{project}" ORDER BY updated DESC'

    @staticmethod
    def _matches(issue: Issue, query: Query, wanted: frozenset[IssueState]) -> bool:
        """Whether one mapped issue satisfies the filters narrowed here.

        The state match restates :data:`CATEGORIES_BY_STATE` so the invariant —
        a returned page holds only matching issues — is local to this file and
        does not depend on the remote query grammar. Labels are an *all of*
        match, so a ticket has to carry every label that was asked for.
        """
        if wanted and issue.state not in wanted:
            return False
        if query.priorities and issue.priority not in query.priorities:
            return False
        if query.labels and not set(query.labels) <= set(issue.labels):
            return False
        if query.assignee and issue.assignee != query.assignee:
            return False
        return True

    def list_issues(self, query: Query) -> Page:
        """A project's issues, filtered by what this adapter can express.

        Two filters are refused rather than half-honoured: text search (this
        adapter reads a project, not a search index — the mirror's ``LIKE`` is
        what a board searches) and project (one Jira source serves one project,
        so a project filter would be a second scope on top of the JQL clause).
        """
        if query.text.strip():
            raise Unsupported(self.id, "server-side text search (the mirror searches)")
        if query.project:
            raise Unsupported(self.id, "project filtering (one Jira source per project)")

        wanted = _requested_states(query.states)
        if wanted and not any(CATEGORIES_BY_STATE.get(state) for state in wanted):
            # Every state asked for is one this source cannot be in. Asking Jira
            # for the empty set would match everything; nothing is the truthful
            # answer.
            return Page()
        limit = max(1, min(int(query.limit), MAX_PER_PAGE))
        payload = self._transport(
            f"{self._api}/search/jql",
            operation="list issues",
            method="POST",
            body={
                "jql": self._jql(),
                "maxResults": limit,
                "fields": list(FIELDS),
                # Absent on the first page rather than null: the token is
                # opaque, and only a token Jira issued is ever sent back.
                **({"nextPageToken": query.cursor} if query.cursor else {}),
            },
        )
        envelope = _mapping(payload)
        nodes = envelope.get("issues")
        nodes = nodes if isinstance(nodes, Sequence) and not isinstance(nodes, (str, bytes)) else []
        issues = tuple(
            self._issue(node) for node in nodes if isinstance(node, Mapping)
        )
        # The enhanced search endpoint answers with an opaque token and a flag
        # rather than a count; the token is handed back verbatim on the next
        # read, which is the whole contract of an opaque cursor.
        has_more = not bool(envelope.get("isLast", True))
        token = envelope.get("nextPageToken")
        narrowed = tuple(issue for issue in issues if self._matches(issue, query, wanted))
        return Page(
            issues=narrowed,
            has_more=has_more,
            cursor=str(token) if has_more and token else None,
        )

    def get(self, uid: str) -> Issue:
        key = self._locate(uid)
        payload = self._read(
            self._url(f"/issue/{urllib.parse.quote(key, safe='')}", fields=",".join(FIELDS)),
            operation=f"get {key}",
            missing=f"no Jira issue {key}",
        )
        node = _mapping(payload)
        if not node.get("key") and not node.get("id"):
            raise KeyError(f"no Jira issue {key}")
        return self._issue(node)

    def comments(self, uid: str) -> Sequence[Comment]:
        """The issue's comments, oldest first — Jira's own order.

        One page: a discussion longer than Jira's ceiling is a discussion the
        board shows the beginning of, and paging a detail view is a later slice.
        The ceiling is Jira's own, so nothing is dropped quietly below it.
        """
        key = self._locate(uid)
        payload = self._read(
            self._url(f"/issue/{urllib.parse.quote(key, safe='')}/comment", maxResults=MAX_PER_PAGE),
            operation=f"comments for {key}",
            missing=f"no Jira issue {key}",
        )
        envelope = _mapping(payload)
        nodes = envelope.get("comments")
        nodes = nodes if isinstance(nodes, Sequence) and not isinstance(nodes, (str, bytes)) else []
        return tuple(
            Comment(
                issue_uid=uid,
                author=_display_name(item.get("author")),
                # A v3 comment body is ADF, exactly like the description.
                body=adf_to_markdown(item.get("body")),
                created_at=item.get("created"),
                url=self._browse(key),
                native_id=str(item["id"]) if item.get("id") is not None else None,
            )
            for item in nodes
            if isinstance(item, Mapping)
        )

    # ── writes ─────────────────────────────────────────────────────────────

    def _issue_path(self, key: str, *suffix: str) -> str:
        """The URL for one issue and any path below it, the key quoted first.

        The key comes from ``_locate`` and is already validated, but it is
        quoted again here so a value that ever reaches this without passing
        through ``_locate`` still cannot re-aim an authenticated request.
        """
        path = "/issue/" + urllib.parse.quote(key, safe="")
        return self._url(path + "".join(f"/{segment}" for segment in suffix))

    def comment(self, uid: str, body: str) -> Comment:
        """Post ``body`` on the issue: ``POST /issue/{key}/comment``.

        The body is sent as a minimal ADF document (see
        :func:`markdown_to_adf`), because on v3 a comment is a document, not a
        string. The key passes the same ``_locate`` validation every read uses,
        so a malformed uid cannot reach the URL.
        """
        key = self._locate(uid)
        payload = self._transport(
            self._issue_path(key, "comment"),
            method="POST",
            operation=f"comment on {key}",
            body={"body": markdown_to_adf(body)},
        )
        node = _mapping(payload)
        # The server echoes the stored document; fall back to what was sent if
        # an install answers without one.
        rendered = adf_to_markdown(node.get("body"))
        return Comment(
            issue_uid=uid,
            author=_display_name(node.get("author")),
            body=rendered or str(body),
            created_at=node.get("created"),
            url=self._browse(key),
            native_id=str(node["id"]) if node.get("id") is not None else None,
        )

    def transition(self, uid: str, state: IssueState) -> Issue:
        """Move the issue: read its transitions, pick one, then ``POST`` it.

        Jira has no status *type*, only workspace-defined statuses whose stable
        ``statusCategory.key`` this adapter already maps (see
        :data:`STATE_BY_CATEGORY`). The first transition landing in the
        requested category is taken; a state no transition reaches —
        ``IN_REVIEW``, ``BLOCKED``, ``CANCELED`` — raises :class:`Unsupported`
        rather than moving the issue somewhere it was not asked to go.

        The ``POST`` answers ``204 No Content``, so the issue the protocol
        promises is read back once afterwards.
        """
        wanted = IssueState(state)
        key = self._locate(uid)
        payload = self._transport(
            self._issue_path(key, "transitions"),
            operation=f"transitions for {key}",
        )
        transitions = _mapping(payload).get("transitions")
        transitions = (
            transitions
            if isinstance(transitions, Sequence) and not isinstance(transitions, (str, bytes))
            else []
        )
        chosen = next(
            (
                item
                for item in transitions
                if isinstance(item, Mapping)
                and item.get("id") is not None
                and _status_state(_mapping(item.get("to"))) is wanted
            ),
            None,
        )
        if chosen is None:
            raise Unsupported(
                self.id,
                f"a {wanted.value} status (no Jira transition lands in that category)",
            )
        self._transport(
            self._issue_path(key, "transitions"),
            method="POST",
            operation=f"transition {key} to {wanted.value}",
            body={"transition": {"id": str(chosen["id"])}},
        )
        return self.get(uid)

    # ── writes: still not supported ────────────────────────────────────────

    def create(self, draft: Draft) -> Issue:
        raise Unsupported(self.id, "create")

    def update(self, uid: str, patch: Patch) -> Issue:
        raise Unsupported(self.id, "update")

    def link(self, from_uid: str, to_uid: str, kind: LinkKind) -> Link:
        raise Unsupported(
            self.id, "links (Jira issue links are not modelled by this adapter)"
        )


def provider(config: SourceConfig, **kwargs: Any) -> JiraProvider:
    """One instance, for the registration loop in ``providers/__init__.py``."""
    return JiraProvider(config, **kwargs)
