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
