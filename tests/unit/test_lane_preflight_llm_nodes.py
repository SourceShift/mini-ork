"""RB-23: lane preflight probes only nodes that call an LLM.

``resolve_dispatch_lanes`` used to route and probe every workflow node, so a
run was blocked ``lane_unhealthy`` on a lane that no node would ever dispatch
against:

* ``run-1792136781-59220`` — ``code-fix``'s ``typecheck`` is the deterministic
  script ``verifiers/typecheck.py`` (``verifier_ref`` set, ``prompt_ref: null``)
  yet was routed to kimi and answered 401;
* ``lhg-smoke-blocked-212556`` — the publisher lane made no LLM call.

The filter reads the workflow entry, not the dispatch tuple: node type
``publisher`` / ``rollback`` or ``verifier_ref`` without ``prompt_ref`` means
"cannot dispatch an LLM" and the node is dropped. With no readable workflow the
old behaviour stands — every node is probed. ``unverified()`` shares
``_evaluate`` → ``resolve_dispatch_lanes``, so the "cannot verify" listing skips
the same nodes for free.

Hermetic: workflows live in ``tmp_path``; routing and family resolution are
monkeypatched; no network, credentials or DB.
"""
from __future__ import annotations

import inspect

import pytest

from mini_ork.cli import lane_preflight

_UNSET = object()


# ── workflow.yaml builders ───────────────────────────────────────────────────

def _node_yaml(name, node_type, *, model_lane="", prompt_ref=_UNSET,
               verifier_ref=_UNSET):
    lines = [f"  - name: {name}", f"    type: {node_type}",
             "    dispatch_mode: serial"]
    if model_lane:
        lines.append(f"    model_lane: {model_lane}")
    if prompt_ref is not _UNSET:
        lines.append("    prompt_ref: null" if prompt_ref is None
                     else f"    prompt_ref: {prompt_ref}")
    if verifier_ref is not _UNSET:
        lines.append("    verifier_ref: null" if verifier_ref is None
                     else f"    verifier_ref: {verifier_ref}")
    return "\n".join(lines)


def _write_workflow(path, node_blocks):
    path.write_text(
        "version: \"1.0\"\n"
        "task_class: code-fix\n"
        "nodes:\n" + "\n".join(node_blocks) + "\n"
        "edges: []\n",
        encoding="utf-8",
    )


def _mixed_nodes():
    """The kickoff's exact scenario: LLM implementer, script verifier,
    publisher, rollback."""
    return [
        _node_yaml("implementer", "implementer", model_lane="glm",
                   prompt_ref="prompts/implementer.md"),
        _node_yaml("typecheck", "verifier", model_lane="kimi",
                   verifier_ref="verifiers/typecheck.py", prompt_ref=None),
        _node_yaml("publisher", "publisher", model_lane="deepseek"),
        _node_yaml("rollback", "rollback", model_lane="deepseek"),
    ]


def _fields(*specs):
    """Legacy 8-field dispatch tuples (``WorkflowNode.dispatch_fields`` layout):
    index 0 node_id, index 1 node type, index 6 model_lane."""
    return [(name, ntype, "", "", "", "", lane, "") for name, ntype, lane in specs]


MIXED_FIELDS = _fields(
    ("implementer", "implementer", "glm"),
    ("typecheck", "verifier", "kimi"),
    ("publisher", "publisher", "deepseek"),
    ("rollback", "rollback", "deepseek"),
)
ALL_NODE_IDS = ["implementer", "publisher", "rollback", "typecheck"]


@pytest.fixture(autouse=True)
def _no_inherited_workflow_env(monkeypatch):
    """A self-measurement run exports ``MINI_ORK_WORKFLOW`` into pytest; a
    test's workflow view must come only from what the test itself sets."""
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)


def _identity_route(node_type, workflow_lane, dry_run=False, root=None,
                    task_class=""):
    return workflow_lane


def _static_family(routed, root="", home=""):
    return f"family:{routed}"


@pytest.fixture
def dispatch_stubs(monkeypatch):
    """Routing/lane-family stubs, as the kickoff pins ("routing
    monkeypatched")."""
    from mini_ork.dispatch import llm_dispatch, routing

    monkeypatch.setattr(routing, "policy_route_lane", _identity_route)
    monkeypatch.setattr(llm_dispatch, "resolve_lane_family", _static_family)


# ── the kickoff's declared tests ────────────────────────────────────────────

def test_mixed_workflow_returns_only_the_implementer(tmp_path, dispatch_stubs):
    wf = tmp_path / "workflow.yaml"
    _write_workflow(wf, _mixed_nodes())
    lanes = lane_preflight.resolve_dispatch_lanes(
        MIXED_FIELDS, root=tmp_path, home=tmp_path, workflow_path=str(wf),
    )
    assert [dl.node_id for dl in lanes] == ["implementer"]


def test_no_readable_workflow_returns_every_node(tmp_path, dispatch_stubs):
    lanes = lane_preflight.resolve_dispatch_lanes(
        MIXED_FIELDS, root=tmp_path, home=tmp_path,
    )
    assert sorted(dl.node_id for dl in lanes) == ALL_NODE_IDS


def test_missing_workflow_path_returns_every_node(tmp_path, dispatch_stubs):
    lanes = lane_preflight.resolve_dispatch_lanes(
        MIXED_FIELDS, root=tmp_path, home=tmp_path,
        workflow_path=str(tmp_path / "no-such-workflow.yaml"),
    )
    assert sorted(dl.node_id for dl in lanes) == ALL_NODE_IDS


def test_llm_verifier_with_prompt_ref_is_still_returned(tmp_path, dispatch_stubs):
    wf = tmp_path / "workflow.yaml"
    _write_workflow(wf, [
        _node_yaml("review_gate", "verifier", model_lane="glm",
                   prompt_ref="prompts/review.md"),
    ])
    lanes = lane_preflight.resolve_dispatch_lanes(
        _fields(("review_gate", "verifier", "glm")),
        root=tmp_path, home=tmp_path, workflow_path=str(wf),
    )
    assert [dl.node_id for dl in lanes] == ["review_gate"]


# ── workflow source resolution ──────────────────────────────────────────────

def test_env_workflow_is_read_when_no_path_argument(tmp_path, monkeypatch,
                                                    dispatch_stubs):
    wf = tmp_path / "workflow.yaml"
    _write_workflow(wf, _mixed_nodes())
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(wf))
    lanes = lane_preflight.resolve_dispatch_lanes(
        MIXED_FIELDS, root=tmp_path, home=tmp_path,
        environ={"MINI_ORK_WORKFLOW": str(wf)},
    )
    assert [dl.node_id for dl in lanes] == ["implementer"]


def test_workflow_path_argument_wins_over_env_var(tmp_path, monkeypatch,
                                                  dispatch_stubs):
    arg_wf = tmp_path / "arg-workflow.yaml"
    env_wf = tmp_path / "env-workflow.yaml"
    _write_workflow(arg_wf, _mixed_nodes())
    # The decoy keeps ``typecheck`` as an LLM node: if the env workflow were
    # read instead, its node id would survive the filter.
    _write_workflow(env_wf, [
        _node_yaml("implementer", "implementer", model_lane="glm",
                   prompt_ref="prompts/implementer.md"),
        _node_yaml("typecheck", "verifier", model_lane="kimi",
                   verifier_ref="verifiers/typecheck.py",
                   prompt_ref="prompts/typecheck.md"),
    ])
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(env_wf))
    lanes = lane_preflight.resolve_dispatch_lanes(
        MIXED_FIELDS, root=tmp_path, home=tmp_path, workflow_path=str(arg_wf),
        environ={"MINI_ORK_WORKFLOW": str(env_wf)},
    )
    assert [dl.node_id for dl in lanes] == ["implementer"]


def test_workflow_path_is_an_optional_keyword_defaulting_to_none():
    params = inspect.signature(
        lane_preflight.resolve_dispatch_lanes).parameters
    assert "workflow_path" in params
    assert params["workflow_path"].default is None


# ── drop-rule boundaries ───────────────────────────────────────────────────

def test_verifier_with_both_refs_is_kept(tmp_path, dispatch_stubs):
    """The drop needs BOTH ``verifier_ref`` set AND no ``prompt_ref``."""
    wf = tmp_path / "workflow.yaml"
    _write_workflow(wf, [
        _node_yaml("dualcheck", "verifier", model_lane="kimi",
                   verifier_ref="verifiers/dualcheck.py",
                   prompt_ref="prompts/dualcheck.md"),
    ])
    lanes = lane_preflight.resolve_dispatch_lanes(
        _fields(("dualcheck", "verifier", "kimi")),
        root=tmp_path, home=tmp_path, workflow_path=str(wf),
    )
    assert [dl.node_id for dl in lanes] == ["dualcheck"]


def test_node_absent_from_workflow_is_kept(tmp_path, dispatch_stubs):
    """Existing callers/tests pass synthetic ids; "not in this workflow" is not
    proof the node cannot dispatch."""
    wf = tmp_path / "workflow.yaml"
    _write_workflow(wf, _mixed_nodes())
    lanes = lane_preflight.resolve_dispatch_lanes(
        _fields(("n1", "researcher", "glm")),
        root=tmp_path, home=tmp_path, workflow_path=str(wf),
    )
    assert [dl.node_id for dl in lanes] == ["n1"]


def test_separator_variant_matches_workflow_name(tmp_path, dispatch_stubs):
    wf = tmp_path / "workflow.yaml"
    _write_workflow(wf, [
        _node_yaml("static-check", "verifier", model_lane="kimi",
                   verifier_ref="verifiers/static.py", prompt_ref=None),
    ])
    lanes = lane_preflight.resolve_dispatch_lanes(
        _fields(("static_check", "verifier", "kimi")),
        root=tmp_path, home=tmp_path, workflow_path=str(wf),
    )
    assert lanes == []


# ── mechanism 2: the "cannot verify" listing skips the same nodes ──────────

def test_unverified_listing_skips_non_llm_nodes(tmp_path, monkeypatch,
                                                dispatch_stubs):
    from mini_ork.dispatch import providers

    wf = tmp_path / "workflow.yaml"
    _write_workflow(wf, _mixed_nodes())
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(wf))
    monkeypatch.setattr(providers, "provider_environment",
                        lambda environment=None: dict(environment or {}))
    monkeypatch.setattr(providers, "preflight", lambda models, root,
                        environment=None: {})
    rows = lane_preflight.unverified(
        MIXED_FIELDS, root=tmp_path, home=tmp_path,
        environ={"MINI_ORK_WORKFLOW": str(wf), "MO_LANE_HEALTH_GATE": "1"},
    )
    assert sorted(u.node_id for u in rows) == ["implementer"]
