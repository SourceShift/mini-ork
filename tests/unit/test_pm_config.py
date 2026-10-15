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


# ── the write side ─────────────────────────────────────────────────────────
#
# ``write_config`` is what ``mini-ork pm add`` / ``pm remove`` call. Two
# properties matter, and they are asserted rather than assumed: the document it
# writes is the reader's own inverse (so a hand-edit and a command agree), and
# it lands under the home — never on the committed ``<repo>/config/pm.yaml``.

_DECLARED = {
    "sources": {
        "linear": {"enabled": True, "token_env": "LINEAR_API_KEY", "team": "ENG"},
        "jira": {"enabled": False, "token_env": "JIRA_API_TOKEN",
                 "site": "https://acme.atlassian.net", "project": "ENG"},
        "bare": {"enabled": False},
    }
}


def test_dump_then_parse_reproduces_the_sources():
    original = config.parse_config(_DECLARED)
    again = config.parse_config(config.dump_config(original))
    assert again.sources == original.sources


def test_a_written_file_reads_back_as_the_same_config(tmp_path):
    original = config.parse_config(_DECLARED)
    path = config.write_config(original, tmp_path / "pm.yaml")
    assert config.load_config(environ={}, path=path).sources == original.sources


def test_write_config_path_is_under_the_home_not_the_repo():
    assert config.write_config_path({"MINI_ORK_HOME": "/tmp/proj/.mini-ork"}) == \
        Path("/tmp/proj/.mini-ork") / "config" / "pm.yaml"
    # No home at all still resolves inside a home, never to <repo>/config/pm.yaml.
    assert config.write_config_path({}) == Path(".mini-ork") / "config" / "pm.yaml"


def test_the_env_override_wins_for_writes_too(tmp_path):
    override = tmp_path / "elsewhere.yaml"
    assert config.write_config_path({"MINI_ORK_PM": str(override)}) == override


def test_write_config_never_touches_the_committed_repo_config(tmp_path):
    """The committed file is a read fallback; a temp-home write must not edit it."""
    committed = REPO / "config" / "pm.yaml"
    before = committed.read_bytes() if committed.is_file() else None
    config.write_config(
        config.parse_config(_DECLARED),
        environ={"MINI_ORK_HOME": str(tmp_path / "proj" / ".mini-ork")},
    )
    after = committed.read_bytes() if committed.is_file() else None
    assert after == before


def test_write_config_lands_owner_only(tmp_path):
    import stat

    path = config.write_config(config.parse_config(_DECLARED),
                               tmp_path / "proj" / "config" / "pm.yaml")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_config_creates_missing_parents(tmp_path):
    path = config.write_config(config.parse_config(_DECLARED),
                               tmp_path / "deep" / "nested" / "pm.yaml")
    assert path.is_file()


def test_write_config_leaves_no_temp_files_behind(tmp_path):
    directory = tmp_path / "config"
    config.write_config(config.parse_config(_DECLARED), directory / "pm.yaml")
    assert [p.name for p in sorted(directory.iterdir())] == ["pm.yaml"]


def test_write_config_keeps_a_top_level_key_it_does_not_model(tmp_path):
    path = tmp_path / "pm.yaml"
    path.write_text("note: keep me\nsources:\n  local:\n    enabled: true\n",
                    encoding="utf-8")
    config.write_config(config.parse_config(_DECLARED), path)
    reloaded = config.load_config(environ={}, path=path)
    assert reloaded.sources == config.parse_config(_DECLARED).sources
    assert "note: keep me" in path.read_text(encoding="utf-8")


def test_an_unparseable_existing_file_does_not_stop_the_write(tmp_path):
    """A file whose extra keys cannot be read is still worth replacing."""
    path = tmp_path / "pm.yaml"
    path.write_text("sources: [unclosed\n", encoding="utf-8")
    config.write_config(config.parse_config(_DECLARED), path)
    assert config.load_config(environ={}, path=path).sources == \
        config.parse_config(_DECLARED).sources


def test_the_written_file_carries_the_key_name_and_says_so(tmp_path):
    """The file names the vault key and states the rule in its own header."""
    path = config.write_config(config.parse_config(_DECLARED), tmp_path / "pm.yaml")
    text = path.read_text(encoding="utf-8")
    assert "LINEAR_API_KEY" in text
    assert "A credential VALUE never appears here" in text
