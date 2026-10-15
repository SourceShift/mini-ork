"""``mini-ork pm`` — declare, test and drop a project-management source.

The write half of the PM surface. ``mini_ork/pm/config.py`` reads
``config/pm.yaml`` and ``mini_ork/pm/providers/`` build the adapters; this is
how a person changes either without hand-editing a file, and it is the
subcommand ``mini_ork/pm/providers/__init__.py`` has promised since
``skipped_sources`` was written and nothing registered.

    mini-ork pm sources
    mini-ork pm add <id> [--token-env NAME] [--set k=v ...] [--token-stdin]
    mini-ork pm test <id>
    mini-ork pm remove <id>

**The credential travels once.** It is read from stdin (``--token-stdin``) or
from a hidden prompt on a terminal — never from an argument, because an
argument is a shell history and a process table. It goes to
``mini_ork.dispatch.vault_write``, which writes the owner-only
``secrets.local.sh`` when the vault allows the file tier and otherwise refuses
with the exact command an operator should run. It never reaches
``config/pm.yaml``, which carries the key *name* only — so grepping the config,
a page payload, a log line or an error for the value finds nothing, by
construction rather than by care.

**Why ``add`` does not test the credential itself.** Connecting is an explicit
action in this project, never a side effect: reading a secret may unseal a
store, and a *config* command has to work on a host that is offline or whose
board is not reachable yet. So ``pm add`` validates the value's shape through
the writer that will store it, writes the source, and leaves the read to
``pm test`` — which the Board's Sources pane offers as its own button, and
which is the only verb here that makes a network call.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.dispatch.vault import VaultError
from mini_ork.dispatch.vault_write import assert_writable, set_secret
from mini_ork.pm import config as pm_config
from mini_ork.pm import providers as pm_providers
from mini_ork.pm import registry
from mini_ork.pm.config import PmConfigError
from mini_ork.pm.provider import ProviderError, Query

USAGE = """Usage:
  mini-ork pm sources
  mini-ork pm add <id> [--token-env NAME] [--set k=v ...] [--token-stdin] [--disable]
  mini-ork pm test <id>
  mini-ork pm remove <id>

`sources` lists what the config declares and how each one stands. `add`
declares a source and stores its credential in the store the vault reads —
`linear` and `jira` today. `test` reads one issue to prove the credential
works; it is the only verb here that touches the network. `remove` drops the
declaration and leaves the credential in the vault.

The value is never an argument: pipe it in with `--token-stdin`, or run on a
terminal and type it at the hidden prompt. `--set` carries what is not secret
(`--set team=ENG`, `--set site=acme project=ENG email_env=JIRA_EMAIL`).

Which store a credential goes to is decided by $MINI_ORK_HOME/config/vault.yaml
(see config/vault.example.yaml), never guessed: the default chain writes the
owner-only secrets.local.sh, and a sops or OpenBao backend is refused with the
command an operator should run instead.
"""

#: The vault key name `pm add` uses when the operator does not name one. The
#: conventional key per adapter, so a project that already exports
#: LINEAR_API_KEY needs no flag.
_DEFAULT_TOKEN_ENV = {
    "linear": "LINEAR_API_KEY",
    "jira": "JIRA_API_TOKEN",
    "github": "GITHUB_TOKEN",
    "local": "",
}


# ── parsers ────────────────────────────────────────────────────────────────
#
# `build_parser()` is the whole surface in one parser, built from the same
# per-verb parsers `main` dispatches with, so the IDE's "did the button
# dispatch?" check reads the command's real surface instead of a second,
# drifting copy of it. Every verb carries `--home`: the IDE appends it to each
# `cli` action whose `home` flag is not False, and a parser that rejected it
# would make the button exit 2 with no terminal to say why.


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", default=None)


def _sources_parser(prog: str = "mini-ork pm sources") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    _common(parser)
    return parser


def _add_parser(prog: str = "mini-ork pm add") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("id")
    parser.add_argument("--token-env", dest="token_env", default=None)
    # `nargs="*"` rather than one value per flag: the Board's Add-source box
    # hands its whole typing over as ONE argument, so a Jira source's three
    # settings arrive together. It also keeps the parser valid with nothing
    # after `--set`, which is exactly the prefix the IDE appends `--home` to.
    parser.add_argument("--set", dest="settings", action="append", nargs="*", default=None)
    parser.add_argument("--token-stdin", dest="token_stdin", action="store_true")
    parser.add_argument(
        "--disable", dest="enable", action="store_false", default=True,
        help="declare the source switched off, and store no credential",
    )
    _common(parser)
    return parser


def _test_parser(prog: str = "mini-ork pm test") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("id")
    _common(parser)
    return parser


def _remove_parser(prog: str = "mini-ork pm remove") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("id")
    _common(parser)
    return parser


def build_parser() -> argparse.ArgumentParser:
    """The whole ``mini-ork pm`` surface as one parser."""
    parser = argparse.ArgumentParser(
        prog="mini-ork pm", add_help=False, usage=argparse.SUPPRESS
    )
    subs = parser.add_subparsers(dest="verb")
    subs.add_parser("sources", parents=[_sources_parser()], usage=argparse.SUPPRESS)
    subs.add_parser("add", parents=[_add_parser()], usage=argparse.SUPPRESS)
    subs.add_parser("test", parents=[_test_parser()], usage=argparse.SUPPRESS)
    subs.add_parser("remove", parents=[_remove_parser()], usage=argparse.SUPPRESS)
    return parser


# ── shared helpers ─────────────────────────────────────────────────────────


def _home(explicit: str | None) -> Path:
    """The home this command resolves the vault and the config against.

    Pinned into the environment on purpose: the vault's file tier *and* its
    ``vault.yaml`` are read from ``MINI_ORK_HOME`` by modules that take no home
    argument of their own (``dispatch.secrets``, ``dispatch.vault``), so a
    ``--home`` only this module knew about would store one project's secret
    using another project's backend. ``mini-ork run`` pins the same variable
    the same way.
    """
    path = (
        Path(explicit).expanduser()
        if explicit
        else Path(os.environ.get("MINI_ORK_HOME") or ".mini-ork")
    )
    os.environ["MINI_ORK_HOME"] = str(path)
    return path


def _parse_settings(values: Iterable[str]) -> dict[str, str]:
    """``--set k=v`` (repeatable, whitespace- or comma-separated) → settings.

    Not a secret: these are Linear's team key, Jira's site and project, the
    things a person reads off a URL. The credential never comes through here.
    """
    # ``nargs="*"`` hands each flag back as a list of its own tokens, so flatten
    # one level before splitting on whitespace and commas.
    tokens: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            tokens.extend(str(item) for item in value)
        else:
            tokens.append(str(value))
    settings: dict[str, str] = {}
    for token in tokens:
        for part in token.replace(",", " ").split():
            key, separator, raw = part.partition("=")
            if not separator or not key.strip():
                raise ValueError(f"--set expects key=value, got {part!r}")
            settings[key.strip()] = raw
    return settings


def _default_token_env(source_id: str) -> str:
    return _DEFAULT_TOKEN_ENV.get(
        source_id, f"{source_id.upper().replace('-', '_')}_TOKEN"
    )


def _read_value(key: str, from_stdin: bool) -> str:
    """Read a credential exactly once: stdin, or a hidden prompt on a terminal."""
    if from_stdin:
        value = sys.stdin.read().strip()
        if not value:
            raise ValueError(f"no value on stdin for {key}")
        return value
    if not sys.stdin.isatty():
        raise ValueError(
            f"no terminal to prompt for {key}; pipe the value in with --token-stdin"
        )
    value = getpass.getpass(f"{key} (hidden, blank aborts): ")
    if not value:
        raise ValueError(f"no value entered for {key}")
    return value


def _provider_for(source: pm_config.SourceConfig) -> tuple[object | None, str]:
    """``(provider, why not)`` for a declared source, built against *this* home.

    Not ``registry.get_provider`` alone. ``mini_ork.pm.providers`` registers the
    configured sources once, at import time, from the home the process started
    with — and this command may have moved the home since (``--home``, or a
    plain ``MINI_ORK_HOME`` the caller set after import). So the registry is the
    first answer, and the adapter registry is the second: an adapter built here
    from the declared ``SourceConfig`` describes the config as it actually is,
    not as it was when the module was imported.
    """
    try:
        return registry.get_provider(source.id), ""
    except registry.UnknownProvider:
        pass
    if not source.enabled:
        # Switched off on purpose: there is nothing to build and nothing to
        # explain, so this is not a "skipped" source — ``providers`` itself
        # only ever skips what it tried to build (it walks ``enabled()``).
        return None, ""
    factory = pm_providers.adapter_for(source.id, source.settings.get("adapter"))
    if factory is None:
        return None, "no adapter in this build"
    try:
        return factory(source), ""
    except PmConfigError as exc:
        # Building an adapter is where a source with a missing setting is
        # refused (``SourceConfig.require``). The reason is the config's own
        # message — the same one ``skipped_sources`` keeps at import time.
        return None, str(exc)


def _merged(configuration: pm_config.PmConfig, source: pm_config.SourceConfig) -> pm_config.PmConfig:
    """``configuration`` with ``source`` replacing any entry under its own id."""
    sources = {
        source_id: existing
        for source_id, existing in configuration.sources.items()
        if source_id != source.id
    }
    sources[source.id] = source
    return pm_config.PmConfig(sources=sources, path=configuration.path)


# ── verbs ──────────────────────────────────────────────────────────────────


def _cmd_sources(args: argparse.Namespace) -> int:
    """Every declared source and how it stands, with the skipped ones explained."""
    home = _home(args.home)
    configuration = pm_config.load_config()
    mirrored = _mirror_state(home)

    declared = sorted(configuration.sources)
    enabled = [
        source_id
        for source_id in declared
        if configuration.sources[source_id].enabled
    ]
    built: dict[str, tuple[object | None, str]] = {
        source_id: _provider_for(configuration.sources[source_id])
        for source_id in declared
    }
    print(f"pm: config {configuration.path or '(no config/pm.yaml — nothing declared)'}")
    print(f"pm: declared {len(declared)} source(s): {', '.join(declared) or '(none)'}")
    print(f"pm: enabled {len(enabled)} source(s): {', '.join(enabled) or '(none)'}")
    unbuilt = [source_id for source_id in declared if built[source_id][1]]
    print(f"pm: skipped {len(unbuilt)} source(s): {', '.join(unbuilt) or '(none)'}")
    failed = [source_id for source_id in declared if _source_error(mirrored.get(source_id) or {})]
    print(f"pm: errored {len(failed)} source(s): {', '.join(failed) or '(none)'}")

    for source_id in declared:
        print(_describe_source(
            source_id,
            configuration.sources[source_id],
            provider=built[source_id][0],
            why_not=built[source_id][1],
            mirror=mirrored.get(source_id) or {},
        ))
    for source_id, why in pm_providers.skipped_sources():
        if source_id not in configuration.sources:
            # A config the loader itself refused, or an id with no declaration
            # left: the reason is the only thing that names it.
            print(f"pm: {source_id or '(config)'} skipped — {why}")
    return 0


def _mirror_state(home: Path) -> dict[str, dict]:
    """The mirror's per-source sync rows, or ``{}`` when there is no mirror yet."""
    from mini_ork.pm import store

    database = home / "state.db"
    if not database.is_file():
        return {}
    try:
        return {
            str(row.get("provider")): dict(row)
            for row in store.mirror_state(db=str(database))
        }
    except Exception:  # noqa: BLE001 — a listing must not fail on a stale mirror
        return {}


def _source_error(mirror: dict[str, Any]) -> str:
    """The recorded sync error for a source, or ``""``.

    Rendered verbatim where it is printed: it was credential-scrubbed when it
    was written (``mini_ork/pm/http.py``), and re-deriving it from a token here
    is exactly the leak this surface must not have.
    """
    return str(mirror.get("error") or "")


def _describe_source(
    source_id: str,
    source: pm_config.SourceConfig,
    *,
    provider: object | None,
    why_not: str,
    mirror: dict[str, Any],
) -> str:
    bits = ["enabled" if source.enabled else "disabled"]
    if source.token_env:
        bits.append(f"token_env {source.token_env}")
    if provider is not None:
        bits.extend(_capability_bits(provider.capabilities))
    elif why_not:
        bits.append(f"not built: {why_not}")
    error = _source_error(mirror)
    if error:
        bits.append(f"error: {error}")
    elif mirror.get("synced_at"):
        bits.append(f"synced {mirror['synced_at']}")
    return f"pm:   {source_id} — {' · '.join(bits)}"


def _capability_bits(capabilities) -> list[str]:
    """A source's declared abilities, in its own words — never a guess from ``hasattr``."""
    writes = list(capabilities.writes)
    bits = ["read-only"] if not writes else [f"writes: {', '.join(writes)}"]
    if capabilities.search:
        bits.append("search")
    if capabilities.pagination:
        bits.append("pagination")
    return bits


def _cmd_add(args: argparse.Namespace) -> int:
    """Declare a source and store its credential — the store first, the config last."""
    _home(args.home)
    source_id = str(args.id).strip()
    if not source_id:
        raise ValueError("pm add needs a source id (linear, jira)")
    settings = _parse_settings(args.settings or [])
    token_env = (args.token_env if args.token_env is not None else _default_token_env(source_id)).strip()
    enabled = bool(args.enable)

    configuration = pm_config.load_config()
    if enabled:
        # Refuse the store before asking anyone for a value: an operator whose
        # vault is sops learns the command to run instead, and never types a
        # secret into a flow that was going to refuse it. Nothing is written.
        assert_writable(token_env)
        value = _read_value(token_env, bool(args.token_stdin))
    else:
        value = ""

    source = pm_config.SourceConfig(
        id=source_id, enabled=enabled, token_env=token_env or None, settings=settings
    )
    # The credential first: a source declared before its key exists is a source
    # that fails closed on the next read, which is the worse of the two orders.
    if enabled and token_env:
        stored = set_secret(token_env, value)
        print(f"pm: {token_env} stored in {stored}")
    elif not enabled:
        print(f"pm: {source_id} declared disabled; no credential stored")
    target = pm_config.write_config(_merged(configuration, source))
    print(f"pm: {source_id} declared in {target}")
    if enabled:
        print(f"pm: check it with `mini-ork pm test {source_id}` (a network read)")
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    """One real read, to prove the credential the vault serves actually works.

    Deliberately its own verb: the epic's design rule is that connecting is an
    explicit action, never something opening a page does, because reading a
    secret may unseal a store. The provider's own message is what surfaces —
    it names the source and the key, never the value.
    """
    source_id = str(args.id).strip()
    _home(args.home)
    source = pm_config.load_config().require(source_id)
    provider, why_not = _provider_for(source)
    if provider is None:
        raise PmConfigError(f"pm source {source_id!r} is not usable: {why_not}")
    page = provider.list_issues(Query(limit=1))
    print(f"pm: {source_id} ok — read {len(page.issues)} issue(s) from {provider.label}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    """Drop a declaration. The credential stays where the vault keeps it."""
    source_id = str(args.id).strip()
    _home(args.home)
    configuration = pm_config.load_config()
    if source_id not in configuration.sources:
        raise ValueError(
            f"pm source {source_id!r} is not declared in "
            f"{configuration.path or 'config/pm.yaml'}"
        )
    remaining = pm_config.PmConfig(
        sources={
            key: value
            for key, value in configuration.sources.items()
            if key != source_id
        },
        path=configuration.path,
    )
    target = pm_config.write_config(remaining)
    print(f"pm: {source_id} removed from {target}")
    print("pm: its credential is left in the vault — remove it there if nothing else uses it")
    return 0


def main(argv: list[str] | None = None, *, root=None) -> int:  # noqa: ARG001 - dispatch contract
    """Run ``mini-ork pm`` without ever putting a credential in its output."""
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = build_parser().parse_known_args(raw)[0]
    except SystemExit as exc:  # argparse's own complaint is already on stderr
        return int(exc.code or 2)
    verb = getattr(parsed, "verb", None)
    try:
        if verb == "sources":
            return _cmd_sources(parsed)
        if verb == "add":
            return _cmd_add(parsed)
        if verb == "test":
            return _cmd_test(parsed)
        if verb == "remove":
            return _cmd_remove(parsed)
    except (
        SecretStoreError,
        VaultError,
        PmConfigError,
        ProviderError,
        registry.UnknownProvider,
        ValueError,
        OSError,
    ) as exc:
        # Every raiser in this path keeps the value out of its text — the
        # vault writer, the provider transport and the config layer all name
        # the key and the backend only — so this is the one place an error
        # reaches a terminal, and it carries no secret.
        sys.stderr.write(f"pm: {exc}\n")
        return 2
    sys.stdout.write(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
