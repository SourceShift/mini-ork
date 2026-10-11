"""GitHub Issues, seen through the PM model.

The first source that needs a network and a credential, and the worked example
of the two seams :mod:`mini_ork.pm.config` and
:mod:`mini_ork.dispatch.vault` were built for:

* the source is **declared by name** in ``config/pm.yaml`` — ``owner``, ``repo``
  and ``token_env`` — and nothing in this file reads the file;
* the token is resolved **by key name** through the vault, **at first call**,
  so ``import mini_ork.pm`` neither fails nor does I/O when a developer has not
  configured a token. A name that resolves to nothing raises
  :class:`~mini_ork.pm.provider.ProviderError`; it is never read as an empty
  string, and so never as "unauthenticated is fine".

**Read-only.** GitHub's REST API can open, edit, close and comment on an issue,
and a later slice will do exactly that, gated by ``Capabilities`` so a surface
never offers a write the adapter would refuse. Declaring those capabilities now
would mean carrying code with no caller; declaring them false is the honest
statement of what this file does today, and :class:`Capabilities` exists
precisely so a false capability is visible rather than implied.

Three places where GitHub and the model genuinely disagree, resolved here and
nowhere else:

* **There is no priority.** GitHub issues have no priority field; teams encode
  one as a label (``priority:high``). So every issue reads
  :attr:`~mini_ork.pm.model.Priority.NONE` and the label survives verbatim in
  :attr:`Issue.labels <.pm.model.Issue.labels>` — inventing a label convention
  here would be the adapter guessing at a team's habits. ``priorities`` is
  declared false, so the board does not offer a priority filter this source
  cannot honour.
* **``state`` is two values, plus a reason.** ``open``/``closed`` says whether
  the issue is out of play; ``state_reason`` says *why* it closed. An open
  issue is TODO when someone is assigned to it and BACKLOG when nobody is,
  which is what those two normalized states mean. A closed one is CANCELED for
  ``not_planned``/``duplicate`` and DONE otherwise — including when the reason
  is absent, because GitHub closed issues before it recorded reasons and a
  closed issue must never land in an open column.
* **A pull request is an issue.** ``/issues`` returns both, each PR carrying a
  ``pull_request`` key. They are filtered out of every read: a board of issues
  that is half pull requests is a board a person stops trusting.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Mapping, Sequence

from mini_ork.pm import http as pm_http
from mini_ork.pm.config import PmConfigError, SourceConfig
from mini_ork.pm.model import (
    CLOSED_STATES,
    OPEN_STATES,
    Comment,
    Issue,
    IssueState,
    Link,
    LinkKind,
    Priority,
    ProviderIdentity,
    make_uid,
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

#: The public API. A GitHub Enterprise install names its own in ``api_url``.
API_URL = "https://api.github.com"

#: GitHub's own ceiling on ``per_page``. Asking for more is silently clamped by
#: the API, which would make ``has_more`` a lie, so it is clamped here instead.
MAX_PER_PAGE = 100

#: A closed issue with one of these readers is out of play without having been
#: done. Anything else — including a reason from a newer API version, and no
#: reason at all — reads as DONE.
CLOSED_AS_CANCELED = frozenset({"not_planned", "duplicate"})

#: Everything the card wants that :class:`Issue` does not carry, kept so the
#: detail view can show GitHub's own words.
NATIVE_FIELDS = ("number", "state_reason", "user", "is_pull_request")


def _state(payload: Mapping[str, Any]) -> IssueState:
    """The normalized state for one issue payload. See the module docstring."""
    native = str(payload.get("state") or "")
    if native == "closed":
        reason = str(payload.get("state_reason") or "")
        return IssueState.CANCELED if reason in CLOSED_AS_CANCELED else IssueState.DONE
    if native == "open":
        # An assignee is GitHub's own word for "someone is on this"; nobody
        # assigned means nobody has promised to do it yet.
        return IssueState.TODO if payload.get("assignee") else IssueState.BACKLOG
    # A state from a newer API version. BACKLOG claims nothing about the work,
    # which is the right thing to claim about a word we do not understand.
    return IssueState.BACKLOG


def _requested_states(states: Sequence[IssueState]) -> frozenset[IssueState]:
    """Coerce to enum members.

    ``IssueState`` is a ``StrEnum``, whose members hash by *name*, so a plain
    ``"done"`` string in a set would silently match nothing. This is the same
    trap ``local.link`` documents.
    """
    return frozenset(IssueState(state) for state in states)


def _native_state_parameter(wanted: frozenset[IssueState]) -> str:
    """GitHub's ``state`` query value for the normalized states asked for.

    ``closed`` and ``open`` cannot be asked for separately *within* closed, so
    the reason is filtered after the fetch rather than here.
    """
    if not wanted:
        return "open"  # a board shows open work — the same default `local` uses
    open_wanted = bool(wanted & OPEN_STATES)
    closed_wanted = bool(wanted & CLOSED_STATES)
    if open_wanted and closed_wanted:
        return "all"
    return "closed" if closed_wanted else "open"


def _page_number(cursor: str | None) -> int:
    """The page a cursor names. A cursor that is not a number starts over.

    ``Page.cursor`` is documented as opaque and this adapter's spelling of it
    happens to be a page number; a hand-written value that is not one is a
    caller mistake that costs an extra page, not an error worth raising.
    """
    try:
        number = int(str(cursor))
    except (TypeError, ValueError):
        return 1
    return max(1, number)


class GithubProvider:
    """``owner/repo`` issues as a PM source, one source per repository.

    Multi-repo is multiple source entries — ``github-core``, ``github-api`` —
    each with its own ``owner``/``repo`` and its own ``token_env`` if the
    tokens differ. The registry id is the source id, so the uids stay
    unambiguous: ``github-core:acme/core#12``.
    """

    label = "GitHub Issues"
    capabilities = Capabilities(
        # Reads only, today. See the module docstring.
        create=False,
        update=False,
        transition=False,
        comment=False,
        link=False,
        search=False,
        states=True,
        priorities=False,
        labels=True,
        assignees=True,
        pagination=True,
    )

    def __init__(self, config: SourceConfig, *, timeout: float | None = None) -> None:
        self.id = config.id
        self._config = config
        self._timeout = pm_http.DEFAULT_TIMEOUT if timeout is None else timeout
        self._owner, self._repo = (str(part) for part in config.require("owner", "repo"))
        if not (config.token_env or "").strip():
            # Refused at construction rather than at the first call: a source
            # that means to authenticate and names no key cannot work at all,
            # and finding that out here names the setting instead of failing
            # one HTTP request later with no explanation.
            raise PmConfigError(
                f"pm source {config.id!r} needs token_env naming the vault key that "
                "holds its GitHub token"
            )
        self._api = str(config.settings.get("api_url") or API_URL).rstrip("/")
        self._token_cache: str | None = None

    # ── credential ─────────────────────────────────────────────────────────

    def _token(self) -> str:
        """The token, from the vault by key name. Raises; never returns ``""``.

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
        return {
            "Authorization": f"Bearer {self._token()}",
            # GitHub rejects a request with no User-Agent outright.
            "User-Agent": "mini-ork",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── transport ──────────────────────────────────────────────────────────

    def _get(self, url: str, *, operation: str, parameters: Mapping[str, Any] | None = None) -> Any:
        if parameters:
            query = urllib.parse.urlencode(
                {key: value for key, value in parameters.items() if value not in (None, "")}
            )
            url = f"{url}?{query}"
        return pm_http.request_json(
            url, headers=self._headers(), timeout=self._timeout,
            source=self.id, operation=operation,
        )

    @property
    def _repository_url(self) -> str:
        return f"{self._api}/repos/{self._owner}/{self._repo}"

    def _locate(self, uid: str) -> tuple[str, str]:
        """``uid`` → ``(owner/repo, number)``. Raises ``KeyError`` when foreign.

        The repository is read from the uid rather than from the config, so a
        uid stays resolvable after a source is repointed at another repo — and
        a uid naming a repo this source does not serve is a plain miss, which
        is what the protocol says a ``get`` on an unknown issue is.
        """
        provider_id, key = split_uid(uid)
        if provider_id != self.id:
            raise KeyError(f"{uid} is not a {self.id} issue")
        repository, _, number = key.rpartition("#")
        if "/" not in repository or not number.isdigit():
            raise KeyError(
                f"{uid} is not a GitHub issue reference (want '<owner>/<repo>#<number>')"
            )
        return repository, number

    def _issue(self, payload: Mapping[str, Any]) -> Issue:
        number = int(payload["number"])
        key = f"{self._owner}/{self._repo}#{number}"
        assignee = payload.get("assignee") or {}
        labels = payload.get("labels") or []
        return Issue(
            identity=ProviderIdentity(
                provider=self.id,
                key=key,
                project=f"{self._owner}/{self._repo}",
                url=payload.get("html_url"),
            ),
            title=str(payload.get("title") or ""),
            state=_state(payload),
            native_state=str(payload.get("state") or ""),
            description=str(payload.get("body") or ""),
            # No priority field exists. See the module docstring.
            priority=Priority.NONE,
            native_priority=None,
            labels=tuple(
                str(label["name"])
                for label in labels
                if isinstance(label, Mapping) and label.get("name")
            ),
            assignee=str(assignee.get("login") or "") or None,
            parent_uid=None,
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            blocked_reason=None,
            native={
                "number": number,
                "state_reason": payload.get("state_reason"),
                "user": str((payload.get("user") or {}).get("login") or "") or None,
                "is_pull_request": "pull_request" in payload,
            },
        )

    # ── webhooks ───────────────────────────────────────────────────────────

    def uid_from_webhook(self, payload: Mapping[str, Any]) -> str | None:
        """Which issue a webhook payload names, or ``None`` when it names none.

        Identity only, and that restraint is the point: a webhook is a
        *notification* that something changed, not the data itself. The ticket
        the agent reads is fetched with :meth:`get`, so it is what GitHub serves
        now rather than whatever a POST carried. The one thing that cannot be
        fetched without already knowing it is *which* issue, and that is all
        this reads.

        ``None`` covers every payload that names no workable issue: a push, a
        pull request (which shares the issues API but is not a ticket), a
        deleted or transferred issue, and — importantly — an issue in another
        repository. One source serves one repository, so a payload signed with
        this source's secret must not be able to name someone else's issue.
        """
        if str(payload.get("action") or "") in ("deleted", "transferred"):
            return None
        issue = payload.get("issue")
        if not isinstance(issue, Mapping) or "pull_request" in issue:
            return None
        repository = payload.get("repository")
        full_name = (
            str(repository.get("full_name") or "") if isinstance(repository, Mapping) else ""
        )
        if full_name and full_name != f"{self._owner}/{self._repo}":
            return None
        number = issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        return make_uid(self.id, f"{self._owner}/{self._repo}#{number}")

    # ── reads ──────────────────────────────────────────────────────────────

    def list_issues(self, query: Query) -> Page:
        """Open work, newest first, filtered by what GitHub can filter on.

        Three filters the adapter refuses rather than half-honours: text search
        (this is the search API, a different endpoint with its own rate limit
        and its own result shape — the mirror's ``LIKE`` is what the board
        searches), priority (there is none), and project (one source serves one
        repository, so a project filter would have exactly one right answer).
        """
        if query.text.strip():
            raise Unsupported(self.id, "server-side text search (the mirror searches)")
        if query.priorities:
            raise Unsupported(self.id, "priority filtering (GitHub issues carry no priority)")
        if query.project:
            raise Unsupported(self.id, "project filtering (one GitHub source per repository)")

        wanted = _requested_states(query.states)
        limit = max(1, min(int(query.limit), MAX_PER_PAGE))
        page = _page_number(query.cursor)
        payload = self._get(
            f"{self._repository_url}/issues",
            operation="list issues",
            parameters={
                "state": _native_state_parameter(wanted),
                "per_page": limit,
                "page": page,
                "labels": ",".join(query.labels) if query.labels else None,
                "assignee": query.assignee,
                # The order the API defaults to; stated so it cannot drift.
                "sort": "updated",
                "direction": "desc",
            },
        )
        items = [item for item in payload or [] if isinstance(item, Mapping)]
        issues = [item for item in items if "pull_request" not in item]
        mapped = tuple(self._issue(item) for item in issues)
        if wanted:
            # The reason for a close cannot be asked for server-side, so the
            # page is narrowed here. A page can therefore come back shorter
            # than `limit` with more to fetch, which `has_more` covers.
            mapped = tuple(issue for issue in mapped if issue.state in wanted)
        has_more = len(issues) >= limit
        return Page(
            issues=mapped,
            has_more=has_more,
            cursor=str(page + 1) if has_more else None,
        )

    def get(self, uid: str) -> Issue:
        repository, number = self._locate(uid)
        payload = self._get(
            f"{self._api}/repos/{repository}/issues/{number}",
            operation=f"get {repository}#{number}",
        )
        if not isinstance(payload, Mapping):
            raise KeyError(f"no issue {repository}#{number}")
        if "pull_request" in payload:
            raise KeyError(f"{repository}#{number} is a pull request, not an issue")
        return self._issue(payload)

    def comments(self, uid: str) -> Sequence[Comment]:
        """The issue's comments, oldest first — GitHub's own order.

        One page: a discussion longer than 100 comments is a discussion the
        board shows the beginning of, and paging a detail view is a later
        slice. The count is GitHub's ceiling, so nothing is dropped quietly
        below it.
        """
        repository, number = self._locate(uid)
        payload = self._get(
            f"{self._api}/repos/{repository}/issues/{number}/comments",
            operation=f"comments for {repository}#{number}",
            parameters={"per_page": MAX_PER_PAGE},
        )
        return tuple(
            Comment(
                issue_uid=uid,
                author=str((item.get("user") or {}).get("login") or ""),
                body=str(item.get("body") or ""),
                created_at=item.get("created_at"),
                url=item.get("html_url"),
                native_id=str(item["id"]) if item.get("id") is not None else None,
            )
            for item in payload or []
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
            self.id, "links (GitHub issue relationships live in the issue bodies)"
        )


def provider(config: SourceConfig, **kwargs: Any) -> GithubProvider:
    """One instance, for the registration loop in ``providers/__init__.py``."""
    return GithubProvider(config, **kwargs)
