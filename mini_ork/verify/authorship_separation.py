"""Authorship separation — the probe author must not be the implementer's model family (I2a).

A probe author writes the acceptance probes before any implementation exists;
it is only an independent check when it is a different *model family* from the
implementer. Role names always differ (``glm_lens`` versus ``implementer``), so
a name comparison only catches a mis-wired recipe. The real failure is two
roles resolving to the same family in a home's agents overlay: the
"independent" author is then the implementer's own model grading itself.

:func:`check_separation` compares the families that
``agents_config.lane_families(agents)`` resolves for each probe-author lane and
each implementer lane. It runs only with ``MO_AUTHORSHIP_SEPARATION`` set to
``shadow`` or ``1`` (default OFF: with the flag unset nothing in core reads or
writes anything).

These are the CONFIGURED lanes, not proof of what served: the router's
fallback walk can serve a different member, so ``llm_calls.model_id`` stays
authoritative for what actually ran.
"""
from __future__ import annotations

import os
from typing import Any

FLAG = "MO_AUTHORSHIP_SEPARATION"

#: ``type`` a workflow node may declare to say "I write acceptance probes".
PROBE_AUTHOR_TYPE = "probe_author"
#: Prompt basenames that mark a probe-author node whatever the node is named.
PROBE_AUTHOR_PROMPTS = frozenset({"spec-test-author.md", "test-author.md"})
IMPLEMENTER_TYPE = "implementer"

# Named reasons the gate reports (task_runs notes, authorship-separation-gate.json, stderr).
AUTHOR_FAMILY_IS_IMPLEMENTER = "author_family_is_implementer"
NO_PROBE_AUTHOR_NODE = "no_probe_author_node"
NO_IMPLEMENTER_NODE = "no_implementer_node"


def mode(environ: dict | None = None) -> str:
    """``off`` (default), ``shadow`` or ``enforce`` (``MO_AUTHORSHIP_SEPARATION=1``).

    Shadow runs the check and records what it WOULD block
    (``authorship-separation-gate.json`` + a task_runs note) but never blocks.
    Unset, empty, ``0`` and ``off`` are off. Any other value raises
    ``ValueError`` naming the flag: a typo must not silently disable a gate the
    operator asked for.
    """
    if environ is not None:
        raw = environ.get(FLAG, "")
    else:
        from mini_ork.context import context_env

        raw = context_env(FLAG, "")
    value = (raw or "").strip().lower()
    if value in ("", "0", "off"):
        return "off"
    if value == "shadow":
        return "shadow"
    if value == "1":
        return "enforce"
    raise ValueError(f"{FLAG} must be one of off, shadow, 1 (enforce); got {raw!r}")


def _nodes(workflow: Any) -> list[dict]:
    nodes = workflow.get("nodes") if isinstance(workflow, dict) else None
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def probe_author_nodes(workflow: dict) -> list[dict]:
    """Nodes that write acceptance probes, identified STRUCTURALLY (never by node name).

    ``type == "probe_author"`` or a ``prompt_ref`` whose basename is
    ``spec-test-author.md`` or ``test-author.md``. A node merely *named*
    ``spec_test_author`` with an unrelated prompt is not matched.
    """
    out: list[dict] = []
    for node in _nodes(workflow):
        prompt = os.path.basename(str(node.get("prompt_ref") or ""))
        if node.get("type") == PROBE_AUTHOR_TYPE or prompt in PROBE_AUTHOR_PROMPTS:
            out.append(node)
    return out


def _implementer_nodes(workflow: dict) -> list[dict]:
    return [n for n in _nodes(workflow) if n.get("type") == IMPLEMENTER_TYPE]


def _lane(node: dict) -> str:
    """The node's lane alias — ``model_lane``, else its type (what dispatch uses)."""
    return str(node.get("model_lane") or node.get("type") or "")


def _family(lane: str, families: dict[str, dict]) -> str:
    """``families[lane]["family"]``; a lane missing from the map resolves to itself."""
    entry = families.get(lane) if isinstance(families, dict) else None
    family = entry.get("family") if isinstance(entry, dict) else None
    return str(family) if family else lane


def check_separation(workflow: dict, families: dict[str, dict]) -> tuple[bool, str, dict]:
    """``(ok, reason, detail)``.

    ``families`` is ``agents_config.lane_families(agents)``. Each probe-author
    node's lane and each implementer node's lane resolve through
    ``families[alias]["family"]`` (an alias missing from the map resolves to
    itself). ``ok`` is False, with reason
    ``author_family_is_implementer: <author_lane>/<impl_lane> -> <family>``,
    when any author family equals any implementer family. No probe-author node
    gives ``(True, "no_probe_author_node", ...)``; no implementer node gives
    ``(True, "no_implementer_node", ...)``.

    ``detail`` is ``{probe_author_lanes, implementer_lanes, author_families,
    implementer_families}`` (the two family maps are ``{lane: family}``).
    """
    author_lanes = [_lane(n) for n in probe_author_nodes(workflow)]
    impl_lanes = [_lane(n) for n in _implementer_nodes(workflow)]
    author_families = {lane: _family(lane, families) for lane in author_lanes}
    impl_families = {lane: _family(lane, families) for lane in impl_lanes}
    detail: dict[str, Any] = {
        "probe_author_lanes": author_lanes,
        "implementer_lanes": impl_lanes,
        "author_families": author_families,
        "implementer_families": impl_families,
    }
    if not author_lanes:
        return True, NO_PROBE_AUTHOR_NODE, detail
    if not impl_lanes:
        return True, NO_IMPLEMENTER_NODE, detail
    for author_lane, author_family in author_families.items():
        for impl_lane, impl_family in impl_families.items():
            if author_family == impl_family:
                return (False,
                        f"{AUTHOR_FAMILY_IS_IMPLEMENTER}: {author_lane}/{impl_lane} -> {author_family}",
                        detail)
    return True, "", detail
