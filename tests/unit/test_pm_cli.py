"""``mini-ork pm`` — declare, test and drop a source without leaking a secret.

Everything here runs against a throwaway home and asserts two things at once:
that the verb does its job, and that the credential the operator typed appears
**nowhere** in what the command printed. A sentinel is piped in (or exported)
and then grepped for across stdout, stderr and the argv every refusal offers.

No network: ``pm test`` is the one verb that reads, and its provider comes from
a stub reached through the same ``registry.get_provider`` seam the command uses
in production.
"""
from __future__ import annotations

import io
import stat
import sys
from pathlib import Path

import pytest

from mini_ork.cli import pm as pm_cli
from mini_ork.dispatch.secrets import read_secret_exports
from mini_ork.pm import registry
from mini_ork.pm.config import load_config
from mini_ork.pm.provider import Page, ProviderError, Query

SENTINEL = "pm-SENTINEL-value-never-printed-4c17"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home with nothing declared, and no ambient overrides.

    ``MINI_ORK_PM`` and ``MINI_ORK_SECRETS`` are cleared so the config and the
    store resolve inside this home rather than a developer's checkout.
    """
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.delenv("MINI_ORK_PM", raising=False)
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    return h


def _run(
    monkeypatch: pytest.MonkeyPatch, *argv: str, stdin: str | None = None
) -> int:
    """Run ``mini-ork pm <argv>`` with ``stdin`` as the piped value.

    ``stdin=None`` leaves the captured stdin alone; any string — including the
    empty one — replaces it, so "piped nothing" and "no pipe at all" are two
    different cases rather than one.
    """
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return pm_cli.main([str(a) for a in argv])


def _sops_vault(home: Path) -> None:
    (home / "config" / "vault.yaml").write_text(
        "backends:\n  - type: env\n  - type: sops\n    file: secrets.enc.yaml\n",
        encoding="utf-8",
    )


def _store(home: Path) -> Path:
    return home / "config" / "secrets.local.sh"


# ── add ────────────────────────────────────────────────────────────────────


def test_add_stores_the_secret_owner_only_and_names_it_in_the_config(
    home, monkeypatch, capsys
):
    rc = _run(monkeypatch, "add", "linear", "--set", "team=ENG",
              "--token-stdin", stdin=SENTINEL)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert read_secret_exports(_store(home)) == {"LINEAR_API_KEY": SENTINEL}
    assert stat.S_IMODE(_store(home).stat().st_mode) == 0o600

    text = (home / "config" / "pm.yaml").read_text(encoding="utf-8")
    assert "LINEAR_API_KEY" in text          # the key NAME, so the vault can serve it
    assert "team: ENG" in text
    assert SENTINEL not in text              # the value never lands in the config
    assert SENTINEL not in out and SENTINEL not in err


def test_add_round_trips_through_load_config(home, monkeypatch, capsys):
    _run(monkeypatch, "add", "linear", "--set", "team=ENG",
         "--token-stdin", stdin=SENTINEL)
    capsys.readouterr()
    linear = load_config().require("linear")
    assert linear.enabled is True
    assert linear.token_env == "LINEAR_API_KEY"
    assert linear.settings == {"team": "ENG"}


def test_add_defaults_the_token_env_to_the_adapters_convention(home, monkeypatch, capsys):
    _run(monkeypatch, "add", "jira", "--set", "site=acme", "--token-stdin",
         stdin=SENTINEL)
    capsys.readouterr()
    assert "JIRA_API_TOKEN" in (home / "config" / "pm.yaml").read_text(encoding="utf-8")


def test_add_keeps_the_sources_it_does_not_own(home, monkeypatch, capsys):
    """A hand-edited config's other sources survive a write into one of them."""
    (home / "config" / "pm.yaml").write_text(
        "note: keep me\nsources:\n  jira:\n    enabled: true\n    site: acme\n",
        encoding="utf-8",
    )
    _run(monkeypatch, "add", "linear", "--token-stdin", stdin=SENTINEL)
    capsys.readouterr()
    text = (home / "config" / "pm.yaml").read_text(encoding="utf-8")
    assert "jira" in text and "note: keep me" in text


def test_add_without_a_value_on_stdin_is_an_error(home, monkeypatch, capsys):
    rc = _run(monkeypatch, "add", "linear", "--token-stdin", stdin="")
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "LINEAR_API_KEY" in err
    assert not _store(home).exists()


def test_add_disable_declares_the_source_and_stores_nothing(home, monkeypatch, capsys):
    rc = _run(monkeypatch, "add", "linear", "--disable")
    out, _err = capsys.readouterr()
    assert rc == 0
    assert "disabled" in out
    assert not _store(home).exists()
    assert load_config().source("linear").enabled is False


def test_add_to_a_secure_tier_refuses_before_it_asks_for_a_value(
    home, monkeypatch, capsys
):
    """The refusal comes first, names the command, and leaks nothing.

    The sentinel is on stdin and must never be read, stored or printed: the
    operator learns to run ``sops set …`` instead of typing a secret into a
    flow that was going to refuse it.
    """
    _sops_vault(home)
    rc = _run(monkeypatch, "add", "linear", "--token-stdin", stdin=SENTINEL)
    out, err = capsys.readouterr()
    assert rc == 2
    assert "sops" in err
    assert "set" in err
    assert "LINEAR_API_KEY" in err
    assert SENTINEL not in out and SENTINEL not in err
    assert not _store(home).exists()
    assert not (home / "config" / "pm.yaml").exists()


def test_add_needs_an_id(home, monkeypatch, capsys):
    """``add`` with no source refuses — the missing positional is named."""
    rc = _run(monkeypatch, "add", "--token-stdin", stdin=SENTINEL)
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "id" in err


# ── sources ────────────────────────────────────────────────────────────────


def test_sources_lists_what_is_declared(home, monkeypatch, capsys):
    _run(monkeypatch, "add", "linear", "--set", "team=ENG", "--token-stdin",
         stdin=SENTINEL)
    capsys.readouterr()
    rc = _run(monkeypatch, "sources")
    out, _err = capsys.readouterr()
    assert rc == 0
    assert "declared" in out and "linear" in out
    assert "LINEAR_API_KEY" in out
    assert SENTINEL not in out                       # the key name, never the value


def test_sources_reports_a_config_it_cannot_read(home, monkeypatch, capsys):
    (home / "config" / "pm.yaml").write_text("sources: [unclosed\n", encoding="utf-8")
    rc = _run(monkeypatch, "sources")
    _out, err = capsys.readouterr()
    assert rc == 2
    assert err.strip()                                  # the config's own message


def test_sources_names_a_source_missing_a_setting(home, monkeypatch, capsys):
    """A source declared without its setting is reported, not silently built."""
    _run(monkeypatch, "add", "linear", "--token-stdin", stdin=SENTINEL)  # no team=
    capsys.readouterr()
    rc = _run(monkeypatch, "sources")
    out, _err = capsys.readouterr()
    assert rc == 0
    assert "team" in out


# ── test ───────────────────────────────────────────────────────────────────


class _StubProvider:
    id = "linear"
    label = "Linear"
    capabilities = None  # unused by `pm test`

    def __init__(self, page: Page | None = None, error: Exception | None = None):
        self._page = page if page is not None else Page(issues=())
        self._error = error
        self.calls: list[Query] = []

    def list_issues(self, query: Query) -> Page:
        self.calls.append(query)
        if self._error is not None:
            raise self._error
        return self._page


def _declared(home: Path, text: str) -> None:
    (home / "config" / "pm.yaml").write_text(text, encoding="utf-8")


def test_test_reads_one_issue_through_the_registered_provider(
    home, monkeypatch, capsys
):
    _declared(home, "sources:\n  linear:\n    enabled: true\n    token_env: LINEAR_API_KEY\n    team: ENG\n")
    stub = _StubProvider()
    monkeypatch.setattr(registry, "get_provider", lambda provider_id: stub)
    rc = _run(monkeypatch, "test", "linear")
    out, _err = capsys.readouterr()
    assert rc == 0
    assert "ok" in out and "linear" in out
    assert [q.limit for q in stub.calls] == [1]        # one issue, not the backlog


def test_test_never_prints_the_value_its_provider_used(home, monkeypatch, capsys):
    _declared(home, "sources:\n  linear:\n    enabled: true\n    token_env: LINEAR_API_KEY\n")
    monkeypatch.setenv("LINEAR_API_KEY", SENTINEL)
    monkeypatch.setattr(registry, "get_provider", lambda provider_id: _StubProvider())
    rc = _run(monkeypatch, "test", "linear")
    out, err = capsys.readouterr()
    assert rc == 0
    assert SENTINEL not in out and SENTINEL not in err


def test_test_surfaces_a_provider_error_without_a_token(home, monkeypatch, capsys):
    _declared(home, "sources:\n  linear:\n    enabled: true\n    token_env: LINEAR_API_KEY\n")
    stub = _StubProvider(error=ProviderError(
        "linear list issues failed: 401 Unauthorized"))
    monkeypatch.setattr(registry, "get_provider", lambda provider_id: stub)
    rc = _run(monkeypatch, "test", "linear")
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "401" in err


def test_test_refuses_an_undeclared_source(home, monkeypatch, capsys):
    _declared(home, "sources:\n  jira:\n    enabled: true\n    token_env: JIRA_API_TOKEN\n")
    rc = _run(monkeypatch, "test", "linear")
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "jira" in err                                # names what IS declared


def test_test_refuses_a_source_whose_settings_are_incomplete(home, monkeypatch, capsys):
    """No provider is built, so nothing reaches the network — the config stops it."""
    _declared(home, "sources:\n  linear:\n    enabled: true\n    token_env: LINEAR_API_KEY\n")

    def _boom(provider_id):
        raise registry.UnknownProvider(provider_id, [])

    monkeypatch.setattr(registry, "get_provider", _boom)
    rc = _run(monkeypatch, "test", "linear")
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "team" in err                                 # the missing setting is named


# ── remove ─────────────────────────────────────────────────────────────────


def test_remove_drops_the_declaration_and_leaves_the_credential(
    home, monkeypatch, capsys
):
    _run(monkeypatch, "add", "linear", "--token-stdin", stdin=SENTINEL)
    capsys.readouterr()
    rc = _run(monkeypatch, "remove", "linear")
    out, _err = capsys.readouterr()
    assert rc == 0
    assert "linear" in out
    assert "linear" not in load_config().sources
    # The value stays where the vault keeps it — another tool may use the key.
    assert read_secret_exports(_store(home)) == {"LINEAR_API_KEY": SENTINEL}


def test_remove_of_an_undeclared_source_is_an_error(home, monkeypatch, capsys):
    _declared(home, "sources:\n  jira:\n    enabled: true\n    token_env: JIRA_API_TOKEN\n")
    rc = _run(monkeypatch, "remove", "linear")
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "linear" in err


# ── the surface itself ─────────────────────────────────────────────────────


def test_a_verb_with_no_verb_prints_usage_and_exits_two(home, monkeypatch, capsys):
    rc = _run(monkeypatch)
    out, _err = capsys.readouterr()
    assert rc == 2
    assert "mini-ork pm" in out


@pytest.mark.parametrize("verb", ["sources", "add", "test", "remove"])
def test_every_verb_accepts_home(monkeypatch, capsys, tmp_path, verb):
    """The IDE appends ``--home`` to every action; a verb without it exits 2
    with no terminal to say why."""
    extra = {"add": ["linear"], "test": ["linear"], "remove": ["linear"],
             "sources": []}[verb]
    parser = pm_cli.build_parser()
    # parse_known_args is the IDE's "did the button dispatch?" check.
    parser.parse_known_args([verb, *extra, "--home", str(tmp_path)])


def test_the_value_is_never_an_argument(home, monkeypatch, capsys):
    """``add`` with no ``--token-stdin`` and no terminal refuses rather than
    falling back to a flag that would land in a shell history."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rc = pm_cli.main(["add", "linear", "--home", str(home)])
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "token-stdin" in err
