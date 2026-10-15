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


# ── writing ────────────────────────────────────────────────────────────────
#
# The write side exists so `mini-ork pm add` can declare a source, and it is
# the *inverse* of the reader rather than a second opinion about the format:
# `dump_config` emits exactly the keys `parse_config` folds back, so the pair
# round-trips by construction. A written file still carries no value — only
# the name of the key that holds one.

_WRITTEN_HEADER = """\
# pm.yaml — project-management sources for this project.
#
# Written by `mini-ork pm add` / `mini-ork pm remove`; edit it by hand freely,
# and re-running a command keeps the keys it does not own.
#
# A credential VALUE never appears here. `token_env` names the vault key that
# holds one, resolved through mini_ork/dispatch/vault.py, so the same line
# works whether the key lives in the environment, in a 0600 secrets.local.sh,
# in sops, in `pass`, or in OpenBao. `mini-ork pm sources` shows what is
# declared; `mini-ork vault which <NAME>` names the store that serves a key.
"""


def write_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where a written ``pm.yaml`` goes: ``$MINI_ORK_PM``, else ``$MINI_ORK_HOME``.

    Never ``<repo>/config/pm.yaml``. That file is the committed team default
    and a fallback for *reading* only — a writer that derived its path from
    ``load_config().path`` would rewrite the checked-in example the first time
    anyone ran ``pm add`` with no home set.
    """
    environment = os.environ if environ is None else environ
    override = str(environment.get(ENV_OVERRIDE) or "").strip()
    if override:
        return Path(override)
    home = str(environment.get("MINI_ORK_HOME") or DEFAULT_HOME).strip()
    return Path(home) / "config" / "pm.yaml"


def dump_config(config: PmConfig) -> dict[str, Any]:
    """The YAML document for ``config`` — the inverse of :func:`parse_config`.

    Every source keeps its ``enabled`` flag, its ``token_env`` *name*, and the
    settings the config layer does not interpret. ``parse_config(dump_config(c))``
    reproduces ``c``'s sources, so the round trip is a property of the two
    functions rather than of keeping two spellings in step.
    """
    sources: dict[str, Any] = {}
    for source_id in sorted(config.sources):
        source = config.sources[source_id]
        entry: dict[str, Any] = {"enabled": bool(source.enabled)}
        if source.token_env is not None:
            entry["token_env"] = source.token_env
        for key in sorted(source.settings, key=str):
            entry[str(key)] = source.settings[key]
        sources[str(source_id)] = entry
    return {"sources": sources}


def _existing_top_level(path: Path) -> Mapping[str, Any] | None:
    """The file's own top-level keys, or ``None``. Never raises and never returns a value."""
    if not path.is_file():
        return None
    import yaml

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        # A file we cannot parse is a file whose extra keys we cannot keep.
        # Writing the effective config is still better than refusing to write.
        return None
    return document if isinstance(document, Mapping) else None


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` through a 0600 temp file and ``os.replace``.

    Its own temp file in the target directory, so the replacement is atomic on
    one filesystem and a crash cannot leave a half-written config where a
    reader would find it mid-parse.
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pm-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise PmConfigError(f"cannot write {path}: {exc}") from exc


def write_config(
    config: PmConfig,
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Write ``config`` to ``pm.yaml`` atomically. Returns the path it wrote.

    Top-level keys the dataclasses do not model are carried over from the file
    already there, so a comment block or a key a future version added survives
    a ``pm add``. Keys *inside* a source are all modelled — ``enabled``,
    ``token_env``, and ``settings`` for everything else — so they round-trip
    through the reader unchanged.
    """
    target = Path(path) if path is not None else write_config_path(environ)
    document: dict[str, Any] = dump_config(config)
    existing = _existing_top_level(target)
    if existing is not None:
        extras = {str(key): value for key, value in existing.items() if key != "sources"}
        if extras:
            document = {**extras, **document}
    import yaml

    try:
        rendered = yaml.safe_dump(
            document, sort_keys=False, default_flow_style=False, allow_unicode=True
        )
    except yaml.YAMLError as exc:
        raise PmConfigError(f"cannot render {target}: {exc}") from exc
    _atomic_write_text(target, _WRITTEN_HEADER + rendered)
    return target
