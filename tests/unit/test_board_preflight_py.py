"""`mini-ork board preflight <kickoff.md>` — resolve a launch without spending it.

The verb answers the four things a misclassified `board start` costs a whole run
to discover: the recipe a start would use, the classifier's marker, the top three
candidate scores, and whether that recipe resolves to a real recipe tree.

Contracts pinned here:
  (a) a front-matter `recipe:` is the recipe, `resolved` true when the tree exists
  (b) `--recipe` beats front matter, exactly as `board start` resolves it
  (c) a kickoff with no declared recipe is NOT blessed by a classifier guess:
      `ok` false with the same refusal a start would give, classifier in `marker`
  (d) the classifier's ranking is exposed as `top3`, best first
  (e) a missing kickoff is a usage error (stage `usage`, exit 2)
  (f) the verb is reachable through the shared parser, from either positional
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import board_cmd  # noqa: E402

KICKOFF_WITH_RECIPE = """---
recipe: tiny-recipe
---

# Do the thing

## Done When

1. `mini-ork validate <kickoff>` exits 0
2. the bug no longer reproduces
"""

KICKOFF_NO_RECIPE = """# Fix a failing regression

A stack trace from the test suite; the survey says SOTA is unclear.

## Done When

1. the test passes
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home with three task classes and one real recipe tree."""
    h = tmp_path / ".mini-ork"
    classes = h / "config" / "task_classes"
    classes.mkdir(parents=True)
    (classes / "code_fix.yaml").write_text(
        "matches:\n  keywords: [bug, fix, regression, failing test]\n"
        "  regex: ['stack ?trace']\n",
        encoding="utf-8",
    )
    (classes / "research_synthesis.yaml").write_text(
        "matches:\n  keywords: [literature, survey, SOTA]\n", encoding="utf-8")
    (classes / "docs_only.yaml").write_text(
        "matches:\n  keywords: [survey]\n", encoding="utf-8")

    recipe = h / "recipes" / "tiny-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(
        "nodes:\n  only:\n    type: implementer\n", encoding="utf-8")
    (recipe / "task_class.yaml").write_text(
        "name: tiny_recipe\n", encoding="utf-8")
    return h


def _preflight(home: Path, *argv: str) -> dict:
    args = board_cmd.build_parser().parse_args(["preflight", *argv])
    assert board_cmd.validate(args) is None
    return board_cmd.build_payload(home, args)


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_the_front_matter_recipe_resolves(home: Path, tmp_path: Path):
    got = _preflight(home, _write(tmp_path, "k.md", KICKOFF_WITH_RECIPE))

    assert got["ok"] is True, got
    assert got["recipe"] == "tiny-recipe"
    assert got["recipe_source"] == "front matter"
    assert got["resolved"] is True
    assert "error" not in got


def test_the_recipe_flag_beats_the_front_matter(home: Path, tmp_path: Path):
    got = _preflight(home, _write(tmp_path, "k.md", KICKOFF_WITH_RECIPE),
                     "--recipe", "code-fix")

    assert got["recipe"] == "code-fix"
    assert got["recipe_source"] == "--recipe"
    # code-fix is an engine recipe, so it resolves from the engine tree.
    assert got["resolved"] is True


def test_a_missing_recipe_is_a_refusal_not_a_guess(home: Path, tmp_path: Path):
    """The classifier's pick is `marker`; it never becomes the recipe, because a
    real start refuses a kickoff that declares none."""
    got = _preflight(home, _write(tmp_path, "k.md", KICKOFF_NO_RECIPE))

    assert got["ok"] is False
    assert got["recipe"] == ""
    assert got["recipe_source"] == ""
    assert got["resolved"] is False
    assert got["marker"] == "code_fix"
    assert "pass --recipe" in got["error"]


def test_the_classifier_ranking_is_exposed_best_first(home: Path, tmp_path: Path):
    got = _preflight(home, _write(tmp_path, "k.md", KICKOFF_NO_RECIPE))

    names = [row["task_class"] for row in got["top3"]]
    scores = [row["score"] for row in got["top3"]]
    assert len(names) == 3
    assert names[0] == "code_fix"
    assert scores == sorted(scores, reverse=True)
    assert got["marker_score"] == scores[0]


def test_an_unresolvable_recipe_names_what_is_missing(home: Path, tmp_path: Path):
    got = _preflight(home, _write(tmp_path, "k.md", KICKOFF_WITH_RECIPE),
                     "--recipe", "no-such-recipe")

    assert got["ok"] is False
    assert got["resolved"] is False
    assert "does not resolve" in got["error"]
    assert "workflow.yaml and task_class.yaml" in got["error"]


def test_a_missing_kickoff_is_a_usage_error(home: Path, tmp_path: Path):
    got = _preflight(home, str(tmp_path / "nope.md"))

    assert got["ok"] is False
    assert got["stage"] == "usage"
    assert "kickoff not found" in got["error"]


def test_the_verb_parses_from_either_positional_slot():
    for argv in (["preflight", "k.md"], ["preflight", "--kickoff", "k.md"]):
        args = board_cmd.build_parser().parse_args(argv)
        assert args.verb == "preflight"
        assert board_cmd.validate(args) is None


def test_the_verb_refuses_an_empty_invocation():
    args = board_cmd.build_parser().parse_args(["preflight"])
    assert board_cmd.validate(args) == "mini-ork board preflight: a kickoff path is required"


def test_the_cli_exits_2_on_a_usage_error(home: Path, tmp_path: Path, capsys):
    rc = board_cmd.main(["preflight", str(tmp_path / "nope.md"), "--home", str(home)], "")

    out = capsys.readouterr()
    assert rc == 2
    assert "kickoff not found" in out.err
    assert json.loads(out.out)["stage"] == "usage"


def test_the_cli_exits_0_on_a_resolved_kickoff(home: Path, tmp_path: Path, capsys):
    rc = board_cmd.main(["preflight", _write(tmp_path, "k.md", KICKOFF_WITH_RECIPE),
                         "--home", str(home)], "")

    out = capsys.readouterr()
    assert rc == 0
    assert json.loads(out.out)["recipe"] == "tiny-recipe"
