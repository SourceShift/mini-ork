"""Skill grants as the IDE pages render them.

Two surfaces claim to show grants: the run's node detail (a "Skills" kv row
reading the workflow snapshot the run carries) and the recipe page (the
flow lane string + a contract line per granting node). Both read the same
``skills:`` declaration the executor materializes at dispatch, so a run's
page can never disagree with what the node actually got — unless these
readers drift from workflow.yaml, which is what the assertions pin.
"""

from __future__ import annotations

from pathlib import Path

from mini_ork.ide_pages.node import _declared_skills, _kv_items
from mini_ork.ide_pages.recipes import _recipe_sections
from mini_ork.ide_pages.run import Node, Run


def _recipe_with_skills(tmp_path: Path) -> Path:
    """A recipe dir whose workflow grants one node two skills."""
    wf = tmp_path / "recipes" / "skill-demo"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "nodes:\n"
        "  - name: granted_lens\n"
        "    type: researcher\n"
        "    model_lane: opus_lens\n"
        "    skills: [alpha-skill, beta-skill]\n"
        "  - name: control_lens\n"
        "    type: researcher\n"
        "    model_lane: opus_lens\n"
        "  - name: csv_grants\n"
        "    type: researcher\n"
        "    skills: gamma-skill, delta-skill\n",
        encoding="utf-8",
    )
    (wf / "artifact_contract.yaml").write_text(
        "source_artifact: out.md\noutputs: []\nsuccess_verifiers: []\n",
        encoding="utf-8",
    )
    (wf / "task_class.yaml").write_text(
        'name: skill_demo\nmatches: {keywords: [], path_globs: [], regex: []}\n',
        encoding="utf-8",
    )
    return wf


def _run(recipe_dir: Path | None) -> Run:
    return Run(
        id="run-x",
        home=Path("/tmp"),
        run_dir=Path("/tmp/run-x"),
        row={},
        card={},
        nodes=[],
        cols=[],
        calls=[],
        recipe_dir=recipe_dir,
        workspace=None,
    )


def test_declared_skills_reads_the_workflow_snapshot(tmp_path: Path) -> None:
    recipe = _recipe_with_skills(tmp_path)
    run = _run(recipe)

    assert _declared_skills(run, "granted_lens") == ["alpha-skill", "beta-skill"]
    # The CSV form a hand-edited workflow uses parses the same way.
    assert _declared_skills(run, "csv_grants") == ["gamma-skill", "delta-skill"]
    # A node without a grant is an empty list, not an error.
    assert _declared_skills(run, "control_lens") == []
    # Unknown node and no snapshot both degrade to empty.
    assert _declared_skills(run, "ghost") == []
    assert _declared_skills(_run(None), "granted_lens") == []


def test_node_detail_carries_a_skills_row(tmp_path: Path) -> None:
    recipe = _recipe_with_skills(tmp_path)
    node = Node(id="granted_lens", type="researcher", role_lane="opus_lens")
    items = _kv_items(node, "12s", _run(recipe))

    skills = next(i for i in items if i["k"] == "Skills")
    assert skills["v"] == "alpha-skill, beta-skill"
    assert skills["c"] == "text"  # a grant reads as a fact, not a muted —

    control = next(
        i for i in _kv_items(Node(id="control_lens"), "5s", _run(recipe)) if i["k"] == "Skills"
    )
    assert control["v"] == "—" and control["c"] == "muted"


def test_recipe_page_lists_the_grants(tmp_path: Path) -> None:
    _recipe_with_skills(tmp_path)
    rows = [{"id": "skill-demo", "source": "project", "runs": 0}]
    sections = _recipe_sections(tmp_path, {"recipe": "skill-demo"}, {}, rows)

    rendered = str(sections)
    # Flow lane string names the skills next to the lane…
    assert "skills: alpha-skill, beta-skill" in rendered
    # …and the contract block has one line per granting node.
    assert "granted_lens grants skills" in rendered
    assert "csv_grants grants skills" in rendered
    assert "control_lens grants skills" not in rendered
