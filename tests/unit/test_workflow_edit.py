"""Tests for typed, schema-checked, reversible ``workflow.yaml`` edits.

Hermetic: ``tmp_path`` only, no DB, no network.
"""

from __future__ import annotations

import copy
import re

import pytest
import yaml

from mini_ork.learning import workflow_edit as we

FIXTURE_YAML = '''version: "0.1.0"
task_class: fw_edit
nodes:
  - name: planner
    type: planner
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/planner.md
  - name: implementer
    type: implementer
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/implementer.md
    tools:
      native: [Read, Write]
      mcp: [codegraph]
    retry:
      max_attempts: 3
  - name: lint
    type: verifier
    dispatch_mode: serial
    verifier_ref: verifiers/lint.py
    tools:
      native: [Read, Bash]
  - name: docs
    type: researcher
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/docs.md
    tools:
      native: [Read]
  - name: publisher
    type: publisher
    dispatch_mode: serial
    prompt_ref: null
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: planner, to: docs, edge_type: depends_on}
  - {from: implementer, to: lint, edge_type: depends_on}
  - {from: lint, to: publisher, edge_type: depends_on}
  - {from: docs, to: publisher, edge_type: depends_on}
'''


@pytest.fixture()
def wf_path(tmp_path):
    path = tmp_path / "workflow.yaml"
    path.write_text(FIXTURE_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def base_doc():
    return yaml.safe_load(FIXTURE_YAML)


def _node(doc, name):
    return next(node for node in doc["nodes"] if node["name"] == name)


# --------------------------------------------------------------- parse_edit

def test_parse_edit_refusals():
    two_edits = [
        {"op": "lane_pin", "node": "a", "lane": "x"},
        {"op": "lane_pin", "node": "b", "lane": "y"},
    ]
    with pytest.raises(we.WorkflowEditError, match="one edit per candidate"):
        we.parse_edit(two_edits)

    with pytest.raises(we.WorkflowEditError, match="unknown op"):
        we.parse_edit({"op": "rename", "node": "x"})

    with pytest.raises(we.WorkflowEditError):
        we.parse_edit(
            {"op": "lane_pin", "node": "implementer", "lane": "glm", "why": "faster"}
        )

    for bad in (0, 6, True):
        with pytest.raises(we.WorkflowEditError):
            we.parse_edit(
                {"op": "retry_count", "node": "planner", "max_attempts": bad}
            )

    with pytest.raises(we.WorkflowEditError):
        we.parse_edit(
            {"op": "tool_grant", "node": "lint", "group": "shell", "tool": "Bash", "grant": True}
        )

    with pytest.raises(we.WorkflowEditError):
        we.parse_edit("not json")


def test_parse_edit_accepts_json_string_and_strips_tool():
    raw = '{"node": "lint", "op": "tool_grant", "tool": " Bash ", "group": "native", "grant": true}'
    parsed = we.parse_edit(raw)
    assert isinstance(parsed, dict)
    assert parsed == {
        "op": "tool_grant",
        "node": "lint",
        "group": "native",
        "tool": "Bash",
        "grant": True,
    }


def test_parse_edit_returns_new_dict():
    raw = {"op": "lane_pin", "node": "implementer", "lane": "glm"}
    parsed = we.parse_edit(raw)
    assert parsed is not raw
    assert parsed == raw
    assert raw["lane"] == "glm"


# ----------------------------------------------------------- edit_signature

def test_edit_signature_is_stable_and_discriminating():
    as_dict = {"op": "lane_pin", "node": "implementer", "lane": "glm"}
    as_json = '{"lane": "glm", "node": "implementer", "op": "lane_pin"}'
    signature = we.edit_signature(as_dict)
    assert re.fullmatch(r"[0-9a-f]{16}", signature)
    assert signature == we.edit_signature(as_json)
    assert signature != we.edit_signature(
        {"op": "lane_pin", "node": "implementer", "lane": "opus"}
    )


# --------------------------------------------------------------- apply_edit

def test_apply_edit_never_mutates_input(base_doc):
    snapshot = copy.deepcopy(base_doc)
    out = we.apply_edit(base_doc, {"op": "lane_pin", "node": "implementer", "lane": "glm"})
    assert _node(out, "implementer")["model_lane"] == "glm"
    assert base_doc == snapshot


def test_lane_pin_sets_and_refuses(base_doc):
    out = we.apply_edit(base_doc, {"op": "lane_pin", "node": "implementer", "lane": "glm"})
    assert _node(out, "implementer")["model_lane"] == "glm"

    for name in ("lint", "publisher"):
        with pytest.raises(we.WorkflowEditError, match="cannot pin a lane"):
            we.apply_edit(base_doc, {"op": "lane_pin", "node": name, "lane": "glm"})

    with pytest.raises(we.WorkflowEditError, match="no-op"):
        we.apply_edit(base_doc, {"op": "lane_pin", "node": "implementer", "lane": "sonnet"})


def test_tool_grant_existing_capability(base_doc):
    out = we.apply_edit(
        base_doc,
        {"op": "tool_grant", "node": "implementer", "group": "native", "tool": "Bash", "grant": True},
    )
    assert _node(out, "implementer")["tools"]["native"] == ["Read", "Write", "Bash"]


def test_tool_grant_new_capability_refused(base_doc):
    with pytest.raises(we.WorkflowEditError, match="new capability"):
        we.apply_edit(
            base_doc,
            {"op": "tool_grant", "node": "implementer", "group": "native", "tool": "Edit", "grant": True},
        )


def test_tool_grant_creates_missing_group(base_doc):
    out = we.apply_edit(
        base_doc,
        {"op": "tool_grant", "node": "docs", "group": "mcp", "tool": "codegraph", "grant": True},
    )
    assert _node(out, "docs")["tools"] == {"native": ["Read"], "mcp": ["codegraph"]}


def test_tool_revoke_leaves_other_group_untouched(base_doc):
    out = we.apply_edit(
        base_doc,
        {"op": "tool_grant", "node": "implementer", "group": "native", "tool": "Write", "grant": False},
    )
    tools = _node(out, "implementer")["tools"]
    assert tools["native"] == ["Read"]
    assert tools["mcp"] == ["codegraph"]


def test_tool_revoke_missing_tool_is_noop(base_doc):
    with pytest.raises(we.WorkflowEditError, match="no-op"):
        we.apply_edit(
            base_doc,
            {"op": "tool_grant", "node": "implementer", "group": "native", "tool": "Edit", "grant": False},
        )


def test_tool_revoke_tool_held_only_elsewhere_is_noop(base_doc):
    # Review fix: `Bash` is on `lint`, not on `implementer`. Revoking it from
    # `implementer` is a no-op error and must never touch implementer's other
    # grants (an earlier version pruned `Write` here).
    snapshot = copy.deepcopy(base_doc)
    with pytest.raises(we.WorkflowEditError, match="no-op"):
        we.apply_edit(
            base_doc,
            {"op": "tool_grant", "node": "implementer", "group": "native", "tool": "Bash", "grant": False},
        )
    assert base_doc == snapshot


def test_tool_revoke_missing_capability_from_untooled_node_is_noop(base_doc):
    # A node with no grants must not gain an empty ``tools[group]``.
    with pytest.raises(we.WorkflowEditError, match="no-op"):
        we.apply_edit(
            base_doc,
            {"op": "tool_grant", "node": "publisher", "group": "native", "tool": "Bash", "grant": False},
        )


def test_retry_count_sets_refuses_and_creates(base_doc):
    out = we.apply_edit(base_doc, {"op": "retry_count", "node": "implementer", "max_attempts": 2})
    assert _node(out, "implementer")["retry"] == {"max_attempts": 2}

    with pytest.raises(we.WorkflowEditError, match="no-op"):
        we.apply_edit(base_doc, {"op": "retry_count", "node": "implementer", "max_attempts": 3})

    with pytest.raises(we.WorkflowEditError, match="no-op"):
        we.apply_edit(base_doc, {"op": "retry_count", "node": "planner", "max_attempts": 3})

    out = we.apply_edit(base_doc, {"op": "retry_count", "node": "planner", "max_attempts": 1})
    assert _node(out, "planner")["retry"] == {"max_attempts": 1}


# -------------------------------------------------------- materialize order

def test_materialize_node_order_moves_docs(wf_path):
    original_bytes = wf_path.read_bytes()
    result = we.materialize(wf_path, {"op": "node_order", "node": "docs", "index": 1})
    assert result["before_order"] == ["planner", "implementer", "lint", "docs", "publisher"]
    assert result["after_order"] == ["planner", "docs", "implementer", "lint", "publisher"]
    assert wf_path.read_bytes() == original_bytes


def test_materialize_node_order_unchanged_order_is_noop(wf_path):
    with pytest.raises(we.WorkflowEditError) as excinfo:
        we.materialize(wf_path, {"op": "node_order", "node": "publisher", "index": 0})
    assert str(excinfo.value).startswith("no-op: execution order unchanged")


def test_materialize_node_order_index_out_of_range(wf_path):
    with pytest.raises(we.WorkflowEditError):
        we.materialize(wf_path, {"op": "node_order", "node": "docs", "index": 9})


# -------------------------------------------------------- materialize result

def test_materialize_result_shape(wf_path):
    original_bytes = wf_path.read_bytes()
    result = we.materialize(wf_path, {"op": "lane_pin", "node": "implementer", "lane": "glm"})
    assert set(result) == {
        "edit",
        "signature",
        "path",
        "before_text",
        "after_text",
        "before_sha256",
        "after_sha256",
        "before_order",
        "after_order",
        "comments_dropped",
    }
    assert result["edit"] == we.parse_edit(
        {"op": "lane_pin", "node": "implementer", "lane": "glm"}
    )
    assert result["signature"] == we.edit_signature(
        {"op": "lane_pin", "node": "implementer", "lane": "glm"}
    )
    assert result["path"] == str(wf_path)
    assert result["before_text"] == FIXTURE_YAML
    assert result["before_sha256"] == we.file_sha256(wf_path)
    assert result["after_sha256"] == we.sha256_text(result["after_text"])
    document = yaml.safe_load(result["after_text"])
    assert _node(document, "implementer")["model_lane"] == "glm"
    assert result["before_order"] == result["after_order"]
    assert wf_path.read_bytes() == original_bytes


# ------------------------------------------------------ materialize refusals

def test_materialize_missing_file(tmp_path):
    with pytest.raises(we.WorkflowEditError) as excinfo:
        we.materialize(
            tmp_path / "nope.yaml",
            {"op": "lane_pin", "node": "implementer", "lane": "glm"},
        )
    assert str(excinfo.value).startswith("workflow not found: ")


def test_materialize_schema_violating_base(tmp_path):
    document = yaml.safe_load(FIXTURE_YAML)
    del document["edges"]
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(we.WorkflowEditError) as excinfo:
        we.materialize(path, {"op": "lane_pin", "node": "implementer", "lane": "glm"})
    assert str(excinfo.value).startswith("base workflow violates the schema: ")


# ------------------------------------------------------------ apply/rollback

def test_apply_to_file_and_rollback_round_trip(wf_path):
    original_bytes = wf_path.read_bytes()
    result = we.materialize(wf_path, {"op": "lane_pin", "node": "implementer", "lane": "glm"})

    assert we.apply_to_file(wf_path, result) is True
    assert wf_path.read_bytes() == result["after_text"].encode("utf-8")
    assert we.file_sha256(wf_path) == result["after_sha256"]
    assert we.apply_to_file(wf_path, result) is False

    assert we.rollback(wf_path, result) is True
    assert wf_path.read_bytes() == original_bytes
    assert we.rollback(wf_path, result) is False


def test_rollback_refuses_when_file_changed_by_hand(wf_path):
    result = we.materialize(wf_path, {"op": "lane_pin", "node": "implementer", "lane": "glm"})
    assert we.apply_to_file(wf_path, result) is True
    hand_edit = "version: '0.1.0'\nhand_edited: true\n"
    wf_path.write_text(hand_edit, encoding="utf-8")
    assert we.rollback(wf_path, result) is False
    assert wf_path.read_text(encoding="utf-8") == hand_edit


# ------------------------------------------------------------ schema_errors

def test_schema_errors_valid_fixture(base_doc):
    assert we.schema_errors(base_doc) == []


def test_schema_errors_wizard_type(base_doc):
    base_doc["nodes"][0]["type"] = "wizard"
    errors = we.schema_errors(base_doc)
    assert errors
    assert all(": " in error for error in errors)
    assert errors == sorted(errors)


def test_schema_errors_missing_or_malformed_schema(tmp_path):
    with pytest.raises(we.WorkflowEditError):
        we.schema_errors({}, schema_path=tmp_path / "missing.json")

    malformed = tmp_path / "schema.json"
    malformed.write_text("{not json", encoding="utf-8")
    with pytest.raises(we.WorkflowEditError):
        we.schema_errors({}, schema_path=malformed)


def test_materialize_refuses_non_utf8_workflow(tmp_path):
    path = tmp_path / "workflow.yaml"
    path.write_bytes(b"\xff\xfe not valid utf-8")
    with pytest.raises(we.WorkflowEditError):
        we.materialize(path, {"op": "lane_pin", "node": "implementer", "lane": "glm"})


# ------------------------------------------------------------- render/hashes

def test_render_and_sha_helpers(tmp_path):
    document = yaml.safe_load(FIXTURE_YAML)
    rendered = we.render(document)
    assert yaml.safe_load(rendered) == document
    assert we.sha256_text("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    path = tmp_path / "one.yaml"
    path.write_text("abc", encoding="utf-8")
    assert we.file_sha256(path) == we.sha256_text("abc")
    assert we.file_sha256(tmp_path / "missing.yaml") == ""

    assert we.OPS == ("node_order", "lane_pin", "tool_grant", "retry_count")
    assert we.TOOL_GROUPS == ("native", "mcp")
    assert isinstance(we.LANE_NODE_TYPES, frozenset)
    assert (we.RETRY_MIN, we.RETRY_MAX, we.DEFAULT_MAX_ATTEMPTS) == (1, 5, 3)
    assert len(we.__all__) == 12


def test_apply_to_file_refuses_to_drop_comments(tmp_path):
    # Review fix: the re-render drops YAML comments, so a commented live file
    # is refused unless the caller accepts the reformat explicitly.
    path = tmp_path / "workflow.yaml"
    path.write_text("# operator note: keep the lint gate first\n" + FIXTURE_YAML, encoding="utf-8")
    result = we.materialize(path, {"op": "lane_pin", "node": "implementer", "lane": "glm"})
    assert result["comments_dropped"] == 1
    original = path.read_bytes()
    with pytest.raises(we.WorkflowEditError, match="comment line"):
        we.apply_to_file(path, result)
    assert path.read_bytes() == original
    assert we.apply_to_file(path, result, allow_reformat=True) is True
    assert we.rollback(path, result) is True
    assert path.read_bytes() == original


def test_comment_lines_counts_hash_comments():
    assert we.comment_lines("a: 1\n# x\nb: 2  # y\nc: 'a#b'\n") == 2
