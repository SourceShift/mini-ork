"""Per-node ``timeout_minutes`` compiles and is validated.

The field was declared in ``schemas/workflow.schema.json`` from the start but
no compiler read it, so a recipe that bounded a node got the global
``MO_NODE_TIMEOUT_S`` anyway. These pin the compile half of the contract; the
dispatch half publishes it at the node boundary in ``execute_handlers``.
"""
from __future__ import annotations

import pytest

from mini_ork.workflow import WorkflowCompileError, compile_workflow


def _write(tmp_path, nodes):
    path = tmp_path / "workflow.yaml"
    body = "nodes:\n"
    for node in nodes:
        body += "  - " + node + "\n"
    body += "edges: []\n"
    path.write_text(body, encoding="utf-8")
    return path


def test_timeout_minutes_parses_onto_the_node(tmp_path):
    wf = compile_workflow(_write(tmp_path, [
        "{ name: bounded, type: researcher, timeout_minutes: 12 }",
        "{ name: inherit, type: implementer }",
    ]))
    assert wf.nodes["bounded"].timeout_minutes == 12.0
    assert wf.nodes["inherit"].timeout_minutes == 0.0


def test_fractional_minutes_are_kept(tmp_path):
    wf = compile_workflow(_write(tmp_path, [
        "{ name: short, type: verifier, timeout_minutes: 1.5 }",
    ]))
    assert wf.nodes["short"].timeout_minutes == 1.5


@pytest.mark.parametrize("value", ["not-a-number", "-1"])
def test_invalid_timeout_minutes_is_refused(tmp_path, value):
    with pytest.raises(WorkflowCompileError):
        compile_workflow(_write(tmp_path, [
            f"{{ name: bad, type: verifier, timeout_minutes: {value} }}",
        ]))


@pytest.mark.parametrize("value", [".inf", ".nan"])
def test_non_finite_timeout_minutes_is_refused(tmp_path, value):
    """NaN fails ``< 0`` and ±inf passes ``int(m * 60)`` right up to dispatch,
    where OverflowError lands mid-run with no node name in the message."""
    with pytest.raises(WorkflowCompileError, match="finite"):
        compile_workflow(_write(tmp_path, [
            f"{{ name: bad, type: verifier, timeout_minutes: {value} }}",
        ]))
