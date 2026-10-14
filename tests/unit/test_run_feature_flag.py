"""``mini-ork run --set K=V`` — repeatable env overrides, gated for premium knobs.

The flag mirrors the ``--deadline``/``--placement``/``--env`` pre-parse: ``--set
K=V`` and ``--set=K=V`` collect repeatably, a value without ``=`` fails closed
with exit 2, and the survivors are run through ``mini_ork.features.gate_env`` so
a premium feature knob is dropped (with a one-line notice) unless
``MO_ACCEPT_PREMIUM`` opts in. The parse/gate seam is a pure helper
(``cli._gate_set_overrides``) so these run without driving the lifecycle.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.cli import main as cli

REPO = Path(__file__).resolve().parents[2]

# review_panel is premium (3 lanes ⇒ ×3.0 > PREMIUM_THRESHOLD); ``MO_REVIEW_PANEL``
# is its only knob. A baseline knob (learning_writeback) must always survive.
_PREMIUM_PAIR = "MO_REVIEW_PANEL=codex kimi glm"
_BASELINE_PAIR = "MO_LEARNING_WRITEBACK=1"


def test_premium_knob_is_stripped_without_the_opt_in():
    filtered, blocked, err = cli._gate_set_overrides([_PREMIUM_PAIR], {})
    assert err == ""
    assert filtered == {}  # the knob was removed with the feature
    assert blocked == ["review_panel"]


def test_premium_knob_is_kept_when_mo_accept_premium_is_set():
    filtered, blocked, err = cli._gate_set_overrides(
        [_PREMIUM_PAIR], {"MO_ACCEPT_PREMIUM": "1"})
    assert err == ""
    assert blocked == []
    assert filtered == {"MO_REVIEW_PANEL": "codex kimi glm"}


def test_set_mo_accept_premium_unlocks_in_the_same_invocation():
    """The overrides themselves gate the run: ``--set MO_ACCEPT_PREMIUM=1`` counts."""
    filtered, blocked, err = cli._gate_set_overrides(
        [_PREMIUM_PAIR, "MO_ACCEPT_PREMIUM=1"], {})
    assert err == ""
    assert blocked == []
    assert filtered == {
        "MO_REVIEW_PANEL": "codex kimi glm",
        "MO_ACCEPT_PREMIUM": "1",
    }


@pytest.mark.parametrize("value", ["0", ""])
def test_a_falsey_mo_accept_premium_does_not_unlock(value):
    filtered, blocked, _ = cli._gate_set_overrides(
        [_PREMIUM_PAIR], {"MO_ACCEPT_PREMIUM": value})
    assert filtered == {} and blocked == ["review_panel"]


def test_baseline_knobs_pass_through_unfiltered():
    filtered, blocked, err = cli._gate_set_overrides([_BASELINE_PAIR], {})
    assert err == "" and blocked == []
    assert filtered == {"MO_LEARNING_WRITEBACK": "1"}


def test_repeatable_pairs_merge_and_last_wins():
    filtered, _, err = cli._gate_set_overrides(
        ["MO_LEARNING_WRITEBACK=1", "MO_LEARNING_WRITEBACK=0"], {})
    assert err == ""
    assert filtered == {"MO_LEARNING_WRITEBACK": "0"}


def test_malformed_pair_reports_the_contract_error():
    filtered, blocked, err = cli._gate_set_overrides(["FOO"], {})
    assert (filtered, blocked) == ({}, [])
    assert err == "--set requires K=V"


@pytest.mark.parametrize("argv", [["--set", "FOO"], ["--set="], ["--set"]])
def test_malformed_set_exits_2_in_the_pre_parse(argv, capsys):
    rc = cli._run_lifecycle_impl([*argv, "code-fix", "k.md"], str(REPO), None)
    assert rc == 2
    assert capsys.readouterr().err == "--set requires K=V\n"


def test_run_usage_documents_the_flag():
    assert "--set K=V" in cli._HELP
