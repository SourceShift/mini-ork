"""The operator-settings catalogue, and the guarantee that each knob is real.

Every :class:`Setting` claims a module honours it (``read_at``) and the page
shows that claim to the operator, so it must be true — a knob the engine ignores
is worse than no knob at all. The central test here reads each ``read_at`` file
and insists the env var's name actually occurs in it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mini_ork import settings_catalogue as sc

REPO = Path(__file__).resolve().parents[2]


def test_groups_are_the_display_order() -> None:
    assert sc.GROUPS == ("Cost & limits", "Verification", "Context & memory", "Git & PR")


def test_every_setting_declares_a_known_group() -> None:
    for setting in sc.ENGINE_SETTINGS:
        assert setting.group in sc.GROUPS, setting.name


def test_every_group_has_at_least_one_setting() -> None:
    """A group heading with nothing under it renders as an empty box."""
    assert {s.group for s in sc.ENGINE_SETTINGS} == set(sc.GROUPS)


def test_names_are_unique_and_env_shaped() -> None:
    names = [s.name for s in sc.ENGINE_SETTINGS]
    assert len(names) == len(set(names))
    for name in names:
        assert re.fullmatch(r"MO_[A-Z0-9_]+", name), name


@pytest.mark.parametrize("setting", sc.ENGINE_SETTINGS, ids=lambda s: s.name)
def test_the_knob_is_honoured_where_the_catalogue_says(setting: sc.Setting) -> None:
    """``read_at`` must name a file that actually mentions the env var."""
    path = REPO / setting.read_at
    assert path.is_file(), f"{setting.name}: read_at {setting.read_at} does not exist"
    assert setting.name in path.read_text(encoding="utf-8"), (
        f"{setting.name} claims to be read at {setting.read_at}, "
        "but its name never appears in that file"
    )


@pytest.mark.parametrize("setting", sc.ENGINE_SETTINGS, ids=lambda s: s.name)
def test_kind_and_tokens_agree(setting: sc.Setting) -> None:
    assert setting.kind in {"money", "seconds", "count", "onoff", "enum", "text"}
    if setting.kind == "enum":
        assert setting.tokens, f"{setting.name}: an enum must list its tokens"
    else:
        assert not setting.tokens, f"{setting.name}: only an enum carries tokens"


def test_enum_defaults_are_accepted_tokens() -> None:
    for s in sc.ENGINE_SETTINGS:
        if s.kind == "enum":
            assert s.default in s.tokens, f"{s.name}: default {s.default!r} not in {s.tokens}"


def test_onoff_defaults_are_zero_or_one() -> None:
    for s in sc.ENGINE_SETTINGS:
        if s.kind == "onoff":
            assert s.default in ("0", "1"), f"{s.name}: {s.default!r}"


def test_numeric_defaults_parse_as_numbers() -> None:
    for s in sc.ENGINE_SETTINGS:
        if s.kind in ("money", "seconds", "count"):
            float(s.default)  # raises if it is not a number


def test_effect_and_label_are_human_readable() -> None:
    for s in sc.ENGINE_SETTINGS:
        assert len(s.label) > 3 and s.label[0].isupper(), s.name
        assert s.effect.endswith(".") and len(s.effect) > 20, s.name


def test_settings_for_filters_by_group() -> None:
    assert sc.settings_for(None) == list(sc.ENGINE_SETTINGS)
    cost = sc.settings_for("Cost & limits")
    assert cost and all(s.group == "Cost & limits" for s in cost)
    assert sc.settings_for("No such group") == []
