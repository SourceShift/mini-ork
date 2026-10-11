"""Project management, inside mini-ork.

Issues from mini-ork's own epics and from other tools — Linear, Jira, whatever
a project already uses — through one model, one registry and one mirror table,
so a board can show them side by side without the IDE knowing any tool's API.

Why the adapters live in mini-ork's Python rather than in the IDE:

* **The credential.** ``mini_ork/dispatch/vault.py`` already resolves secrets
  from five backends and reports a protection gap when none of them is really
  protecting anything. Linear and Jira tokens go through that, and the IDE
  stays credential-free; a token in the editor would be a second, weaker
  policy for the same secret.
* **The source of truth.** ``MINI-ORK.md`` says mini-ork owns the state and the
  IDE draws it. Writing to Linear from the editor's process would put a second
  writer next to ``scheduler._settle``.
* **The blast radius.** A provider failure has to leave the local board intact.
  ``store`` mirrors rows, so an unreachable Linear produces a stale card and a
  note, not an empty column.

Reading order for a newcomer: :mod:`mini_ork.pm.model` (the vocabulary),
:mod:`mini_ork.pm.provider` (what a source must implement), then
:mod:`mini_ork.pm.registry`, :mod:`mini_ork.pm.store`,
:mod:`mini_ork.pm.config`, and finally ``pm/providers/local.py`` as the worked
example.
"""

from mini_ork.pm import config, registry, store
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
    Provider,
    ProviderError,
    Query,
    Unsupported,
)

# Importing the built-in sources is what registers them; do it last so a caller
# can import model/provider without paying for the adapters.
import mini_ork.pm.providers  # noqa: E402,F401  (registration side effect)

__all__ = [
    "CLOSED_STATES",
    "OPEN_STATES",
    "Capabilities",
    "Comment",
    "Draft",
    "Issue",
    "IssueState",
    "Link",
    "LinkKind",
    "Page",
    "Patch",
    "Priority",
    "Provider",
    "ProviderError",
    "ProviderIdentity",
    "Query",
    "Unsupported",
    "config",
    "make_uid",
    "registry",
    "split_uid",
    "store",
]
