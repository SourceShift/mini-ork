"""Hermetic tests for ``mini_ork.pm.registry``.

The registry is a module-level dict, so these tests treat it as shared state:
every registration is either the built-in one or is undone in a ``finally``.
What is pinned:

* importing ``mini_ork.pm`` registers the local source — that side effect is
  what makes "add a provider" one file plus one line
* an unknown id raises ``UnknownProvider`` and names what would have worked
* a duplicate id raises unless ``replace=True``
* an id with a colon is refused, because it would mint uids that
  ``model.split_uid`` rejects
* the catalogue is JSON-safe and capability-shaped
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import mini_ork.pm  # noqa: E402,F401  (import registers the built-ins)
from mini_ork.pm import registry  # noqa: E402
from mini_ork.pm.provider import Capabilities  # noqa: E402


class StubSource:
    """A source that only has to have the three registry attributes."""

    def __init__(self, source_id: str, label: str = "Stub") -> None:
        self.id = source_id
        self.label = label
        self.capabilities = Capabilities(create=True)


@pytest.fixture
def unregistered():
    """Remove a stub after the test, whatever happens inside it."""
    added: list[str] = []
    yield added
    for source_id in added:
        registry.unregister_provider(source_id)


def test_importing_the_package_registers_its_own_epics():
    assert "local" in registry.provider_ids()
    assert registry.get_provider("local").label == "mini-ork epics"


def test_providers_are_ordered_by_id():
    ids = [provider.id for provider in registry.providers()]
    assert ids == sorted(ids)


def test_unknown_id_names_the_known_ones():
    with pytest.raises(registry.UnknownProvider) as caught:
        registry.get_provider("linear")
    assert "linear" in str(caught.value)
    assert "local" in str(caught.value)


def test_registering_twice_is_refused(unregistered):
    registry.register_provider(StubSource("stub"), replace=True)
    unregistered.append("stub")
    with pytest.raises(ValueError):
        registry.register_provider(StubSource("stub"))


def test_replace_overrides(unregistered):
    registry.register_provider(StubSource("stub", "First"), replace=True)
    unregistered.append("stub")
    registry.register_provider(StubSource("stub", "Second"), replace=True)
    assert registry.get_provider("stub").label == "Second"


def test_an_id_with_a_colon_is_refused():
    # 'stub:1' would mint uids of the form 'stub:1:ENG-1', which split_uid
    # would read back as provider 'stub' — quietly wrong rather than broken.
    with pytest.raises(ValueError):
        registry.register_provider(StubSource("stub:1"))


def test_a_source_without_an_id_is_refused():
    with pytest.raises(ValueError):
        registry.register_provider(StubSource(""))


def test_capabilities_shortcut_matches_the_provider():
    assert registry.capabilities("local") is registry.get_provider("local").capabilities


def test_catalogue_is_json_safe_and_capability_shaped():
    payload = json.loads(registry.catalogue_json())
    entries = {entry["id"]: entry for entry in payload["providers"]}
    assert "local" in entries
    local = entries["local"]
    assert local["read_only"] is False
    assert local["capabilities"]["create"] is True
    # The one this whole package exists for: epics cannot be transitioned.
    assert local["capabilities"]["transition"] is False


# ── one adapter, many sources ──────────────────────────────────────────────
#
# config/pm.yaml documents `github-core` / `linear-ops` / `jira-ops` as second
# sources with their own repo/team. Registration used to look adapters up by
# exact id, so every one of them was silently never registered.


def _register(*sources):
    from mini_ork.pm import providers
    from mini_ork.pm.config import PmConfig

    before = len(providers._SKIPPED)
    providers._register_configured(PmConfig(sources={s.id: s for s in sources}, path=None))
    added = providers._SKIPPED[before:]
    del providers._SKIPPED[before:]
    return added


def test_a_suffixed_id_registers_with_its_prefix_adapter(unregistered):
    from mini_ork.pm.config import SourceConfig
    from mini_ork.pm.providers.github import GithubProvider
    from mini_ork.pm.providers.linear import LinearProvider

    skipped = _register(
        SourceConfig(id="github-cn", enabled=True, token_env="GITHUB_TOKEN",
                     settings={"owner": "acme", "repo": "cn"}),
        SourceConfig(id="linear-ops", enabled=True, token_env="LINEAR_API_KEY",
                     settings={"team": "OPS"}),
    )
    unregistered.extend(["github-cn", "linear-ops"])

    assert skipped == []
    assert isinstance(registry.get_provider("github-cn"), GithubProvider)
    assert isinstance(registry.get_provider("linear-ops"), LinearProvider)
    assert registry.get_provider("github-cn").id == "github-cn"  # uids stay per-source


def test_an_explicit_adapter_setting_wins(unregistered):
    from mini_ork.pm.config import SourceConfig
    from mini_ork.pm.providers.jira import JiraProvider

    skipped = _register(SourceConfig(
        id="tracker", enabled=True, token_env="JIRA_API_TOKEN",
        settings={"adapter": "jira", "site": "https://acme.atlassian.net",
                  "email_env": "JIRA_EMAIL", "project": "ENG"}))
    unregistered.append("tracker")

    assert skipped == []
    assert isinstance(registry.get_provider("tracker"), JiraProvider)


def test_an_enabled_source_with_no_adapter_is_reported_not_dropped():
    from mini_ork.pm.config import SourceConfig

    skipped = _register(SourceConfig(id="asana-eng", enabled=True, token_env="X", settings={}))

    assert [source for source, _ in skipped] == ["asana-eng"]
    assert "'asana' adapter" in skipped[0][1]
    with pytest.raises(registry.UnknownProvider):
        registry.get_provider("asana-eng")


def test_the_local_stanza_is_not_reported_as_skipped():
    from mini_ork.pm.config import SourceConfig

    assert _register(SourceConfig(id="local", enabled=True, settings={})) == []
