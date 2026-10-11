"""Hermetic tests for ``mini_ork.pm.config``.

The config layer's job is to name a credential, never to hold one, and to fail
closed when a source is declared but unusable. What is pinned:

* a missing file is an ordinary unconfigured project; a file named explicitly
  that does not exist is an error
* ``enabled`` filters and orders; ``source`` names what was declared
* ``require`` refuses a disabled source and a blank ``token_env`` — an empty
  string is never read as "no credential needed"
* ``SourceConfig.require`` refuses a missing or blank setting
* the committed ``config/pm.yaml`` is the fallback, so a checkout has `local`
  switched on without anyone copying anything
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.pm import config  # noqa: E402


def test_no_file_is_an_empty_config(tmp_path, monkeypatch):
    # The repo's committed example is the last candidate, so point it away for
    # this test rather than depending on whether one is installed.
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path / "nowhere")
    loaded = config.load_config(environ={"MINI_ORK_HOME": str(tmp_path)})
    assert loaded.sources == {}
    assert loaded.path is None
    assert loaded.enabled() == ()


def test_the_committed_example_is_the_fallback():
    # A fresh checkout can use mini-ork's own epics with no setup at all.
    loaded = config.load_config(environ={})
    assert "local" in loaded.sources
    assert [source.id for source in loaded.enabled()] == ["local"]


def test_an_explicit_path_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(config.PmConfigError):
        config.load_config(environ={}, path=tmp_path / "missing.yaml")


def test_the_env_override_that_points_nowhere_is_an_error(tmp_path):
    with pytest.raises(config.PmConfigError):
        config.load_config(environ={"MINI_ORK_PM": str(tmp_path / "missing.yaml")})


def test_the_env_override_wins(tmp_path):
    path = tmp_path / "pm.yaml"
    path.write_text("sources:\n  linear:\n    enabled: true\n    team: ENG\n", encoding="utf-8")
    loaded = config.load_config(environ={"MINI_ORK_PM": str(path)})
    assert [source.id for source in loaded.enabled()] == ["linear"]


def test_parse_keeps_settings_and_enabled_flag():
    parsed = config.parse_config(
        {
            "sources": {
                "linear": {"enabled": True, "token_env": "LINEAR_API_KEY", "team": "ENG"},
                "jira": {"enabled": False},
                "bare": None,
            }
        }
    )
    assert [source.id for source in parsed.enabled()] == ["linear"]
    linear = parsed.source("linear")
    assert linear.token_env == "LINEAR_API_KEY"
    assert linear.settings == {"team": "ENG"}
    # A source with a name and nothing under it is declared and off, not an
    # error: it is how someone leaves themselves a note about a tool they have
    # not configured yet.
    assert parsed.source("bare").enabled is False


@pytest.mark.parametrize("document", [42, [1, 2], "sources"])
def test_a_document_that_is_not_a_mapping_is_refused(document):
    with pytest.raises(config.PmConfigError):
        config.parse_config(document)


def test_a_sources_block_that_is_not_a_mapping_is_refused():
    # A typo'd indentation would otherwise disable every source and look like a
    # project that never configured any.
    with pytest.raises(config.PmConfigError):
        config.parse_config({"sources": ["linear"]})


def test_an_unknown_source_names_the_declared_ones():
    parsed = config.parse_config({"sources": {"local": {"enabled": True}}})
    with pytest.raises(config.PmConfigError) as caught:
        parsed.source("linear")
    assert "local" in str(caught.value)


def test_require_refuses_a_disabled_source():
    parsed = config.parse_config({"sources": {"linear": {"enabled": False}}})
    with pytest.raises(config.PmConfigError):
        parsed.require("linear")


def test_require_refuses_an_empty_token_env():
    parsed = config.parse_config({"sources": {"linear": {"enabled": True, "token_env": "  "}}})
    with pytest.raises(config.PmConfigError):
        parsed.require("linear")


def test_require_accepts_a_source_that_needs_no_credential():
    parsed = config.parse_config({"sources": {"local": {"enabled": True}}})
    assert parsed.require("local").id == "local"


def test_source_settings_require_fails_closed():
    parsed = config.parse_config({"sources": {"linear": {"enabled": True, "team": "ENG"}}})
    linear = parsed.require("linear")
    assert linear.require("team") == ("ENG",)
    with pytest.raises(config.PmConfigError):
        linear.require("site")


def test_a_setting_that_is_only_whitespace_counts_as_missing():
    parsed = config.parse_config({"sources": {"linear": {"enabled": True, "team": " "}}})
    with pytest.raises(config.PmConfigError):
        parsed.source("linear").require("team")


def test_reading_a_broken_file_raises(tmp_path):
    path = tmp_path / "pm.yaml"
    path.write_text("sources: [unclosed\n", encoding="utf-8")
    with pytest.raises(config.PmConfigError):
        config.load_config(environ={}, path=path)
