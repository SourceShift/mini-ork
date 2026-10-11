"""``config/pm.yaml`` — which PM sources this project has, and the vault key each one signs in with.

The file holds *names*, never values. ``token_env`` is the name of the key the
vault resolves — ``env``, ``secrets.local.sh``, sops, ``pass``, ``infisical``,
OpenBao, whichever backend ``config/vault.yaml`` declares (see
``mini_ork/dispatch/vault.py``). Nothing in this module reads a secret and
nothing in it can leak one: an adapter asks the vault for a key *by name*, so
the same config file works unchanged in both delivery tiers — a 0600 file next
to the project on a laptop, a real secret store on a shared deployment.

Resolution order is ``config/providers.yaml``'s, for the same reason it is
documented there: a project-local override beats the checkout's example.

  1. ``$MINI_ORK_PM``
  2. ``$MINI_ORK_HOME/config/pm.yaml``
  3. ``<repo>/config/pm.yaml``

Fail closed on the two ways a source can be declared but unusable: an enabled
source with a blank key name, and a source that a caller requires but that is
disabled. Both raise :class:`PmConfigError` naming the source and the setting —
an empty string is never read as "no credential needed".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_HOME = ".mini-ork"
ENV_OVERRIDE = "MINI_ORK_PM"


class PmConfigError(RuntimeError):
    """The PM config is unusable as written. Names the source and the setting."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One declared source.

    ``settings`` carries whatever the adapter needs beyond the credential —
    Linear's team key, Jira's site and project, mini-ork's database path. The
    config layer does not interpret it; the adapter does, and it fails closed on
    a value it needs and does not find.
    """

    id: str
    enabled: bool = False
    token_env: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)

    def require(self, *keys: str) -> tuple[Any, ...]:
        """The named settings, in order. Raises when one is missing or blank."""
        values = []
        for key in keys:
            value = self.settings.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise PmConfigError(
                    f"pm source {self.id!r} needs {key!r} in config/pm.yaml"
                )
            values.append(value)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class PmConfig:
    """The whole file, plus where it was read from (``None`` = nothing found)."""

    sources: Mapping[str, SourceConfig] = field(default_factory=dict)
    path: Path | None = None

    def enabled(self) -> tuple[SourceConfig, ...]:
        """Sources a caller may use, ordered by id."""
        return tuple(
            source for _, source in sorted(self.sources.items()) if source.enabled
        )

    def source(self, source_id: str) -> SourceConfig:
        """One declared source, enabled or not. Raises when it is not declared."""
        try:
            return self.sources[source_id]
        except KeyError:
            declared = ", ".join(sorted(self.sources)) or "(none)"
            raise PmConfigError(
                f"pm source {source_id!r} is not declared in config/pm.yaml; "
                f"declared: {declared}"
            ) from None

    def require(self, source_id: str) -> SourceConfig:
        """One source that must be usable: declared, enabled, and credentialed.

        A source with no ``token_env`` is fine — mini-ork's own epics need no
        credential — but a source whose ``token_env`` is present and blank is
        not, because that is a config that means to authenticate and cannot.
        """
        source = self.source(source_id)
        if not source.enabled:
            raise PmConfigError(
                f"pm source {source_id!r} is declared but not enabled "
                f"(set enabled: true in {self.path or 'config/pm.yaml'})"
            )
        if source.token_env is not None and not source.token_env.strip():
            raise PmConfigError(
                f"pm source {source_id!r} declares an empty token_env; "
                "name the vault key, or remove the line for an unauthenticated source"
            )
        return source


def repo_root() -> Path:
    """The mini-ork checkout, so the committed example is reachable from a home."""
    from mini_ork.dispatch.providers import mini_ork_root

    return Path(mini_ork_root())


def config_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """The first ``pm.yaml`` that exists, or ``None``. Does not raise on a
    missing file — a project with no PM config is a project with no sources."""
    environment = os.environ if environ is None else environ
    override = environment.get(ENV_OVERRIDE)
    if override:
        return Path(override)
    home = str(environment.get("MINI_ORK_HOME") or DEFAULT_HOME).strip()
    for candidate in (Path(home) / "config" / "pm.yaml", repo_root() / "config" / "pm.yaml"):
        if candidate.is_file():
            return candidate
    return None


def parse_config(document: Any, *, path: Path | None = None) -> PmConfig:
    """A parsed YAML document → :class:`PmConfig`. Pure, so tests need no disk.

    A malformed ``sources`` block raises rather than being read as empty: a
    typo'd indentation would otherwise disable every source and look like a
    project that never configured any.
    """
    if document is None:
        return PmConfig(sources={}, path=path)
    if not isinstance(document, Mapping):
        raise PmConfigError(f"{path or 'pm.yaml'} must be a mapping at the top level")
    raw_sources = document.get("sources") or {}
    if not isinstance(raw_sources, Mapping):
        raise PmConfigError(f"{path or 'pm.yaml'}: 'sources' must be a mapping")

    sources: dict[str, SourceConfig] = {}
    for key, raw in raw_sources.items():
        source_id = str(key)
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise PmConfigError(f"pm source {source_id!r} must be a mapping")
        token_env = raw.get("token_env")
        settings = {
            str(k): v for k, v in raw.items() if k not in ("enabled", "token_env")
        }
        sources[source_id] = SourceConfig(
            id=source_id,
            enabled=bool(raw.get("enabled", False)),
            token_env=None if token_env is None else str(token_env),
            settings=settings,
        )
    return PmConfig(sources=sources, path=path)


def load_config(
    environ: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> PmConfig:
    """Read the config. A missing file is an empty config; a broken one raises."""
    environment = os.environ if environ is None else environ
    named = path is not None or bool(environment.get(ENV_OVERRIDE))
    if path is None:
        path = config_path(environment)
    if path is None:
        return PmConfig(sources={}, path=None)
    if not path.is_file():
        # A path someone asked for by name that is not there is a mistake worth
        # naming; a searched candidate that is absent is just an unconfigured
        # project, which is an ordinary state.
        if named:
            raise PmConfigError(f"pm config {path} does not exist")
        return PmConfig(sources={}, path=None)
    import yaml

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PmConfigError(f"cannot read {path}: {exc}") from exc
    return parse_config(document, path=path)
