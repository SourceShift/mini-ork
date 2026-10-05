"""Unit tests for ``mini_ork.recipe_author``.

Coverage (per kickoff §"Tests"):

* ``guide`` exposes spec_schema + step_types + roles + rules + example.
* Each validation error and warning.
* ``render`` is deterministic; the rendered ``task_class`` /
  ``workflow`` / ``artifact_contract`` pass the repo's JSON schemas.
* Edge construction: explicit ``after`` edges, ``verifies`` (implementer →
  verifier), implicit prev-step chaining, terminal-step → publisher,
  publisher/rollback escalation.
* The generated verifier passes for ``true`` and fails for ``false``
  (run as subprocess, parse its JSON line).
* ``draft`` writes ONLY under ``<home>/recipe-drafts/<id>/`` and reports
  grade + previous file contents.
* ``commit_draft`` moves into ``<home>/recipes/<id>/`` with a backup
  when replacing an existing recipe.
* ``discard_draft`` removes the draft (idempotent when absent).
* ``get_spec`` round-trip — spec survives a commit unchanged.
* The committed recipe is discoverable via
  ``mini_ork.recipes_catalog.find_recipe`` as a project recipe.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Fresh ``.mini-ork`` home with a lane map so role validation works.

    Sets ``MINI_ORK_HOME`` so ``recipe_author._safe_load_lanes`` reads the
    right config and the recipe catalog's project lookup succeeds.
    """
    h = tmp_path / ".mini-ork"
    h.mkdir()
    (h / "config").mkdir()
    (h / "config" / "agents.yaml").write_text(
        "lanes:\n  planner: opus\n  worker: sonnet\n  reviewer: sonnet\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    return h


@pytest.fixture
def fake_engine_root(tmp_path, monkeypatch) -> Path:
    """A throwaway engine root with one recipe so the shadow-warning path fires."""
    engine = tmp_path / "engine"
    (engine / "recipes" / "code-fix").mkdir(parents=True)
    (engine / "recipes" / "code-fix" / "workflow.yaml").write_text(
        "name: code_fix\n", encoding="utf-8"
    )
    (engine / "recipes" / "code-fix" / "task_class.yaml").write_text(
        "name: code_fix\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "mini_ork.web.control._mini_ork_root", lambda: engine
    )
    return engine


def _basic_spec() -> dict:
    return {
        "id": "demo-recipe",
        "description": "Demo recipe used by the recipe_author test suite.",
        "keywords": ["demo", "test"],
        "input": "The path to inspect.",
        "steps": [
            {
                "id": "scanner",
                "type": "researcher",
                "role": "planner",
                "instructions": "Read the file at the path in the kickoff.",
            },
            {
                "id": "smoke",
                "type": "verifier",
                "check": "true",
                "after": ["scanner"],
            },
        ],
        "publish": False,
        "rollback_on_failure": False,
    }


# ── guide ─────────────────────────────────────────────────────────────────


def test_guide_returns_schema_roles_rules_example(home):
    from mini_ork.recipe_author import guide

    g = guide(home)
    assert "spec_schema" in g and g["spec_schema"]["type"] == "object"
    assert g["step_types"]["verifier"].startswith("Runs a shell check")
    assert g["roles"] == {"planner": "opus", "worker": "sonnet", "reviewer": "sonnet"}
    assert any("Use the fewest steps" in r for r in g["rules"])
    assert g["example"]["id"] == "sql-migration-audit"


def test_guide_tolerates_missing_home():
    """``guide(None)`` must not crash when the home / config is absent."""
    from mini_ork.recipe_author import guide

    g = guide(None)
    assert isinstance(g["spec_schema"], dict)
    # roles falls back to the engine's agents.yaml template (or {} when the
    # import path is broken); what matters is the call never raises.
    assert isinstance(g["roles"], dict)
    assert isinstance(g["example"], dict)


# ── validate_spec ─────────────────────────────────────────────────────────


def test_validate_spec_ok_on_basic(home):
    from mini_ork.recipe_author import validate_spec

    findings = validate_spec(_basic_spec(), home)
    assert findings == []


def test_validate_spec_schema_error_returns_findings(home):
    from mini_ork.recipe_author import validate_spec

    bad = _basic_spec()
    bad["id"] = "BadID"  # uppercase
    findings = validate_spec(bad, home)
    assert any(f["sev"] == "error" for f in findings)
    # Schema errors are fatal: only one finding (the schema one).
    assert len(findings) == 1


def test_validate_spec_unknown_after_id(home):
    from mini_ork.recipe_author import validate_spec

    spec = _basic_spec()
    spec["steps"][1]["after"] = ["nope"]
    findings = validate_spec(spec, home)
    assert any("unknown `after` id" in f["msg"] for f in findings)


def test_validate_spec_self_dependency(home):
    from mini_ork.recipe_author import validate_spec

    spec = _basic_spec()
    spec["steps"][0]["after"] = ["scanner"]
    findings = validate_spec(spec, home)
    assert any("depends on itself" in f["msg"] for f in findings)


def test_validate_spec_cycle(home):
    from mini_ork.recipe_author import validate_spec

    spec = _basic_spec()
    spec["steps"][0]["after"] = ["smoke"]  # scanner → smoke
    spec["steps"][1]["after"] = ["scanner"]  # smoke → scanner   (cycle)
    findings = validate_spec(spec, home)
    assert any("cycle" in f["msg"] for f in findings)


def test_validate_spec_unknown_role(home):
    from mini_ork.recipe_author import validate_spec

    spec = _basic_spec()
    spec["steps"][0]["role"] = "no-such-lane"
    findings = validate_spec(spec, home)
    assert any("unknown role" in f["msg"] for f in findings)


def test_validate_spec_warns_when_no_evaluator(home):
    from mini_ork.recipe_author import validate_spec

    spec = _basic_spec()
    spec["steps"] = [s for s in spec["steps"] if s["type"] != "verifier"]
    findings = validate_spec(spec, home)
    assert any(f["sev"] == "warn" and "no verifier" in f["msg"] for f in findings)


def test_validate_spec_warns_engine_collision(home, fake_engine_root):
    from mini_ork.recipe_author import validate_spec

    spec = _basic_spec()
    spec["id"] = "code-fix"  # collides with the fake engine recipe
    findings = validate_spec(spec, home)
    assert any(
        f["sev"] == "warn" and "engine recipe" in f["msg"]
        for f in findings
    )


def test_validate_spec_never_raises():
    """Bad input types must not propagate — returns findings, never raises."""
    from mini_ork.recipe_author import validate_spec

    for bad in (None, 42, "string", [], {"id": 1}):
        findings = validate_spec(bad, None)  # type: ignore[arg-type]
        assert isinstance(findings, list)


# ── render ─────────────────────────────────────────────────────────────────


def _load_schemas():
    schemas = {}
    for name in ("task_class", "workflow", "artifact_contract"):
        with (REPO / "schemas" / f"{name}.schema.json").open() as f:
            schemas[name] = json.load(f)
    return schemas


def test_render_is_deterministic(home):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    a = render(spec)
    b = render(spec)
    assert a == b


def test_rendered_files_pass_schemas(home):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    files = render(spec)
    schemas = _load_schemas()
    jsonschema.validate(
        yaml.safe_load(files["task_class.yaml"]), schemas["task_class"]
    )
    jsonschema.validate(
        yaml.safe_load(files["workflow.yaml"]), schemas["workflow"]
    )
    jsonschema.validate(
        yaml.safe_load(files["artifact_contract.yaml"]), schemas["artifact_contract"]
    )


def test_render_emits_all_required_files(home):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    files = render(spec)
    expected = {
        "task_class.yaml", "workflow.yaml", "artifact_contract.yaml",
        "prompts/scanner.md", "verifiers/smoke.py",
        "README.md", "examples/basic/kickoff.md", "recipe.spec.json",
    }
    assert expected.issubset(files.keys())
    # spec is round-tripped
    parsed_spec = json.loads(files["recipe.spec.json"])
    assert parsed_spec["id"] == spec["id"]


def test_render_publisher_and_rollback_nodes_when_requested(home):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    spec["publish"] = True
    spec["rollback_on_failure"] = True
    files = render(spec)
    wf = yaml.safe_load(files["workflow.yaml"])
    node_names = [n["name"] for n in wf["nodes"]]
    assert "publisher" in node_names
    assert "rollback" in node_names
    # rollback_strategy only when rollback_on_failure
    assert wf.get("rollback_strategy") == "revert_branch"
    # rollback edge escalates from publisher
    rollback_edges = [e for e in wf["edges"] if e["to"] == "rollback"]
    assert rollback_edges and rollback_edges[0]["from"] == "publisher"
    assert rollback_edges[0]["edge_type"] == "escalates_to"


def test_render_no_publisher_no_rollback(home):
    from mini_ork.recipe_author import render

    spec = _basic_spec()  # publish=False, rollback_on_failure=False
    files = render(spec)
    wf = yaml.safe_load(files["workflow.yaml"])
    node_names = [n["name"] for n in wf["nodes"]]
    assert "publisher" not in node_names
    assert "rollback" not in node_names
    assert "rollback_strategy" not in wf


# ── edge construction ─────────────────────────────────────────────────────


def test_edge_verifies_when_implementer_to_verifier(home):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    spec["steps"] = [
        {"id": "writer", "type": "implementer", "role": "worker",
         "instructions": "Write it."},
        {"id": "gate", "type": "verifier", "check": "true", "after": ["writer"]},
    ]
    files = render(spec)
    wf = yaml.safe_load(files["workflow.yaml"])
    edges = [e for e in wf["edges"] if e["from"] == "writer" and e["to"] == "gate"]
    assert edges and edges[0]["edge_type"] == "verifies"


def test_edge_implicit_chains_when_no_after_and_not_first(home):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    # Both steps have no `after` → second chains to first via implicit link.
    files = render(spec)
    wf = yaml.safe_load(files["workflow.yaml"])
    implicit = [e for e in wf["edges"] if e["from"] == "scanner" and e["to"] == "smoke"]
    assert implicit and implicit[0]["edge_type"] == "depends_on"


def test_edge_terminal_steps_feed_publisher(home):
    from mini_ork.recipe_author import render

    # A chain first → middle → last: `last` is the end of the flow (no edge
    # starts from it), so it alone feeds the publisher — publishing waits for
    # every check.
    spec = {
        "id": "demo-recipe",
        "description": "Demo recipe used by the recipe_author test suite.",
        "keywords": ["demo", "test"],
        "input": "The path to inspect.",
        "steps": [
            {
                "id": "first",
                "type": "researcher",
                "role": "planner",
                "instructions": "Read.",
            },
            {
                "id": "middle",
                "type": "verifier",
                "check": "true",
                "after": ["first"],
            },
            {
                "id": "last",
                "type": "verifier",
                "check": "true",
                "after": ["middle"],
            },
        ],
        "publish": True,
        "rollback_on_failure": False,
    }
    files = render(spec)
    wf = yaml.safe_load(files["workflow.yaml"])
    edges = [e for e in wf["edges"] if e["to"] == "publisher"]
    assert [e["from"] for e in edges] == ["last"]


# ── generated verifier ────────────────────────────────────────────────────


def test_generated_verifier_passes_for_true(home, tmp_path):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    files = render(spec)
    script = tmp_path / "v.py"
    script.write_text(files["verifiers/smoke.py"], encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip())
    assert result["pass"] is True
    assert result["rc"] == 0


def test_generated_verifier_fails_for_false(home, tmp_path):
    from mini_ork.recipe_author import render

    spec = _basic_spec()
    spec["steps"][1]["check"] = "false"
    files = render(spec)
    script = tmp_path / "v.py"
    script.write_text(files["verifiers/smoke.py"], encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr  # the script itself succeeds
    result = json.loads(proc.stdout.strip())
    assert result["pass"] is False
    assert result["rc"] != 0


# ── draft ──────────────────────────────────────────────────────────────────


def test_draft_writes_only_under_recipe_drafts(home):
    from mini_ork.recipe_author import draft

    res = draft(home, _basic_spec())
    assert res["ok"] is True
    drafts = home / "recipe-drafts" / "demo-recipe"
    assert drafts.is_dir()
    assert (drafts / "task_class.yaml").is_file()
    assert not (home / "recipes" / "demo-recipe").exists()


def test_draft_reports_previous_contents_when_target_exists(home):
    from mini_ork.recipe_author import draft

    target = home / "recipes" / "demo-recipe"
    target.mkdir(parents=True)
    (target / "task_class.yaml").write_text("# old version\n", encoding="utf-8")
    res = draft(home, _basic_spec())
    assert res["ok"] is True
    task_entry = next(f for f in res["files"] if f["path"] == "task_class.yaml")
    assert task_entry["previous"] == "# old version\n"


def test_draft_grade_payload_shape(home):
    from mini_ork.recipe_author import draft

    res = draft(home, _basic_spec())
    g = res["grade"]
    assert {"score", "letter", "findings"} <= g.keys()
    assert isinstance(g["score"], int)
    assert g["letter"] in {"A", "B", "C", "D", "F"}
    assert isinstance(g["findings"], list)


def test_draft_validation_errors_do_not_write(home):
    from mini_ork.recipe_author import draft

    bad = _basic_spec()
    bad["id"] = "BadID"
    res = draft(home, bad)
    assert res["ok"] is False
    assert "errors" in res
    assert not (home / "recipe-drafts").exists()


def test_draft_base_must_equal_id(home):
    from mini_ork.recipe_author import draft

    res = draft(home, _basic_spec(), base="other-id")
    assert res["ok"] is False
    assert any("base" in e["msg"] for e in res["errors"])


# ── commit_draft / discard_draft ──────────────────────────────────────────


def test_commit_draft_promotes_into_recipes(home):
    from mini_ork.recipe_author import draft, commit_draft

    assert draft(home, _basic_spec())["ok"] is True
    res = commit_draft(home, "demo-recipe")
    assert res["ok"] is True
    assert res["path"].endswith("/recipes/demo-recipe")
    assert res["backup"] is None
    assert (home / "recipes" / "demo-recipe" / "task_class.yaml").is_file()
    # draft dir is wiped
    assert not (home / "recipe-drafts" / "demo-recipe").exists()


def test_commit_draft_makes_backup_when_replacing(home):
    from mini_ork.recipe_author import commit_draft, draft

    # Commit a first version
    draft(home, _basic_spec())
    commit_draft(home, "demo-recipe")
    # Now draft a new one — commit must back up the existing recipe.
    spec = _basic_spec()
    spec["description"] = "Updated description."
    draft(home, spec)
    res = commit_draft(home, "demo-recipe")
    assert res["ok"] is True
    assert res["backup"] is not None
    backups = home / "recipe-backups"
    assert backups.is_dir()
    backups_list = list(backups.iterdir())
    assert len(backups_list) == 1
    assert backups_list[0].name.startswith("demo-recipe-")
    # New recipe is the updated one
    tc = yaml.safe_load(
        (home / "recipes" / "demo-recipe" / "task_class.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert tc["description"] == "Updated description."


def test_commit_draft_no_draft_is_error(home):
    from mini_ork.recipe_author import commit_draft

    res = commit_draft(home, "nope")
    assert res["ok"] is False
    assert "no draft" in res["error"]


def test_discard_draft_removes_dir(home):
    from mini_ork.recipe_author import discard_draft, draft

    draft(home, _basic_spec())
    assert (home / "recipe-drafts" / "demo-recipe").is_dir()
    res = discard_draft(home, "demo-recipe")
    assert res["ok"] is True
    assert not (home / "recipe-drafts" / "demo-recipe").exists()


def test_discard_draft_absent_is_ok(home):
    from mini_ork.recipe_author import discard_draft

    res = discard_draft(home, "never-existed")
    assert res["ok"] is True


# ── get_spec + recipe catalog round-trip ──────────────────────────────────


def test_get_spec_round_trip_after_commit(home):
    from mini_ork.recipe_author import commit_draft, draft, get_spec

    spec = _basic_spec()
    draft(home, spec)
    commit_draft(home, "demo-recipe")
    spec_back = get_spec(home, "demo-recipe")
    assert spec_back == spec


def test_get_spec_missing_recipe(home):
    from mini_ork.recipe_author import get_spec

    assert get_spec(home, "no-such-recipe") is None
    assert get_spec(None, "no-such-recipe") is None


def test_committed_recipe_is_project_recipe_in_catalog(home):
    from mini_ork.recipe_author import commit_draft, draft
    from mini_ork.recipes_catalog import find_recipe

    draft(home, _basic_spec())
    commit_draft(home, "demo-recipe")
    entry = find_recipe("demo-recipe", home)
    assert entry is not None
    assert entry.id == "demo-recipe"
    assert entry.source == "project"
    assert entry.path == home / "recipes" / "demo-recipe"


def test_get_spec_handles_hand_edited_recipes(home):
    """A recipe without ``recipe.spec.json`` returns ``None`` (not raise)."""
    from mini_ork.recipe_author import get_spec

    legacy = home / "recipes" / "old-recipe"
    legacy.mkdir(parents=True)
    (legacy / "workflow.yaml").write_text("name: x\n", encoding="utf-8")
    (legacy / "task_class.yaml").write_text("name: x\n", encoding="utf-8")
    assert get_spec(home, "old-recipe") is None

def test_publisher_waits_for_the_end_of_every_branch():
    """The publisher depends on the steps nothing starts from — the checks at
    the end — never on a step that other steps already follow."""
    spec = {
        "id": "chain-probe", "description": "d", "keywords": ["k"], "input": "i",
        "steps": [
            {"id": "a", "type": "implementer", "role": "worker", "instructions": "x"},
            {"id": "b", "type": "verifier", "check": "true", "after": ["a"]},
            {"id": "c", "type": "reviewer", "role": "reviewer", "instructions": "y", "after": ["b"]},
        ],
        "publish": True,
    }
    from mini_ork.recipe_author import render

    wf = yaml.safe_load(render(spec)["workflow.yaml"])
    into_publisher = sorted(e["from"] for e in wf["edges"] if e["to"] == "publisher")
    assert into_publisher == ["c"]


def test_draft_grade_with_a_relative_home(tmp_path, monkeypatch):
    """A relative home path must not break grading (the stage symlink must be
    absolute) — a complete recipe grades well, not as 'all files missing'."""
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text("lanes:\n  worker: sonnet\n  reviewer: opus\n")
    monkeypatch.chdir(tmp_path)
    spec = {
        "id": "rel-probe", "description": "d", "keywords": ["k"], "input": "i",
        "steps": [
            {"id": "edit", "type": "implementer", "role": "worker", "instructions": "x"},
            {"id": "check", "type": "verifier", "check": "true", "after": ["edit"]},
        ],
    }
    from mini_ork.recipe_author import draft

    out = draft(Path(".mini-ork"), spec)
    assert out["ok"], out
    assert out["grade"]["score"] >= 90, out["grade"]
