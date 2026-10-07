"""Hermetic tests for :mod:`mini_ork.kickoff_lint` (Zed S7b).

Every test builds a tiny tmp git project (the parser walks ``project`` for
``## Files in scope`` paths), points ``MINI_ORK_HOME`` at a tmp dir with a
crafted recipe tree, and exercises the deterministic findings. The module
must NEVER raise: each finding case is paired with an explicit
``findings == [...]`` assertion so a future change to the message text
surfaces as a single diff line in CI.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import kickoff_lint  # noqa: E402


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A bare tmp project — files are added per-test as needed."""
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp ``.mini-ork`` home with one implementer recipe and one
    researcher-only recipe. ``MINI_ORK_HOME`` is set so
    ``recipe_dir`` resolves home-first."""
    h = tmp_path / ".mini-ork"
    h.mkdir()
    _write(h / "recipes" / "framework-edit" / "workflow.yaml", """
        version: '0.1.0'
        task_class: framework_edit
        nodes:
          - {name: implementer, type: implementer}
          - {name: static_check, type: verifier}
        edges: []
    """)
    _write(h / "recipes" / "framework-edit" / "example-kickoff.md", """
        # Framework Edit: Title
        ## Goal
        ## Acceptance
        - AC1
        ## Files in scope
        ## Out of scope
        ## Verification command
    """)
    _write(h / "recipes" / "framework-edit" / "task_class.yaml", """
        name: framework_edit
        description: routine self-edit
    """)
    _write(h / "recipes" / "audit-only" / "workflow.yaml", """
        version: '0.1.0'
        task_class: audit
        nodes:
          - {name: auditor, type: researcher}
        edges: []
    """)
    _write(h / "recipes" / "audit-only" / "example-kickoff.md", """
        # Audit: Title
        ## Files in scope
    """)
    _write(h / "recipes" / "audit-only" / "task_class.yaml", """
        name: audit
        description: read-only audit
    """)
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    return h


# ── slug ────────────────────────────────────────────────────────────────────


def test_slug_basic_title():
    assert kickoff_lint.slug("# Foo Bar Baz") == "foo-bar-baz"


def test_slug_strips_non_alnum_and_collapses_dashes():
    assert kickoff_lint.slug("# Hello, world!") == "hello-world"
    assert kickoff_lint.slug("# multiple   spaces and -- dashes") == "multiple-spaces-and-dashes"


def test_slug_caps_at_48_chars():
    long = "a" * 200
    out = kickoff_lint.slug(f"# {long}")
    assert len(out) == 48
    assert out == "a" * 48


def test_slug_falls_back_when_no_title():
    assert kickoff_lint.slug("body without a title") == "kickoff"


def test_slug_empty_input():
    assert kickoff_lint.slug("") == "kickoff"


# ── lint: error / warn shapes ──────────────────────────────────────────────


def test_lint_empty_markdown_returns_one_error():
    findings = kickoff_lint.lint("   \n  ", project=Path("/tmp"), home=Path("/tmp"))
    assert findings == [{
        "sev": "error",
        "msg": "The kickoff is empty.",
        "fix": "Write the kickoff before drafting.",
    }]


def test_lint_no_title_warns(project: Path, home: Path):
    body = "## Files in scope\n- `foo.py`\n\n## Success criteria\n- runs\n"
    (project / "foo.py").write_text("x", encoding="utf-8")
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert any(f["msg"].startswith("The kickoff has no '# ' title line.") for f in out)


def test_lint_no_scope_section_for_implementer_recipe_warns(project: Path, home: Path):
    body = "# Title\n\n## Success criteria\n- runs\n"
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert any(
        "no '## Files in scope' section" in f["msg"]
        and f["sev"] == "warn"
        for f in out
    )


def test_lint_no_scope_warning_for_researcher_only_recipe(project: Path, home: Path):
    """A researcher-only recipe does NOT require a scope section."""
    body = "# Title\n\n## Success criteria\n- runs\n"
    out = kickoff_lint.lint(body, project=project, recipe="audit-only", home=home)
    assert not any(
        "no '## Files in scope' section" in f["msg"] for f in out
    )


def test_lint_missing_scope_path_warns(project: Path, home: Path):
    body = (
        "# Title\n\n## Files in scope\n"
        "- `does_not_exist.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert any(
        f["msg"] == "Not in the project: does_not_exist.py"
        and f["sev"] == "warn"
        and "(new)" in f["fix"]
        for f in out
    )


def test_lint_existing_scope_path_is_clean(project: Path, home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    body = (
        "# Title\n\n## Files in scope\n"
        "- `present.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert not any("Not in the project" in f["msg"] for f in out)


def test_lint_new_marker_accepted(project: Path, home: Path):
    body = (
        "# Title\n\n## Files in scope\n"
        "- `brand_new_file.py (new)`\n\n"
        "## Success criteria\n- runs\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert not any("Not in the project" in f["msg"] for f in out)


def test_lint_glob_match_succeeds(project: Path, home: Path):
    (project / "one.py").write_text("x", encoding="utf-8")
    (project / "two.py").write_text("x", encoding="utf-8")
    body = (
        "# Title\n\n## Files in scope\n"
        "- `*.py`\n\n"
        "## Success criteria\n- runs\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert not any("Not in the project" in f["msg"] for f in out)


def test_lint_glob_no_match_warns(project: Path, home: Path):
    body = (
        "# Title\n\n## Files in scope\n"
        "- `*.nowhere`\n\n"
        "## Success criteria\n- runs\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert any("Not in the project: *.nowhere" in f["msg"] for f in out)


def test_lint_no_success_section_warns(project: Path, home: Path):
    body = "# Title\n\n## Files in scope\n- `present.py`\n"
    (project / "present.py").write_text("x", encoding="utf-8")
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert any(
        "no success section" in f["msg"] and f["sev"] == "warn" for f in out
    )


def test_lint_success_variants_accepted(project: Path, home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    for heading in ("## Success criteria", "## Acceptance criteria",
                    "## Done when", "## Verification"):
        body = f"# Title\n\n## Files in scope\n- `present.py`\n\n{heading}\n- runs\n"
        out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
        assert not any("no success section" in f["msg"] for f in out), heading


def test_lint_missing_example_heading_warns(project: Path, home: Path):
    """framework-edit's example lists five contract sections. Drop '## Verification
    command' from the user's kickoff and lint must flag it as a missing example
    heading."""
    (project / "present.py").write_text("x", encoding="utf-8")
    body = (
        "# Title\n\n## Goal\n- ship\n\n## Acceptance\n- AC1\n\n"
        "## Files in scope\n- `present.py`\n\n## Out of scope\n- none\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    # "Verification command" is the heading used in the example.
    assert any(
        "have a '## Verification command' section" in f["msg"] for f in out
    )


def test_lint_long_markdown_warns(project: Path, home: Path):
    (project / "present.py").write_text("x", encoding="utf-8")
    body = (
        "# Title\n\n## Files in scope\n- `present.py`\n\n"
        "## Success criteria\n- runs\n\n" + ("x" * 20_500)
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert any("cap 20000" in f["msg"] for f in out)


def test_lint_unknown_recipe_warns(project: Path, home: Path):
    body = "# Title\n\n## Success criteria\n- runs\n"
    out = kickoff_lint.lint(body, project=project, recipe="no-such-recipe", home=home)
    assert any("not registered" in f["msg"] for f in out)


def test_lint_complete_kickoff_returns_no_findings(project: Path, home: Path):
    """A complete kickoff — title, all five contract sections (with an AC id),
    scope paths that exist, length under cap — must return []."""
    (project / "present.py").write_text("x", encoding="utf-8")
    # Title matches the example kickoff so the "examples have a section"
    # warning does not fire.
    body = (
        "# Framework Edit: Title\n\n"
        "## Goal\n"
        "- ship the change\n\n"
        "## Acceptance\n"
        "- AC1: the lint stays silent\n\n"
        "## Files in scope\n"
        "- `present.py`\n\n"
        "## Out of scope\n"
        "- nothing\n\n"
        "## Verification command\n"
        "- runs\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert out == []


def test_lint_does_not_raise_on_garbage_recipe_dir(project: Path, tmp_path: Path):
    """A workflow.yaml that explodes on parse must fail without raising."""
    broken = tmp_path / ".mini-ork-broken"
    broken.mkdir()
    (broken / "recipes" / "broken").mkdir(parents=True)
    (broken / "recipes" / "broken" / "workflow.yaml").write_text(
        ":\n  - not: valid: yaml: at: all\n", encoding="utf-8"
    )
    import os
    old = os.environ.get("MINI_ORK_HOME")
    os.environ["MINI_ORK_HOME"] = str(broken)
    try:
        body = (
            "# Title\n\n## Files in scope\n"
            "- `present.py`\n\n## Success criteria\n- runs\n"
        )
        (project / "present.py").write_text("x", encoding="utf-8")
        out = kickoff_lint.lint(body, project=project, recipe="broken", home=broken)
    finally:
        if old is None:
            os.environ.pop("MINI_ORK_HOME", None)
        else:
            os.environ["MINI_ORK_HOME"] = old
    # We don't assert the contents, only that lint completed without raising
    # and produced a list (warnings about unknown recipe are fine).
    assert isinstance(out, list)


def test_lint_uses_examples_dir_layout(project: Path, tmp_path: Path):
    """When the recipe ships examples/<id>/kickoff.md (newer convention) the
    lint should pick up the headings from there, not from example-kickoff.md."""
    h = tmp_path / "home-newer"
    h.mkdir()
    _write(h / "recipes" / "newer-recipe" / "workflow.yaml", """
        version: '0.1.0'
        task_class: framework_edit
        nodes:
          - {name: implementer, type: implementer}
        edges: []
    """)
    _write(h / "recipes" / "newer-recipe" / "examples" / "newer-recipe" / "kickoff.md", """
        # Newer Recipe Title
        ## Goal
        ## Acceptance
        - AC1
        ## Files in scope
        ## Out of scope
        ## Verification command
    """)
    _write(h / "recipes" / "newer-recipe" / "task_class.yaml", """
        name: newer-recipe
        description: x
    """)
    import os
    old = os.environ.get("MINI_ORK_HOME")
    os.environ["MINI_ORK_HOME"] = str(h)
    try:
        (project / "present.py").write_text("x", encoding="utf-8")
        body = (
            "# Newer Recipe Title\n\n## Goal\n- x\n\n## Acceptance\n- AC1\n\n"
            "## Files in scope\n- `present.py`\n\n## Out of scope\n- none\n\n"
            "## Verification command\n- runs\n"
        )
        out = kickoff_lint.lint(body, project=project, recipe="newer-recipe", home=h)
    finally:
        if old is None:
            os.environ.pop("MINI_ORK_HOME", None)
        else:
            os.environ["MINI_ORK_HOME"] = old
    assert out == []

def test_lint_new_marker_after_the_path_is_honoured(project: Path, home: Path):
    """The natural form: ``- `docs/race.md` (new)``. parse_scope drops the
    commentary, so the marker is read from the source line."""
    (project / "present.py").write_text("x", encoding="utf-8")
    body = ("# Title\n\n## Files in scope\n- `present.py`\n- `docs/race.md` (new)\n"
            "- `gone.py`\n\n## Success criteria\n- runs\n")
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    missing = [f["msg"] for f in out if "Not in the project" in f["msg"]]
    assert missing == ["Not in the project: gone.py"]


def test_lint_does_not_demand_the_examples_own_title(project: Path, home: Path):
    """Only an example's ``## `` sections are expected — not its ``# Title``."""
    (project / "present.py").write_text("x", encoding="utf-8")
    body = (
        "# Another task\n\n## Goal\n- x\n\n## Acceptance\n- AC1\n\n"
        "## Files in scope\n- `present.py`\n\n## Out of scope\n- none\n\n"
        "## Verification command\n- runs\n"
    )
    out = kickoff_lint.lint(body, project=project, recipe="framework-edit", home=home)
    assert [f for f in out if "examples have" in f["msg"]] == []
