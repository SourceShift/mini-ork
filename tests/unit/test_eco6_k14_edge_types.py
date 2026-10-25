"""ECO6 K14 — five decorative ``edge_type`` values become ``depends_on``.

``schemas/workflow.schema.json`` accepts eight ``edge_type`` names, but the
compiler gives runtime meaning to three: ``depends_on`` (ordering parent plus
an optional ``from_output``/``to_input`` artifact binding), ``retries`` (the
revise loop, ``max_rounds``) and ``escalates_to`` (a repair edge only inside a
``recursion:`` block). ``supplies_context_to``, ``verifies``, ``blocks``,
``human_decision_gate`` and ``verifies_user_choice`` were already compiled
exactly like ``depends_on`` — this suite pins that equivalence, pins the
codemod's byte discipline, and pins the ``validate`` deprecation warnings.

Hermetic: every fixture lives under ``tmp_path``; the corpus-wide equivalence
check needs no network and no credentials.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

import mini_ork

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
CODEMOD = REPO_ROOT / "scripts" / "codemods" / "eco6_collapse_edge_types.py"
SCHEMA_PATH = REPO_ROOT / "schemas" / "workflow.schema.json"
DOCS_PATH = REPO_ROOT / "docs" / "ARCHITECTURE.md"
WORKFLOWS = sorted((REPO_ROOT / "recipes").glob("*/workflow.yaml"))

DEPRECATED_ALIASES = (
    "supplies_context_to",
    "verifies",
    "blocks",
    "human_decision_gate",
    "verifies_user_choice",
)
RUNTIME_EDGE_TYPES = ("depends_on", "retries", "escalates_to")
ALL_EDGE_TYPES = frozenset(DEPRECATED_ALIASES) | frozenset(RUNTIME_EDGE_TYPES)

# `edge_type: <alias>` with a real word boundary, so `verifies` cannot match
# the prefix of `verifies_user_choice`.
_ALIAS_VALUE_RE = re.compile(
    r"edge_type:\s*['\"]?(?:supplies_context_to|verifies|blocks|"
    r"human_decision_gate|verifies_user_choice)\b"
)

# The 42 recipes the codemod rewrote (measured at test-write time with
# `git diff --name-only HEAD -- recipes`). The other four shipped recipes carry
# no alias at all: chapter-validation-10lens, goal-loop, orch-smoke,
# registry-audit.
CHANGED_RECIPES = (
    "recipes/audit-judge-panel-3l/workflow.yaml",
    "recipes/audit-judge-panel-4l/workflow.yaml",
    "recipes/audit-judge-panel/workflow.yaml",
    "recipes/author-style-imitation-review/workflow.yaml",
    "recipes/bdd-first-delivery/workflow.yaml",
    "recipes/blog-cohesion/workflow.yaml",
    "recipes/blog-post/workflow.yaml",
    "recipes/bug-audit-cmgk/workflow.yaml",
    "recipes/bug-audit-fe-be/workflow.yaml",
    "recipes/chapter-review/workflow.yaml",
    "recipes/code-fix/workflow.yaml",
    "recipes/coord-technique-review/workflow.yaml",
    "recipes/db-migration/workflow.yaml",
    "recipes/doc-to-features-loop/workflow.yaml",
    "recipes/docs/workflow.yaml",
    "recipes/epic-runner/workflow.yaml",
    "recipes/feature-inventory-cmgk/workflow.yaml",
    "recipes/framework-edit/workflow.yaml",
    "recipes/frontier-llm-research/workflow.yaml",
    "recipes/harness-bridge/workflow.yaml",
    "recipes/obs-smoke/workflow.yaml",
    "recipes/ops-runbook/workflow.yaml",
    "recipes/orch-smoke-default-on/workflow.yaml",
    "recipes/post-mvp-delivery/workflow.yaml",
    "recipes/prompt-graph-loop/workflow.yaml",
    "recipes/recipe-creator/workflow.yaml",
    "recipes/recursive-self-improve/workflow.yaml",
    "recipes/recursive-validate-impl/workflow.yaml",
    "recipes/refactor-audit/workflow.yaml",
    "recipes/research-synthesis/workflow.yaml",
    "recipes/researcher-qdrant-contract/workflow.yaml",
    "recipes/rsi-technique-review/workflow.yaml",
    "recipes/schema-judge-panel/workflow.yaml",
    "recipes/self-migrate/workflow.yaml",
    "recipes/session-task-judge/workflow.yaml",
    "recipes/silent-catch-audit/workflow.yaml",
    "recipes/skill-smoke/workflow.yaml",
    "recipes/spec-driven-delivery/workflow.yaml",
    "recipes/star-growth-panel/workflow.yaml",
    "recipes/ui-audit/workflow.yaml",
    "recipes/ui-feature-parity/workflow.yaml",
    "recipes/verification-technique-review/workflow.yaml",
)


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_codemod():
    spec = importlib.util.spec_from_file_location("eco6_collapse_edge_types_k14", CODEMOD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compile(text: str, tmp_path: Path, name: str = "workflow.yaml"):
    from mini_ork.workflow import compile_workflow

    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return compile_workflow(path)


def _signature(compiled) -> dict:
    """Everything the rename must leave untouched, in comparable form."""
    return {
        "control_parents": {k: tuple(v) for k, v in compiled.control_parents.items()},
        "retry_edges": {
            k: (v.target, v.max_rounds, repr(v.condition))
            for k, v in compiled.retry_edges.items()
        },
        "escalation_edges": {
            k: (v.target, v.max_rounds, repr(v.condition))
            for k, v in compiled.escalation_edges.items()
        },
        "bindings": tuple(
            (b.producer_node, b.producer_output, b.consumer_node, b.consumer_input)
            for b in compiled.bindings
        ),
    }


def _git_show(rel: str) -> str | None:
    """The committed text of ``rel``, or None when git/history is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _make_recipe_tree(tmp_path: Path, recipe: str, workflow_text: str):
    """A hermetic recipe tree: tmp MINI_ORK_ROOT plus tmp MINI_ORK_HOME."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    recipe_dir = root / "recipes" / recipe
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "task_class.yaml").write_text(f"name: {recipe}\n", encoding="utf-8")
    (recipe_dir / "workflow.yaml").write_text(workflow_text, encoding="utf-8")
    (recipe_dir / "artifact_contract.yaml").write_text("outputs: []\n", encoding="utf-8")
    (root / "config").mkdir(parents=True)
    (root / "config" / "agents.yaml").write_text("lanes: {}\n", encoding="utf-8")
    home.mkdir(parents=True)
    return root, home


def _run_validate(monkeypatch, root: Path, home: Path, *args: str, capsys):
    from mini_ork.cli import validate

    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    rc = validate.main(list(args))
    captured = capsys.readouterr()
    return rc, captured.err, captured.out


# ─────────────────────────────────────────────────────────────────────────────
# codemod: checked in, byte-preserving, all five aliases, idempotent
# ─────────────────────────────────────────────────────────────────────────────

BYTE_FIXTURE = """\
# edges appear inline and in block form; comments must survive
task_class: codemod-probe
nodes:
  - name: implementer
    type: implementer
    description: implements and verifies the change
  - name: typecheck
    type: verifier
    description: runs the typechecker
  - name: reviewer
    type: reviewer
    description: reviews the diff
edges:
  - { from: implementer, to: typecheck, edge_type: verifies }
  - { from: typecheck, to: reviewer, edge_type: depends_on }
  - { from: reviewer, to: implementer, edge_type: retries, max_rounds: 2 }

  - from: typecheck
    to: reviewer
    edge_type: supplies_context_to
"""


def test_codemod_is_checked_in():
    assert CODEMOD.is_file(), f"codemod missing: {CODEMOD}"


def test_codemod_preserves_every_other_byte(tmp_path):
    """Only the two alias ``edge_type`` values change; every other byte stays."""
    original = BYTE_FIXTURE
    target = tmp_path / "workflow.yaml"
    target.write_text(original, encoding="utf-8")

    assert _load_codemod().main([str(target)]) == 0
    updated = target.read_text(encoding="utf-8")

    changed = [
        (left, right)
        for left, right in zip(original.splitlines(), updated.splitlines())
        if left != right
    ]
    assert len(original.splitlines()) == len(updated.splitlines())
    assert changed == [
        (
            "  - { from: implementer, to: typecheck, edge_type: verifies }",
            "  - { from: implementer, to: typecheck, edge_type: depends_on }",
        ),
        (
            "    edge_type: supplies_context_to",
            "    edge_type: depends_on",
        ),
    ]
    # The comment, the node description containing the word `verifies`, and the
    # non-alias edges are byte-identical.
    assert "# edges appear inline and in block form" in updated
    assert "implements and verifies the change" in updated
    assert "edge_type: retries, max_rounds: 2" in updated


QUOTED_BYTE_FIXTURE = """\
# a quoted alias must lose both quotes, never leave a dangling one
task_class: codemod-quoted-probe
nodes:
  - { name: implementer, type: implementer }
  - { name: reviewer, type: reviewer }
  - { name: publisher, type: publisher }
edges:
  - { from: implementer, to: reviewer, edge_type: "verifies" }
  - from: reviewer
    to: publisher
    edge_type: 'human_decision_gate'
  - { from: implementer, to: publisher, edge_type: "depends_on" }
"""


def test_codemod_consumes_quotes_around_a_quoted_alias(tmp_path):
    """A quoted alias collapses to a bare value; no orphan quote survives."""
    target = tmp_path / "workflow.yaml"
    target.write_text(QUOTED_BYTE_FIXTURE, encoding="utf-8")

    assert _load_codemod().main([str(target)]) == 0
    updated = target.read_text(encoding="utf-8")

    expected = QUOTED_BYTE_FIXTURE.replace(
        'edge_type: "verifies"', "edge_type: depends_on"
    ).replace("edge_type: 'human_decision_gate'", "edge_type: depends_on")
    assert updated == expected
    assert 'edge_type: depends_on"' not in updated
    assert "edge_type: depends_on'" not in updated
    # A quoted non-alias value is not in the alias set and stays byte-identical.
    assert 'edge_type: "depends_on"' in updated


def test_codemod_rewrites_all_five_aliases_without_prefix_corruption(tmp_path):
    fixture = (
        "nodes:\n"
        "  - { name: planner, type: planner }\n"
        "  - { name: implementer, type: implementer }\n"
        "  - { name: reviewer, type: reviewer }\n"
        "  - { name: gate, type: publisher }\n"
        "  - { name: publisher, type: publisher }\n"
        "edges:\n"
        "  - { from: planner, to: implementer, edge_type: supplies_context_to }\n"
        "  - { from: planner, to: reviewer, edge_type: verifies }\n"
        "  - { from: implementer, to: reviewer, edge_type: blocks }\n"
        "  - { from: reviewer, to: gate, edge_type: human_decision_gate }\n"
        "  - { from: gate, to: publisher, edge_type: verifies_user_choice }\n"
        "  - { from: reviewer, to: publisher, edge_type: depends_on }\n"
    )
    target = tmp_path / "workflow.yaml"
    target.write_text(fixture, encoding="utf-8")

    assert _load_codemod().main([str(target)]) == 0
    updated = target.read_text(encoding="utf-8")

    assert updated == _ALIAS_VALUE_RE.sub("edge_type: depends_on", fixture)
    assert "depends_on_user_choice" not in updated
    assert updated.count("edge_type: depends_on") == 6
    assert not _ALIAS_VALUE_RE.search(updated)


def test_codemod_is_idempotent(tmp_path):
    target = tmp_path / "workflow.yaml"
    target.write_text(BYTE_FIXTURE, encoding="utf-8")
    module = _load_codemod()

    assert module.main([str(target)]) == 0
    once = target.read_text(encoding="utf-8")
    assert module.main([str(target)]) == 0
    assert target.read_text(encoding="utf-8") == once
    assert module.collapse_edge_types(once) == once


def test_codemod_leaves_alias_free_files_untouched(tmp_path):
    original = (
        "nodes:\n"
        "  - { name: implementer, type: implementer }\n"
        "  - { name: reviewer, type: reviewer }\n"
        "edges:\n"
        "  - { from: implementer, to: reviewer, edge_type: depends_on }\n"
        "  - { from: reviewer, to: implementer, edge_type: retries, max_rounds: 2 }\n"
    )
    target = tmp_path / "workflow.yaml"
    target.write_text(original, encoding="utf-8")

    assert _load_codemod().main([str(target)]) == 0
    assert target.read_text(encoding="utf-8") == original


# ─────────────────────────────────────────────────────────────────────────────
# shipped corpus: no aliases, schema-valid, compilable
# ─────────────────────────────────────────────────────────────────────────────

def test_recipe_corpus_is_present():
    assert len(WORKFLOWS) >= 10, f"recipe corpus unexpectedly small: {len(WORKFLOWS)}"


@pytest.mark.parametrize("wf_path", WORKFLOWS, ids=[p.parent.name for p in WORKFLOWS])
def test_no_shipped_recipe_uses_a_deprecated_alias(wf_path):
    rel = wf_path.relative_to(REPO_ROOT).as_posix()
    document = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
    offenders = [
        f"{edge.get('from')} -> {edge.get('to')} ({edge.get('edge_type')})"
        for edge in document.get("edges") or []
        if edge.get("edge_type") in DEPRECATED_ALIASES
    ]
    assert not offenders, (
        f"{rel} still uses deprecated edge_type(s): {', '.join(offenders)}"
    )


@pytest.mark.parametrize("wf_path", WORKFLOWS, ids=[p.parent.name for p in WORKFLOWS])
def test_recipe_validates_against_workflow_schema(wf_path):
    from jsonschema.validators import validator_for

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    document = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
    errors = [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator_for(schema)(schema).iter_errors(document)
    ]
    assert not errors, f"{wf_path.relative_to(REPO_ROOT)}: {errors}"


@pytest.mark.parametrize("wf_path", WORKFLOWS, ids=[p.parent.name for p in WORKFLOWS])
def test_recipe_still_compiles(wf_path):
    from mini_ork.workflow import compile_workflow

    assert compile_workflow(wf_path) is not None


# ─────────────────────────────────────────────────────────────────────────────
# behaviour is unchanged: old text vs collapsed text, signature-for-signature
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", CHANGED_RECIPES)
def test_collapse_preserves_the_compiled_graph(rel, tmp_path):
    """Compiling the pre-codemod text and the shipped text is indistinguishable.

    ``git show HEAD:<path>`` is the pre-change recipe while the change is
    uncommitted (and inside the verifier's base-snapshot copy). Once the change
    lands, HEAD carries the collapsed file and the baseline is unrecoverable —
    the test skips rather than reporting a vacuous pass; the invariant stays
    covered by ``test_alias_relabel_preserves_the_compiled_graph`` below.
    """
    old_text = _git_show(rel)
    if old_text is None:
        pytest.skip("git history unavailable for the pre-change baseline")
    new_text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    if old_text == new_text:
        pytest.skip("HEAD already carries the collapsed file; baseline unrecoverable")

    before = _signature(_compile(old_text, tmp_path, "old.yaml"))
    after = _signature(_compile(new_text, tmp_path, "new.yaml"))
    assert after == before, f"{rel}: the collapse changed the compiled graph"


@pytest.mark.parametrize("wf_path", WORKFLOWS, ids=[p.parent.name for p in WORKFLOWS])
def test_alias_relabel_preserves_the_compiled_graph(wf_path, tmp_path):
    """Re-labelling ``depends_on`` as an alias changes nothing the compiler emits."""
    text = wf_path.read_text(encoding="utf-8")
    baseline = _signature(_compile(text, tmp_path, "baseline.yaml"))
    relabelled = text.replace("edge_type: depends_on", "edge_type: verifies")
    assert _signature(_compile(relabelled, tmp_path, "alias.yaml")) == baseline, (
        f"{wf_path.relative_to(REPO_ROOT)}: a `verifies` edge does not compile "
        "exactly like the `depends_on` edge it replaced"
    )


ALIAS_WORKFLOW = """\
nodes:
  - name: implementer
    type: implementer
    outputs:
      - name: impl_patch
        kind: file
        path: artifacts/impl.patch
  - name: reviewer
    type: reviewer
    inputs:
      patch: {}
edges:
  - { from: implementer, to: reviewer, edge_type: verifies, from_output: impl_patch, to_input: patch }
"""


def test_verifies_alias_still_compiles_as_ordering_parent(tmp_path):
    compiled = _compile(ALIAS_WORKFLOW, tmp_path)
    assert "implementer" in compiled.control_parents.get("reviewer", ())
    assert any(
        binding.producer_node == "implementer"
        and binding.producer_output == "impl_patch"
        and binding.consumer_node == "reviewer"
        and binding.consumer_input == "patch"
        for binding in compiled.bindings
    )


def test_all_five_aliases_compile_identically_to_depends_on(tmp_path):
    def workflow(edge_type: str) -> str:
        return (
            "nodes:\n"
            "  - { name: planner, type: planner }\n"
            "  - { name: implementer, type: implementer }\n"
            "  - { name: reviewer, type: reviewer }\n"
            "edges:\n"
            f"  - {{ from: planner, to: implementer, edge_type: {edge_type} }}\n"
            f"  - {{ from: implementer, to: reviewer, edge_type: {edge_type} }}\n"
        )

    baseline = _signature(_compile(workflow("depends_on"), tmp_path, "depends_on.yaml"))
    for alias in DEPRECATED_ALIASES:
        assert _signature(_compile(workflow(alias), tmp_path, f"{alias}.yaml")) == baseline


# ─────────────────────────────────────────────────────────────────────────────
# validate: deprecation and dead-condition warnings
# ─────────────────────────────────────────────────────────────────────────────

_BLOCKS_WF = (
    "nodes:\n"
    "  - { name: planner, type: planner }\n"
    "  - { name: implementer, type: implementer }\n"
    "  - { name: reviewer, type: reviewer }\n"
    "edges:\n"
    "  - { from: planner, to: implementer, edge_type: depends_on }\n"
    "  - { from: implementer, to: reviewer, edge_type: blocks }\n"
)


def test_validate_warns_on_deprecated_alias_rc0(monkeypatch, tmp_path, capsys):
    root, home = _make_recipe_tree(tmp_path, "alias-probe", _BLOCKS_WF)
    rc, err, _ = _run_validate(monkeypatch, root, home, "--recipe", "alias-probe", capsys=capsys)
    assert rc == 0, f"warnings-only run must exit 0, got {rc}; stderr:\n{err}"
    assert (
        "recipe alias-probe uses deprecated edge_type(s) blocks "
        "— they behave exactly like depends_on" in err
    ), f"exact deprecation warning missing from stderr:\n{err}"
    assert (
        "rename them to depends_on "
        "(scripts/codemods/eco6_collapse_edge_types.py does it)" in err
    ), f"deprecation Fix hint missing from stderr:\n{err}"


def test_validate_strict_rc1_on_deprecated_alias(monkeypatch, tmp_path, capsys):
    root, home = _make_recipe_tree(tmp_path, "alias-probe", _BLOCKS_WF)
    rc, _, _ = _run_validate(
        monkeypatch, root, home, "--recipe", "alias-probe", "--strict", capsys=capsys
    )
    assert rc == 1, "--strict must turn the deprecation warning into rc 1"


def test_validate_emits_one_warning_listing_every_alias(monkeypatch, tmp_path, capsys):
    workflow = (
        "nodes:\n"
        "  - { name: planner, type: planner }\n"
        "  - { name: implementer, type: implementer }\n"
        "  - { name: reviewer, type: reviewer }\n"
        "edges:\n"
        "  - { from: planner, to: implementer, edge_type: verifies }\n"
        "  - { from: implementer, to: reviewer, edge_type: blocks }\n"
    )
    root, home = _make_recipe_tree(tmp_path, "multi-alias-probe", workflow)
    rc, err, _ = _run_validate(
        monkeypatch, root, home, "--recipe", "multi-alias-probe", capsys=capsys
    )
    assert rc == 0
    warning_lines = [line for line in err.splitlines() if "uses deprecated edge_type" in line]
    assert len(warning_lines) == 1, f"expected one warning per recipe, got {warning_lines}"
    assert "blocks" in warning_lines[0] and "verifies" in warning_lines[0]


def test_validate_warns_on_string_conditions(monkeypatch, tmp_path, capsys):
    workflow = (
        "nodes:\n"
        "  - { name: planner, type: planner }\n"
        "  - { name: implementer, type: implementer }\n"
        "  - { name: reviewer, type: reviewer }\n"
        "edges:\n"
        "  - { from: planner, to: implementer, edge_type: depends_on, condition: pass }\n"
        "  - { from: implementer, to: reviewer, edge_type: depends_on, condition: pass }\n"
    )
    root, home = _make_recipe_tree(tmp_path, "cond-probe", workflow)
    rc, err, _ = _run_validate(monkeypatch, root, home, "--recipe", "cond-probe", capsys=capsys)
    assert rc == 0, f"warnings-only run must exit 0, got {rc}; stderr:\n{err}"
    assert (
        "recipe cond-probe has 2 string condition(s) the runtime ignores" in err
    ), f"exact string-condition warning missing from stderr:\n{err}"
    assert (
        "remove them, or use an object predicate on a retries edge" in err
    ), f"string-condition Fix hint missing from stderr:\n{err}"


def test_validate_silent_for_clean_recipe_and_object_condition(monkeypatch, tmp_path, capsys):
    workflow = (
        "nodes:\n"
        "  - { name: planner, type: planner }\n"
        "  - { name: implementer, type: implementer }\n"
        "  - { name: reviewer, type: reviewer }\n"
        "edges:\n"
        "  - { from: planner, to: implementer, edge_type: depends_on }\n"
        "  - { from: implementer, to: reviewer, edge_type: depends_on }\n"
        "  - { from: reviewer, to: implementer, edge_type: retries, max_rounds: 2, "
        "condition: {pointer: /verdict, match: {const: pass}} }\n"
    )
    root, home = _make_recipe_tree(tmp_path, "clean-probe", workflow)
    rc, err, _ = _run_validate(monkeypatch, root, home, "--recipe", "clean-probe", capsys=capsys)
    assert rc == 0, f"clean recipe must validate OK, got {rc}; stderr:\n{err}"
    assert "deprecated edge_type" not in err
    assert "string condition" not in err


# ─────────────────────────────────────────────────────────────────────────────
# schema, types and docs carry the one-release compatibility story
# ─────────────────────────────────────────────────────────────────────────────

def test_schema_keeps_all_eight_edge_type_names():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    edge_type = schema["$defs"]["WorkflowEdge"]["properties"]["edge_type"]
    assert set(edge_type["enum"]) == set(ALL_EDGE_TYPES), edge_type["enum"]


def test_schema_description_marks_the_five_aliases_deprecated():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    description = schema["$defs"]["WorkflowEdge"]["properties"]["edge_type"]["description"]
    assert "have runtime meaning" in description
    assert "deprecated aliases of `depends_on` (2026-10) and will be removed" in description


def test_types_edge_type_lists_all_eight():
    import typing

    from mini_ork.types import EdgeType

    assert set(typing.get_args(EdgeType)) == set(ALL_EDGE_TYPES)


def test_docs_describe_real_semantics_and_list_the_aliases():
    text = DOCS_PATH.read_text(encoding="utf-8")
    assert "The executor enforces them" not in text
    section = text.lower().split("edge-type semantics", 1)[1].split("\n## ", 1)[0]
    for alias in DEPRECATED_ALIASES:
        assert alias in section, f"§6 must list the deprecated alias `{alias}`"
    assert "max_rounds" in section
    assert "recursion" in section
    assert "deprecated" in section
