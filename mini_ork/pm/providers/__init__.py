"""Built-in PM sources.

Importing this package registers them, and that import-for-side-effect is the
single line a new source costs beyond its own module: the registry is then the
one list every surface reads. Keep registrations here rather than in
``pm/__init__.py`` so that "what sources exist" has exactly one answer.

Adding a source: write ``providers/<name>.py`` with a class, import it here,
call :func:`mini_ork.pm.registry.register_provider` on an instance.

Two kinds of source, and the split is the point:

* **Unconditional** — ``local``, mini-ork's own epics. No config, no network,
  no credential, so it is always registered and the registry is never empty.
* **Configured** — everything else, listed in ``_CONFIGURED`` and built only
  when ``config/pm.yaml`` declares it *and* the adapter accepts the
  declaration. A source with no token key, or with a required setting missing,
  is skipped and the reason is kept (see :func:`skipped_sources`) rather than
  raised: ``import mini_ork.pm`` must not fail because a developer has not set
  up a token yet, and the default checkout has no such file at all.

A skipped source is *not* a source that silently returns nothing — its adapter
never exists, so a caller that asks for it by id gets ``UnknownProvider``, and
one that resolves it through the vault gets a ``ProviderError`` naming the key.
Skipping decides what the catalogue lists; it never decides that a failure is
acceptable.
"""

from __future__ import annotations

from mini_ork.pm import config as pm_config
from mini_ork.pm.providers import github, jira, linear, local
from mini_ork.pm.registry import register_provider

#: mini-ork's own epics. The one source that needs no config, no network and no
#: credential, which is why it is registered first and unconditionally.
_LOCAL = local.provider()
register_provider(_LOCAL)
LOCAL_ID = _LOCAL.id

#: Adapters that need a ``SourceConfig``, keyed by adapter name. A source's id is
#: its registry id, so one adapter serves many sources: ``github`` and
#: ``github-core`` are two repositories, ``linear`` and ``linear-ops`` two teams.
#: See :func:`_factory_for` for how an id finds its adapter.
_CONFIGURED = {
    "github": github.GithubProvider,
    "linear": linear.LinearProvider,
    "jira": jira.JiraProvider,
}

_SKIPPED: list[tuple[str, str]] = []


def skipped_sources() -> tuple[tuple[str, str], ...]:
    """``(source id, why)`` for every configured source that could not be built.

    Kept so ``mini-ork pm sources`` can say why a source the config declares is
    missing from the catalogue, instead of leaving a person to compare two
    lists.
    """
    return tuple(_SKIPPED)


def adapter_for(source_id: str):
    """The adapter class that serves ``source_id``, or ``None``.

    The one answer to "which adapters this build has", read by a caller that
    cannot ask the registry: ``mini-ork pm add`` builds a source from a config
    the project has not written yet, so there is nothing registered to look up.
    ``local`` is absent on purpose — it is unconditional, not configured.
    """
    return _CONFIGURED.get(str(source_id))


def _factory_for(source: pm_config.SourceConfig) -> tuple[object | None, str]:
    """The adapter that builds ``source``, and the adapter name it was looked up by.

    An explicit ``adapter:`` setting wins, so any id can name its tool. Otherwise
    the id itself (``github``), and then the id's prefix before the first ``-``
    (``github-core`` → ``github``). Exact match only used to apply, so a second
    repository or team — the shape config/pm.yaml documents — was silently
    never registered.
    """
    named = str(source.settings.get("adapter") or "").strip()
    if named:
        return _CONFIGURED.get(named), named
    if source.id in _CONFIGURED:
        return _CONFIGURED[source.id], source.id
    prefix = source.id.split("-", 1)[0]
    return _CONFIGURED.get(prefix), prefix


def _register_configured(configuration: pm_config.PmConfig | None = None) -> None:
    if configuration is not None:
        _register_sources(configuration)
        return
    try:
        configuration = pm_config.load_config()
    except pm_config.PmConfigError as exc:
        # A bad config is not a reason for `import mini_ork.pm` to fail: the
        # local source still works, and the mistake surfaces with the config's
        # own message the moment anyone asks about sources.
        _SKIPPED.append(("", str(exc)))
        return
    _register_sources(configuration)


def _register_sources(configuration: pm_config.PmConfig) -> None:
    for source in configuration.enabled():
        if source.id == LOCAL_ID:
            continue  # registered above, unconditionally
        factory, adapter = _factory_for(source)
        if factory is None:
            # Declared and enabled with no adapter in this build — a source
            # from a newer version, or a name that never had one. Recorded
            # rather than raised: a bad name must not break `import mini_ork.pm`,
            # but it must not vanish either.
            _SKIPPED.append((source.id, f"no {adapter!r} adapter in this build "
                             f"(known: {', '.join(sorted(_CONFIGURED))})"))
            continue
        try:
            register_provider(factory(source))
        except pm_config.PmConfigError as exc:
            _SKIPPED.append((source.id, str(exc)))


_register_configured()

__all__ = ["adapter_for", "github", "jira", "linear", "local", "skipped_sources"]
