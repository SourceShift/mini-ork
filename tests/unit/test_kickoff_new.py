"""``mini-ork kickoff new`` — draft a kickoff from a one-line goal (FR-011).

The draft lands in the folder the Orchestrator's Kickoffs tab reads, with the
slug the tab derives and front matter ``board start`` resolves — so the tests
assert against those two readers rather than against the template's own text:
a draft the linter rejects, or one ``board start`` cannot resolve a recipe from,
is a draft the user cannot use, however the file looks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork import kickoff_lint
from mini_ork.cli import board_cmd, kickoff_cmd
from mini_ork.ide_pages import orch
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
GOAL = "Fix the auth middleware timeout"
SLUG = "fix-the-auth-middleware-timeout"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """An initialised home: the seam test at the end launches through it."""
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config" / "task_classes").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _new(rest: list[str], capsys) -> tuple[int, str, str]:
    rc = kickoff_cmd.main(rest, "")
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ── where the draft lands ──────────────────────────────────────────────────

def test_the_draft_lands_in_the_folder_the_kickoffs_tab_reads(home: Path, capsys) -> None:
    rc, out, _err = _new(["new", GOAL, "--home", str(home)], capsys)
    path = home / "kickoffs" / f"{SLUG}.md"
    assert rc == 0
    assert out.strip() == str(path), "the caller is told what was written"
    rows = orch._scan_kickoffs(home)
    assert [row["path"] for row in rows] == [path]
    assert rows[0]["declared"] == "code-fix"
    assert rows[0]["recipe"] == "code-fix"


def test_the_slug_is_the_one_the_linter_derives(home: Path, capsys) -> None:
    """Not a second slugger: a draft here and a thread's draft are one file."""
    _new(["new", GOAL, "--home", str(home)], capsys)
    assert (home / "kickoffs" / f"{kickoff_lint.slug(f'# {GOAL}')}.md").is_file()


def test_a_draft_reads_back_lint_clean_on_the_tab(home: Path, capsys) -> None:
    """The point of the template: a fresh draft is not born red."""
    _new(["new", GOAL, "--home", str(home)], capsys)
    row = orch._scan_kickoffs(home)[0]
    assert row["findings"] == []
    assert orch._lint_badge(row["findings"]) == ("lint clean", "sub")


def test_the_title_is_the_first_line_and_the_rest_is_the_body(home: Path, capsys) -> None:
    _new(["new", f"{GOAL}\n\nOnly the API keys, not the session cookie.\nAnd log the retry.", "--home",
          str(home)], capsys)
    text = (home / "kickoffs" / f"{SLUG}.md").read_text(encoding="utf-8")
    # The title comes first — it is what ``slug`` derives the filename from, so a
    # draft whose first line landed in the body would be filed under the wrong name.
    assert text.index(f"# {GOAL}") < text.index("Only the API keys, not the session cookie.")
    assert "And log the retry." in text
    assert "## Success" in text


# ── the recipe the draft declares ──────────────────────────────────────────

def test_the_recipe_comes_from_the_flag(home: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MO_ACP_RECIPE", "audit")
    _new(["new", GOAL, "--recipe", "refactor", "--home", str(home)], capsys)
    text = (home / "kickoffs" / f"{SLUG}.md").read_text(encoding="utf-8")
    assert kickoff_lint.front_matter_recipe(text) == "refactor"


def test_the_recipe_comes_from_the_environment_when_the_flag_is_absent(home: Path, monkeypatch,
                                                                       capsys) -> None:
    monkeypatch.setenv("MO_ACP_RECIPE", "refactor")
    _new(["new", GOAL, "--home", str(home)], capsys)
    text = (home / "kickoffs" / f"{SLUG}.md").read_text(encoding="utf-8")
    assert kickoff_lint.front_matter_recipe(text) == "refactor"


def test_the_recipe_defaults_to_the_one_every_reader_assumes(home: Path, monkeypatch,
                                                             capsys) -> None:
    monkeypatch.delenv("MO_ACP_RECIPE", raising=False)
    _new(["new", GOAL, "--home", str(home)], capsys)
    text = (home / "kickoffs" / f"{SLUG}.md").read_text(encoding="utf-8")
    assert kickoff_lint.front_matter_recipe(text) == kickoff_cmd.DEFAULT_RECIPE == "code-fix"


# ── the refusals ───────────────────────────────────────────────────────────

def test_an_existing_draft_is_refused_and_the_existing_file_is_named(home: Path, capsys) -> None:
    _new(["new", GOAL, "--home", str(home)], capsys)
    path = home / "kickoffs" / f"{SLUG}.md"
    path.write_text("---\nrecipe: refactor\n---\n# Mine\n", encoding="utf-8")
    rc, out, err = _new(["new", GOAL, "--home", str(home)], capsys)
    assert rc == 2
    assert out == ""
    assert str(path) in err and "already exists" in err
    assert path.read_text(encoding="utf-8") == "---\nrecipe: refactor\n---\n# Mine\n", \
        "the refusal wrote nothing"


def test_an_empty_goal_is_refused_and_nothing_is_created(home: Path, capsys) -> None:
    rc, out, err = _new(["new", "   \n  \n", "--home", str(home)], capsys)
    assert rc == 2
    assert out == "" and "the goal is empty" in err
    assert not (home / "kickoffs").exists()


def test_no_subverb_is_a_usage_error(home: Path, capsys) -> None:
    """Bare invocation, and nothing written: the verb is what a caller must name."""
    rc, out, err = _new([], capsys)
    assert rc == 2
    assert out == ""
    assert "usage: mini-ork kickoff new" in err
    assert not (home / "kickoffs").exists()


# ── the seam ───────────────────────────────────────────────────────────────

def test_what_the_draft_declares_is_what_board_start_reads_back(home: Path, capsys) -> None:
    """The write→read loop FR-011 claims: draft, then start from it, dry.

    ``--dry-run`` because the point is the *resolution* — recipe and its source
    — and a real launch is a side effect on the shared engine.
    """
    _new(["new", GOAL, "--recipe", "refactor", "--home", str(home)], capsys)
    path = home / "kickoffs" / f"{SLUG}.md"
    rc = board_cmd.main(["start", "--kickoff", str(path), "--dry-run", "--home", str(home)], "")
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["recipe"] == "refactor"
    assert payload["recipe_source"] == "front matter"
    assert payload["kickoff"] == str(path)
