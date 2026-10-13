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
from mini_ork.pm.providers import github, linear, local
from mini_ork.pm.registry import register_provider

#: mini-ork's own epics. The one source that needs no config, no network and no
#: credential, which is why it is registered first and unconditionally.
register_provider(local.provider())

#: Adapters that need a ``SourceConfig``, keyed by the id they register under.
#: The bare ``github`` id served a single repository first; a second repository
#: is a second entry (``github-core``) with its own ``owner``/``repo``. A second
#: Linear team is likewise a second entry (``linear-ops``) with its own ``team``.
_CONFIGURED = {
    "github": github.GithubProvider,
    "linear": linear.LinearProvider,
}

_SKIPPED: list[tuple[str, str]] = []


def skipped_sources() -> tuple[tuple[str, str], ...]:
    """``(source id, why)`` for every configured source that could not be built.

    Kept so ``mini-ork pm sources`` can say why a source the config declares is
    missing from the catalogue, instead of leaving a person to compare two
    lists.
    """
    return tuple(_SKIPPED)


def _register_configured() -> None:
    try:
        configuration = pm_config.load_config()
    except pm_config.PmConfigError as exc:
        # A bad config is not a reason for `import mini_ork.pm` to fail: the
        # local source still works, and the mistake surfaces with the config's
        # own message the moment anyone asks about sources.
        _SKIPPED.append(("", str(exc)))
        return
    for source in configuration.enabled():
        factory = _CONFIGURED.get(source.id)
        if factory is None:
            # Declared and enabled with no adapter in this build — a source
            # from a newer version, or a name that never had one. Naming it is
            # the config's job; inventing an error here would break imports.
            continue
        try:
            register_provider(factory(source))
        except pm_config.PmConfigError as exc:
            _SKIPPED.append((source.id, str(exc)))


_register_configured()

__all__ = ["github", "linear", "local", "skipped_sources"]
