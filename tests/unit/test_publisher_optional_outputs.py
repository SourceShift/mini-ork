"""Publisher optional/green-path run-local outputs.

Pins the two green-run shapes a strict required-only check got wrong —
first exercised by the first all-green SDD child
(run-1791790859-83222--p1-5-parity-suite-and-docs--D1, 2026-10-12): every
gate APPROVED, yet the publisher failed "3 of 8 outputs" because

- ``reflector.json`` / ``replan.json`` are failure-loop artifacts a green
  pass legitimately never writes, and
- ``tier4-{family}.md`` is a template path naming a FAMILY of files
  (tier4-glm.md, tier4-kimi.md, ...) — the literal brace path never exists.

Covered:
- ``_outputs_from_contract`` — string entries and flag-less mappings stay
  required=True (fail-closed default; existing recipes unchanged); only an
  explicit ``required: false`` opts out.
- ``_run_local_output_status`` — present / glob_present / optional_absent /
  missing classification, including the back-compat required-missing fail.

Hermetic: pure functions + tmp_path; no db, no LLM, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli.publisher import (  # noqa: E402
    _outputs_from_contract,
    _run_local_output_status,
)


# ── _outputs_from_contract ──────────────────────────────────────────────────


def test_string_entry_is_required():
    assert _outputs_from_contract({"outputs": ["synthesis.md"]}) == [("synthesis.md", True)]


def test_mapping_without_flag_defaults_required():
    d = {"outputs": [{"path": "panel-verdict.json", "mime": "application/json"}]}
    assert _outputs_from_contract(d) == [("panel-verdict.json", True)]


def test_explicit_required_true_stays_required():
    d = {"outputs": [{"path": "a.json", "required": True}]}
    assert _outputs_from_contract(d) == [("a.json", True)]


def test_required_false_opts_out():
    d = {"outputs": [{"path": "reflector.json", "required": False}]}
    assert _outputs_from_contract(d) == [("reflector.json", False)]


def test_pathless_mapping_is_dropped():
    d = {"outputs": [{"mime": "application/json"}, "ok.md"]}
    assert _outputs_from_contract(d) == [("ok.md", True)]


def test_empty_outputs():
    assert _outputs_from_contract({}) == []
    assert _outputs_from_contract({"outputs": []}) == []


# ── _run_local_output_status ────────────────────────────────────────────────


def test_present_file(tmp_path):
    f = tmp_path / "panel-verdict.json"
    f.write_text("{}")
    assert _run_local_output_status(str(f), "panel-verdict.json", str(tmp_path)) == "present"


def test_empty_file_is_optional_absent(tmp_path):
    """required:false means the artifact is not demanded at all — an empty
    stub is as good as absent. (A REQUIRED artifact that is empty still
    fails: the getsize>0 guard falls through to missing.)"""
    f = tmp_path / "reflector.json"
    f.write_text("")
    assert (
        _run_local_output_status(str(f), "reflector.json", str(tmp_path), required=False) == "optional_absent"
    )


def test_optional_absent(tmp_path):
    dst = tmp_path / "reflector.json"
    assert (
        _run_local_output_status(str(dst), "reflector.json", str(tmp_path), required=False)
        == "optional_absent"
    )


def test_required_absent_still_fails(tmp_path):
    """Back-compat: no flag (required=True) + absent → missing (fail closed)."""
    dst = tmp_path / "reflector.json"
    assert _run_local_output_status(str(dst), "reflector.json", str(tmp_path)) == "missing"


def test_template_family_glob_matches(tmp_path):
    """tier4-{family}.md matches the concrete tier4-<lens>.md files."""
    for lens in ("glm", "kimi"):
        (tmp_path / f"tier4-{lens}.md").write_text("## tier4 review\n")
    dst = tmp_path / "tier4-{family}.md"  # literal — never exists
    assert _run_local_output_status(str(dst), "tier4-{family}.md", str(tmp_path)) == "glob_present"


def test_template_family_glob_no_match_fails(tmp_path):
    dst = tmp_path / "tier4-{family}.md"
    assert _run_local_output_status(str(dst), "tier4-{family}.md", str(tmp_path)) == "missing"


def test_template_glob_with_required_false_prefers_optional(tmp_path):
    """Glob miss + optional → optional_absent (no family emitted, none needed)."""
    dst = tmp_path / "tier4-{family}.md"
    assert (
        _run_local_output_status(str(dst), "tier4-{family}.md", str(tmp_path), required=False)
        == "optional_absent"
    )


def test_nonempty_dir_is_present(tmp_path):
    d = tmp_path / "spec-cards"
    d.mkdir()
    (d / "a.json").write_text("{}")
    assert _run_local_output_status(str(d), "spec-cards", str(tmp_path)) == "present"


def test_angle_bracket_template_in_a_subdirectory_matches(tmp_path):
    """spec-driven-delivery's contract names ``spec-cards/<spec_id>.json``: the
    family lives in a subdirectory and the placeholder uses angle brackets.
    Treating it as a literal path failed run-1791884666-31084's publisher after
    all four of its deliverables had delivered."""
    cards = tmp_path / "spec-cards"
    cards.mkdir()
    for spec in ("b1-recipe", "b2-runner"):
        (cards / f"{spec}.json").write_text("{}")
    dst = cards / "<spec_id>.json"  # literal — never exists
    assert _run_local_output_status(
        str(dst), "${MINI_ORK_RUN_DIR}/spec-cards/<spec_id>.json", str(tmp_path)) == "glob_present"


def test_angle_bracket_template_with_no_match_still_fails(tmp_path):
    (tmp_path / "spec-cards").mkdir()
    dst = tmp_path / "spec-cards" / "<spec_id>.json"
    assert _run_local_output_status(
        str(dst), "${MINI_ORK_RUN_DIR}/spec-cards/<spec_id>.json", str(tmp_path)) == "missing"


def test_a_template_matches_only_its_own_directory(tmp_path):
    """A matching name at the run dir's top level does not satisfy a template
    that names a subdirectory."""
    (tmp_path / "b1-recipe.json").write_text("{}")
    (tmp_path / "gates").mkdir()
    dst = tmp_path / "gates" / "<spec_id>.json"
    assert _run_local_output_status(
        str(dst), "${MINI_ORK_RUN_DIR}/gates/<spec_id>.json", str(tmp_path)) == "missing"
