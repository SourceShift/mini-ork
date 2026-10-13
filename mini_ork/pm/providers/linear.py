"""Linear, seen through the PM model.

Linear is the second network source and the first that speaks GraphQL: one
endpoint (``https://api.linear.app/graphql``) takes every read as a query
document plus variables, and answers ``{"data": ...}`` or ``{"errors": [...]}``.
That shape decides three things here:

* **One request helper, one header.** Transport goes through
  :mod:`mini_ork.pm.http` exactly as GitHub's does — the runtime dependency list
  stays two entries long — and the single header Linear wants is
  ``Authorization``. A personal API key is sent as-is; Linear does not use a
  ``Bearer`` prefix for one.

* **An error can arrive with HTTP 200.** A GraphQL gateway answers a bad query
  with an ``errors`` list beside an absent ``data``, so a non-empty ``errors``
  is raised as :class:`~mini_ork.pm.provider.ProviderError` even though the
  transport succeeded. The credential is resolved through the vault by key name
  and never written down here: the shared helper scrubs it from any message it
  builds, and nothing in this file puts it back.

* **A cursor is a token Linear minted.** ``pageInfo.endCursor`` is handed back
  verbatim as :attr:`~mini_ork.pm.provider.Page.cursor` and returned as
  ``after`` on the next read. Nothing parses it — that is the whole contract of
  an opaque cursor, and a page is followed by handing the token back.

Three places where Linear and the model genuinely disagree, resolved here and
nowhere else:

* **Priority numbers run the other way.** Linear counts urgency *down* — 1
  urgent, 4 low — while :class:`~mini_ork.pm.model.Priority` counts up. The
  inversion is written down once, in :meth:`Priority.from_linear`, and this
  adapter calls it rather than restating the table.

* **Workflow states are workspace-defined.** A Linear state carries a stable
  ``type`` (``backlog``, ``unstarted``, ``started``, ``completed``,
  ``canceled``) and a workspace-chosen ``name`` ("In Progress", "Icebox"). The
  type maps onto :class:`~mini_ork.pm.model.IssueState`; the name survives
  verbatim in ``native_state``, so a mapping that turns out to be wrong still
  shows Linear's own word. A state whose type maps to nothing — the ``triage``
  type today, anything Linear adds later — lands in ``BACKLOG``, the one state
  that claims nothing about the work.

* **Some filters are narrowed here, not sent.** The read is scoped server-side
  by team and by workflow-state *type*; the label, assignee and priority
  matches are applied to the returned page. That is deliberate: it pins the
  filter semantics in this file — where they are tested — instead of in a
  remote filter grammar, and a filter is never silently dropped. A narrowed
  page can therefore come back shorter than ``limit`` with more to fetch,
  which :attr:`Page.has_more` covers because it follows Linear's own
  ``pageInfo`` rather than counting rows.

**Read-only.** Linear's GraphQL API can create, update, transition and comment
on an issue, and a later slice will do exactly that, gated by
``Capabilities``. Declaring those capabilities now would mean carrying code
with no caller; declaring them false is the honest statement of what this file
does today.
"""

from __future__ import annotations

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

#: The public GraphQL endpoint. A self-hosted or proxied install names its own
#: in ``api_url``.
API_URL = "https://api.linear.app/graphql"

#: Linear's own ceiling on a connection's ``first``. Asking for more is clamped
#: by the API, which would make ``has_more`` a lie, so it is clamped here.
MAX_PER_PAGE = 100

#: Linear's workflow-state ``type`` → the normalized state it means. The type is
#: stable across workspaces; the ``name`` beside it is not, which is why the
#: mapping keys on the type and keeps the name only as ``native_state``.
#:
#: ``triage`` is a real Linear type for teams that triage arrivals: an issue
#: there has not been promised to anyone yet, so it reads ``BACKLOG`` exactly as
#: an unrecognized type does — the mapping says so explicitly rather than
#: relying on the fallback.
STATE_BY_TYPE: Mapping[str, IssueState] = {
    "backlog": IssueState.BACKLOG,
    "triage": IssueState.BACKLOG,
    "unstarted": IssueState.TODO,
    "started": IssueState.IN_PROGRESS,
    "completed": IssueState.DONE,
    "canceled": IssueState.CANCELED,
}

#: The inverse of :data:`STATE_BY_TYPE`: the workflow-state types that normalize
#: to each state. A state Linear cannot produce — ``IN_REVIEW``, ``BLOCKED`` —
#: inverts to no types at all, which is what makes a filter for it ask for
#: nothing rather than for everything.
TYPES_BY_STATE: Mapping[IssueState, tuple[str, ...]] = {
    state: tuple(sorted(kind for kind, value in STATE_BY_TYPE.items() if value is state))
    for state in IssueState
}

_ISSUES_QUERY = """
query Issues($first: Int!, $after: String, $filter: IssueFilter) {
  issues(first: $first, after: $after, filter: $filter) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      identifier
      title
      description
      url
      priority
      priorityLabel
      createdAt
      updatedAt
      state { name type }
      assignee { name displayName }
      labels { nodes { name } }
      team { key name }
    }
  }
}
"""

_COMMENTS_QUERY = """
query Comments($first: Int!, $commentFirst: Int!, $filter: IssueFilter) {
  issues(first: $first, filter: $filter) {
    nodes {
      comments(first: $commentFirst) {
        nodes { id body createdAt url user { name displayName } }
      }
    }
  }
}
"""


def _state(state: Mapping[str, Any]) -> IssueState:
    """The normalized state for one workflow-state payload. See the docstring."""
    return STATE_BY_TYPE.get(str(state.get("type") or ""), IssueState.BACKLOG)


def _native_state(state: Mapping[str, Any]) -> str:
    """Linear's own word for the state: the workspace's ``name``, else the type.

    Kept beside the normalized value so "In review" or a renamed column still
    reads as itself when the type alone would lose it.
    """
    name = str(state.get("name") or "").strip()
    return name or str(state.get("type") or "")


def _requested_states(states: Sequence[IssueState]) -> frozenset[IssueState]:
    """Coerce to enum members.

    ``IssueState`` is a ``StrEnum``, whose members hash by *name*, so a plain
    ``"done"`` string in a set would silently match nothing. This is the same
    trap ``local.link`` and ``github`` both document.
    """
    return frozenset(IssueState(state) for state in states)


def _first_node(data: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """The first node of a connection in a GraphQL ``data`` block, if any."""
    connection = data.get(key)
    if not isinstance(connection, Mapping):
        return None
    nodes = connection.get("nodes")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        return None
    for node in nodes:
        if isinstance(node, Mapping):
            return node
    return None


def _person_name(person: Any) -> str:
    """A Linear user's own name, from its ``name``, else its ``displayName``."""
    if not isinstance(person, Mapping):
        return ""
    return str(person.get("name") or person.get("displayName") or "")


def _error_text(errors: Any) -> str:
    """A GraphQL ``errors`` value as one readable line."""
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
        messages = [
            str(item.get("message") or "")
            for item in errors
            if isinstance(item, Mapping)
        ]
        joined = "; ".join(message for message in messages if message)
        return joined or "unknown GraphQL error"
    return str(errors) or "unknown GraphQL error"


class LinearProvider:
    """One Linear workspace (or one team within it) as a PM source.

    ``team`` in ``config/pm.yaml`` names the team key the source reads. The
    registry id is the source id, so a second team is a second entry
    (``linear-ops``) with its own ``team`` and its own ``token_env`` if the keys
    differ, and the uids stay unambiguous: ``linear:ENG-123``.
    """

    label = "Linear"
    capabilities = Capabilities(
        # Reads only, today. See the module docstring.
        create=False,
        update=False,
        transition=False,
        comment=False,
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
        (team,) = config.require("team")
        self._team = str(team)
        if not (config.token_env or "").strip():
            # Refused at construction rather than at the first call: a source
            # that means to authenticate and names no key cannot work at all,
            # and finding that out here names the setting instead of failing
            # one HTTP request later with no explanation.
            raise PmConfigError(
                f"pm source {config.id!r} needs token_env naming the vault key that "
                "holds its Linear API key"
            )
        self._api = str(config.settings.get("api_url") or API_URL)
        self._token_cache: str | None = None

    # ── credential ─────────────────────────────────────────────────────────

    def _token(self) -> str:
        """The API key, from the vault by key name. Raises; never returns ``""``.

        Cached for the life of the instance, so a board that lists many issues
        resolves the key once.
        """
        if self._token_cache is not None:
            return self._token_cache
        key = (self._config.token_env or "").strip()
        if not key:
            raise ProviderError(
                f"{self.id}: no token_env in config/pm.yaml, so there is no key to resolve"
            )
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
                "add it to the vault, or set enabled: false in config/pm.yaml"
            )
        self._token_cache = value
        return value

    def _headers(self) -> dict[str, str]:
        # Linear takes a personal API key as the header value itself.
        return {"Authorization": self._token(), "Accept": "application/json"}

    # ── transport ──────────────────────────────────────────────────────────

    def _graphql(
        self, query: str, variables: Mapping[str, Any], *, operation: str
    ) -> Mapping[str, Any]:
        """One GraphQL read, with the shared helper carrying the credential."""
        payload = pm_http.request_json(
            self._api,
            method="POST",
            headers=self._headers(),
            body={"query": query, "variables": dict(variables)},
            timeout=self._timeout,
            source=self.id,
            operation=operation,
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(f"{self.id}: {operation} returned no GraphQL envelope")
        errors = payload.get("errors")
        if errors:
            # A gateway reports a rejected read with HTTP 200 and an `errors`
            # list, so the transport succeeding is not the read succeeding.
            raise ProviderError(f"{self.id} {operation} failed: {_error_text(errors)}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ProviderError(f"{self.id}: {operation} returned no data")
        return data

    def _locate(self, uid: str) -> tuple[str, int]:
        """``uid`` → ``(team key, number)``. Raises ``KeyError`` when foreign.

        The team is read from the uid rather than from the config, so a uid
        stays resolvable after a source is repointed at another team — and a
        uid naming a team this source does not serve is a plain miss, which is
        what the protocol says a ``get`` on an unknown issue is.
        """
        provider_id, key = split_uid(uid)
        if provider_id != self.id:
            raise KeyError(f"{uid} is not a {self.id} issue")
        team_key, _, number = key.rpartition("-")
        if not team_key or not number.isdigit():
            raise KeyError(
                f"{uid} is not a Linear issue reference (want '<TEAM>-<number>')"
            )
        return team_key, int(number)

    def _issue(self, node: Mapping[str, Any]) -> Issue:
        state = node.get("state")
        state = state if isinstance(state, Mapping) else {}
        team = node.get("team")
        team = team if isinstance(team, Mapping) else {}
        assignee = node.get("assignee")
        assignee = assignee if isinstance(assignee, Mapping) else {}
        labels = node.get("labels")
        label_nodes = (labels or {}).get("nodes") if isinstance(labels, Mapping) else None
        identifier = str(node.get("identifier") or node.get("id") or "")
        priority = node.get("priority")
        return Issue(
            identity=ProviderIdentity(
                provider=self.id,
                key=identifier,
                project=str(team.get("key") or team.get("name") or "") or None,
                url=node.get("url"),
            ),
            title=str(node.get("title") or ""),
            state=_state(state),
            native_state=_native_state(state),
            description=str(node.get("description") or ""),
            # The inversion lives in the model. See the module docstring.
            priority=Priority.from_linear(priority),
            native_priority=(
                int(priority)
                if isinstance(priority, (int, float)) and not isinstance(priority, bool)
                else None
            ),
            labels=tuple(
                str(label["name"])
                for label in label_nodes or []
                if isinstance(label, Mapping) and label.get("name")
            ),
            # Linear's `name` is the canonical field its own payloads carry;
            # `displayName` is the fallback for an actor that has only one.
            assignee=_person_name(assignee) or None,
            parent_uid=None,
            created_at=node.get("createdAt"),
            updated_at=node.get("updatedAt"),
            blocked_reason=None,
            native={
                "id": node.get("id"),
                "identifier": identifier,
                "priority_label": node.get("priorityLabel"),
                "state_type": str(state.get("type") or ""),
                "team": str(team.get("key") or "") or None,
            },
        )

    # ── reads ──────────────────────────────────────────────────────────────

    def _filter(self, wanted: frozenset[IssueState]) -> dict[str, Any]:
        """The server-side filter: this team's issues, optionally by state type.

        The team clause is what keeps one source to one workspace's worth of
        work; the state clause is emitted only when a caller asked for states,
        and then only for the types that can normalize to them.
        """
        clauses: dict[str, Any] = {"team": {"key": {"eq": self._team}}}
        if wanted:
            types = sorted(
                {kind for state in wanted for kind in TYPES_BY_STATE.get(state, ())}
            )
            clauses["state"] = {"type": {"in": types}}
        return clauses

    @staticmethod
    def _matches(issue: Issue, query: Query, wanted: frozenset[IssueState]) -> bool:
        """Whether one mapped issue satisfies the filters narrowed here.

        The state match restates the server's own clause so the invariant — a
        returned page holds only matching issues — is local to this file and
        does not depend on the remote filter grammar. Labels are an *all of*
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
        """A team's issues, filtered by what this adapter can express.

        Two filters are refused rather than half-honoured: text search (this
        adapter reads a team, not a search index — the mirror's ``LIKE`` is
        what a board searches) and project (one Linear source serves one team,
        so a project filter would need a name-to-id lookup this slice does not
        carry).
        """
        if query.text.strip():
            raise Unsupported(self.id, "server-side text search (the mirror searches)")
        if query.project:
            raise Unsupported(self.id, "project filtering (one Linear source per team)")

        wanted = _requested_states(query.states)
        if wanted and not any(TYPES_BY_STATE.get(state) for state in wanted):
            # Every state asked for (IN_REVIEW, BLOCKED, …) is one this source
            # cannot be in. Asking Linear for the empty set would match
            # everything; returning nothing is the truthful answer.
            return Page()
        limit = max(1, min(int(query.limit), MAX_PER_PAGE))
        data = self._graphql(
            _ISSUES_QUERY,
            {
                "first": limit,
                "after": query.cursor or None,
                "filter": self._filter(wanted),
            },
            operation="list issues",
        )
        connection = data.get("issues")
        connection = connection if isinstance(connection, Mapping) else {}
        nodes = connection.get("nodes")
        nodes = nodes if isinstance(nodes, Sequence) and not isinstance(nodes, (str, bytes)) else []
        issues = tuple(
            self._issue(node)
            for node in nodes
            if isinstance(node, Mapping)
        )
        page_info = connection.get("pageInfo")
        page_info = page_info if isinstance(page_info, Mapping) else {}
        has_more = bool(page_info.get("hasNextPage"))
        end_cursor = page_info.get("endCursor")
        narrowed = tuple(
            issue for issue in issues if self._matches(issue, query, wanted)
        )
        return Page(
            issues=narrowed,
            has_more=has_more,
            # The token Linear minted, handed back unused for the next read.
            cursor=str(end_cursor) if has_more and end_cursor else None,
        )

    def get(self, uid: str) -> Issue:
        team_key, number = self._locate(uid)
        data = self._graphql(
            _ISSUES_QUERY,
            {
                "first": 1,
                "after": None,
                "filter": {"team": {"key": {"eq": team_key}}, "number": {"eq": number}},
            },
            operation=f"get {team_key}-{number}",
        )
        node = _first_node(data, "issues")
        if node is None:
            raise KeyError(f"no Linear issue {team_key}-{number}")
        return self._issue(node)

    def comments(self, uid: str) -> Sequence[Comment]:
        """The issue's comments, oldest first — Linear's own order.

        One page: a discussion longer than the connection's ceiling is a
        discussion the board shows the beginning of, and paging a detail view
        is a later slice. The count is Linear's ceiling, so nothing is dropped
        quietly below it.
        """
        team_key, number = self._locate(uid)
        data = self._graphql(
            _COMMENTS_QUERY,
            {
                "first": 1,
                "commentFirst": MAX_PER_PAGE,
                "filter": {"team": {"key": {"eq": team_key}}, "number": {"eq": number}},
            },
            operation=f"comments for {team_key}-{number}",
        )
        node = _first_node(data, "issues")
        if node is None:
            raise KeyError(f"no Linear issue {team_key}-{number}")
        comments = node.get("comments")
        comment_nodes = (comments or {}).get("nodes") if isinstance(comments, Mapping) else None
        return tuple(
            Comment(
                issue_uid=uid,
                author=_person_name(item.get("user")),
                body=str(item.get("body") or ""),
                created_at=item.get("createdAt"),
                url=item.get("url"),
                native_id=str(item["id"]) if item.get("id") is not None else None,
            )
            for item in comment_nodes or []
            if isinstance(item, Mapping)
        )

    # ── writes: not supported yet ──────────────────────────────────────────

    def create(self, draft: Draft) -> Issue:
        raise Unsupported(self.id, "create")

    def update(self, uid: str, patch: Patch) -> Issue:
        raise Unsupported(self.id, "update")

    def transition(self, uid: str, state: IssueState) -> Issue:
        raise Unsupported(self.id, "transition")

    def comment(self, uid: str, body: str) -> Comment:
        raise Unsupported(self.id, "comment")

    def link(self, from_uid: str, to_uid: str, kind: LinkKind) -> Link:
        raise Unsupported(
            self.id, "links (Linear relations are not modelled by this adapter)"
        )


def provider(config: SourceConfig, **kwargs: Any) -> LinearProvider:
    """One instance, for the registration loop in ``providers/__init__.py``."""
    return LinearProvider(config, **kwargs)
