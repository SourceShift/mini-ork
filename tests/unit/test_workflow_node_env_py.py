"""A node's ``env:`` block compiles into the WorkflowNode the dispatch reads.

The compile-time contract is deliberately narrow: the recipe declares *which*
agent needs a variable and leaves the value a ``${NAME}`` reference, because a
recipe is compiled — and tested — without the operator's credentials present.
Only the shape is validated here.
"""
import pytest

from mini_ork.workflow import WorkflowCompileError, compile_workflow


def _compile(tmp_path, node_block):
    path = tmp_path / "workflow.yaml"
    path.write_text(
        "version: '0.2.0'\ntask_class: t\n"
        "nodes:\n"
        "  - name: collector\n"
        "    type: researcher\n"
        "    dispatch_mode: serial\n" + node_block +
        "edges: []\n"
    )
    return compile_workflow(path).nodes["collector"]


def test_node_env_compiles_onto_the_node(tmp_path):
    node = _compile(tmp_path, "    env:\n      ARXIV_API_TOKEN: ${LIBWIT_ARXIV_TOKEN}\n")
    assert node.env == {"ARXIV_API_TOKEN": "${LIBWIT_ARXIV_TOKEN}"}


def test_placeholder_is_left_unresolved_at_compile_time(tmp_path):
    # The value is a reference, not a secret: compiling must not need the store.
    node = _compile(tmp_path, "    env:\n      T: ${SOMETHING_UNSET}\n")
    assert node.env["T"] == "${SOMETHING_UNSET}"


def test_null_declares_the_variable_absent(tmp_path):
    node = _compile(tmp_path, "    env:\n      T: null\n")
    assert node.env == {"T": None}


def test_booleans_become_one_and_zero(tmp_path):
    # YAML true/false stringify as "True"/"False", which no child reads as a flag.
    node = _compile(tmp_path, "    env:\n      DEBUG_FLAG: true\n      QUIET: false\n")
    assert node.env == {"DEBUG_FLAG": "1", "QUIET": "0"}


def test_a_yaml_boolean_key_is_refused(tmp_path):
    # `ON:` parses as the boolean True, so the variable would silently be "True".
    with pytest.raises(WorkflowCompileError, match="YAML boolean"):
        _compile(tmp_path, "    env:\n      ON: 1\n")


def test_numbers_are_stringified(tmp_path):
    node = _compile(tmp_path, "    env:\n      MO_MIN: 40\n      RATIO: 0.5\n")
    assert node.env == {"MO_MIN": "40", "RATIO": "0.5"}


def test_a_nested_value_is_refused(tmp_path):
    # It would otherwise reach the child as the literal "{'a': 'b'}".
    with pytest.raises(WorkflowCompileError, match="must be a string, number, boolean or null"):
        _compile(tmp_path, "    env:\n      T:\n        a: b\n")


def test_a_non_mapping_env_is_refused(tmp_path):
    with pytest.raises(WorkflowCompileError, match="env must be a mapping"):
        _compile(tmp_path, "    env:\n      - A=1\n")


def test_a_node_without_env_compiles_to_an_empty_map(tmp_path):
    assert _compile(tmp_path, "").env == {}
