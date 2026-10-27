"""Typed, schema-checked, reversible ``workflow.yaml`` edits.

This module is a pure library: it turns ONE typed edit into a validated,
compiled, reversible workflow text without touching the live file.  Every
refusal raises :class:`WorkflowEditError`; nothing here reads the environment,
touches a database or reaches the network.

The edit shapes are deliberately narrow so a candidate can be attributed to a
single surface:

- ``node_order`` moves a node in the declared ``nodes`` list;
- ``lane_pin`` changes a node's ``model_lane``;
- ``tool_grant`` grants or revokes one tool in one ``tools`` group;
- ``retry_count`` changes the per-node ``retry.max_attempts`` knob.

The loop may move an existing capability but never invent a new one, so a
``tool_grant`` is refused unless another node already holds the tool.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema.validators import validator_for

from mini_ork.workflow.compiler import WorkflowCompileError, compile_workflow

OPS: tuple[str, ...] = ("node_order", "lane_pin", "tool_grant", "retry_count")
TOOL_GROUPS: tuple[str, ...] = ("native", "mcp")
LANE_NODE_TYPES: frozenset[str] = frozenset(
    {"planner", "researcher", "implementer", "reviewer", "eval", "reflector"}
)
RETRY_MIN = 1
RETRY_MAX = 5
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "workflow.schema.json"

_LANE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

# The op-specific keys, excluding the shared ``op`` and ``node``.
_OP_KEYS: dict[str, frozenset[str]] = {
    "node_order": frozenset({"index"}),
    "lane_pin": frozenset({"lane"}),
    "tool_grant": frozenset({"group", "tool", "grant"}),
    "retry_count": frozenset({"max_attempts"}),
}

__all__ = [
    "OPS",
    "WorkflowEditError",
    "parse_edit",
    "edit_signature",
    "apply_edit",
    "schema_errors",
    "render",
    "sha256_text",
    "file_sha256",
    "materialize",
    "apply_to_file",
    "rollback",
]


class WorkflowEditError(ValueError):
    """Raised for every refusal in this module."""


def _format_keys(keys: set[Any]) -> str:
    return "{" + ", ".join(sorted(str(key) for key in keys)) + "}"


def parse_edit(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    """Decode and validate one edit, returning a new plain dict.

    A ``str`` is decoded with :func:`json.loads`.  A list is always refused:
    one edit per candidate is the attribution contract.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise WorkflowEditError(f"edit is not valid JSON: {exc}") from exc

    if isinstance(raw, list):
        raise WorkflowEditError(
            "one edit per candidate: a candidate may change only one thing"
        )
    if not isinstance(raw, Mapping):
        raise WorkflowEditError("edit must be a JSON object")

    op = raw.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise WorkflowEditError(f"unknown op: {op!r}")

    allowed = {"op", "node"} | set(_OP_KEYS[op])
    actual = set(raw.keys())
    if actual != allowed:
        missing = allowed - actual
        extra = actual - allowed
        raise WorkflowEditError(
            f"invalid edit keys: missing {_format_keys(missing)}, extra {_format_keys(extra)}"
        )

    node = raw["node"]
    if not isinstance(node, str) or not node.strip():
        raise WorkflowEditError("node must be a non-empty string")

    if op == "node_order":
        index = raw["index"]
        if isinstance(index, bool) or not isinstance(index, int):
            raise WorkflowEditError("index must be an integer")
        if index < 0:
            raise WorkflowEditError("index must be >= 0")
        return {"op": op, "node": node, "index": index}

    if op == "lane_pin":
        lane = raw["lane"]
        if not isinstance(lane, str) or _LANE_PATTERN.fullmatch(lane) is None:
            raise WorkflowEditError(
                "lane must be a string matching ^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
            )
        return {"op": op, "node": node, "lane": lane}

    if op == "tool_grant":
        group = raw["group"]
        if not isinstance(group, str) or group not in TOOL_GROUPS:
            raise WorkflowEditError(f"group must be one of {TOOL_GROUPS}")
        tool = raw["tool"]
        if not isinstance(tool, str) or not tool.strip():
            raise WorkflowEditError("tool must be a non-empty string")
        grant = raw["grant"]
        if not isinstance(grant, bool):
            raise WorkflowEditError("grant must be a boolean")
        return {
            "op": op,
            "node": node,
            "group": group,
            "tool": tool.strip(),
            "grant": grant,
        }

    # retry_count
    max_attempts = raw["max_attempts"]
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise WorkflowEditError("max_attempts must be an integer")
    if not RETRY_MIN <= max_attempts <= RETRY_MAX:
        raise WorkflowEditError(
            f"max_attempts must be between {RETRY_MIN} and {RETRY_MAX}"
        )
    return {"op": op, "node": node, "max_attempts": max_attempts}


def edit_signature(edit: str | Mapping[str, Any]) -> str:
    """Return a stable 16-hex-character signature for one parsed edit."""
    parsed = parse_edit(edit)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _find_target(nodes: list[Any], name: str) -> int:
    for index, node in enumerate(nodes):
        if isinstance(node, Mapping) and node.get("name") == name:
            return index
    raise WorkflowEditError(f"unknown node: {name}")


def _tool_granted_elsewhere(
    nodes: list[Any], target_index: int, group: str, tool: str
) -> bool:
    for index, other in enumerate(nodes):
        if index == target_index or not isinstance(other, Mapping):
            continue
        other_tools = other.get("tools")
        if not isinstance(other_tools, dict):
            continue
        granted = other_tools.get(group)
        if isinstance(granted, list) and tool in granted:
            return True
    return False


def apply_edit(document: Mapping[str, Any], edit: str | Mapping[str, Any]) -> dict[str, Any]:
    """Return a new document with ``edit`` applied; the input is never mutated."""
    parsed = parse_edit(edit)
    if not isinstance(document, Mapping):
        raise WorkflowEditError("workflow document must be a mapping")

    out = copy.deepcopy(dict(document))
    nodes = out.get("nodes")
    if not isinstance(nodes, list):
        raise WorkflowEditError("workflow nodes must be a list")

    target_index = _find_target(nodes, parsed["node"])
    node = nodes[target_index]
    if not isinstance(node, dict):
        raise WorkflowEditError("workflow node must be a mapping")

    op = parsed["op"]

    if op == "node_order":
        index = parsed["index"]
        if index >= len(nodes):
            raise WorkflowEditError(
                f"index {index} is out of range for {len(nodes)} nodes"
            )
        if index == target_index:
            raise WorkflowEditError("no-op: node is already at that index")
        nodes.insert(index, nodes.pop(target_index))
        return out

    if op == "lane_pin":
        node_type = node.get("type")
        if not isinstance(node_type, str) or node_type not in LANE_NODE_TYPES:
            raise WorkflowEditError(f"cannot pin a lane on node type {node_type!r}")
        lane = parsed["lane"]
        if node.get("model_lane") == lane:
            raise WorkflowEditError("no-op: lane already pinned")
        node["model_lane"] = lane
        return out

    if op == "tool_grant":
        tools = node.get("tools")
        if isinstance(tools, list):
            raise WorkflowEditError("list-form tools are not editable")
        if tools is None:
            tools = {}
        elif not isinstance(tools, dict):
            raise WorkflowEditError("node tools must be a mapping")

        group = parsed["group"]
        tool = parsed["tool"]
        current = list(tools.get(group) or [])

        if parsed["grant"]:
            if tool in current:
                raise WorkflowEditError(f"no-op: {tool!r} is already granted")
            if not _tool_granted_elsewhere(nodes, target_index, group, tool):
                raise WorkflowEditError(
                    f"new capability: {tool!r} is not granted to any other node"
                )
            current.append(tool)
        elif tool in current:
            current = [item for item in current if item != tool]
        else:
            # Revoking a tool this node does not hold is a no-op, whether or
            # not another node holds it. It must never touch the node's other
            # grants.
            raise WorkflowEditError(f"no-op: {tool!r} is not granted")

        tools[group] = current
        node["tools"] = tools
        return out

    # retry_count
    retry = node.get("retry")
    if retry is None:
        retry = {}
    elif not isinstance(retry, dict):
        raise WorkflowEditError("node retry must be a mapping")
    try:
        current_attempts = int(retry.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError) as exc:
        raise WorkflowEditError(f"retry max_attempts is not an integer: {exc}") from exc
    if current_attempts == parsed["max_attempts"]:
        raise WorkflowEditError("no-op: retry count already set")
    retry["max_attempts"] = parsed["max_attempts"]
    node["retry"] = retry
    return out


def schema_errors(document: Any, *, schema_path: str | Path | None = None) -> list[str]:
    """Validate ``document`` against the workflow schema and return sorted errors."""
    path = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH
    try:
        schema_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkflowEditError(f"cannot read workflow schema {path}: {exc}") from exc
    try:
        schema = json.loads(schema_text)
    except ValueError as exc:
        raise WorkflowEditError(f"workflow schema is not valid JSON: {exc}") from exc
    validator = validator_for(schema)(schema)
    errors: list[str] = []
    for error in validator.iter_errors(document):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{location}: {error.message}")
    return sorted(errors)


_COMMENT_RE = re.compile(r"(^|\s)#")


def comment_lines(text: str) -> int:
    """Lines of ``text`` that carry a YAML comment (conservative: a ``#`` that
    starts a line or follows whitespace). ``render`` drops every comment."""
    return sum(1 for line in text.splitlines() if _COMMENT_RE.search(line))


def render(document: Mapping[str, Any]) -> str:
    """Render a workflow document as YAML, preserving key order."""
    return yaml.safe_dump(
        dict(document),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def sha256_text(text: str) -> str:
    """Return the hex sha256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Return the hex sha256 of a file's UTF-8 text, or ``""`` when absent."""
    workflow_path = Path(path)
    try:
        text = workflow_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return sha256_text(text)


def materialize(
    workflow_path: str | Path,
    edit: str | Mapping[str, Any],
    *,
    schema_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and compile one edit against a live workflow without writing it.

    Returns the parsed edit, its signature, before/after text and hashes, and
    the before/after topological order.  ``workflow_path`` is only read.
    """
    parsed = parse_edit(edit)
    path = Path(workflow_path)

    try:
        before_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkflowEditError(f"workflow not found: {workflow_path}") from exc
    except (OSError, UnicodeError) as exc:
        raise WorkflowEditError(f"cannot read workflow {workflow_path}: {exc}") from exc

    try:
        base = yaml.safe_load(before_text)
    except yaml.YAMLError as exc:
        raise WorkflowEditError(f"base workflow is not a YAML mapping: {exc}") from exc
    if not isinstance(base, Mapping):
        raise WorkflowEditError("base workflow is not a YAML mapping")

    base_errors = schema_errors(base, schema_path=schema_path)
    if base_errors:
        raise WorkflowEditError(f"base workflow violates the schema: {base_errors[0]}")

    try:
        compiled_before = compile_workflow(path)
    except WorkflowCompileError as exc:
        raise WorkflowEditError(f"base workflow does not compile: {exc}") from exc

    after = apply_edit(base, parsed)
    after_errors = schema_errors(after, schema_path=schema_path)
    if after_errors:
        raise WorkflowEditError(f"edited workflow violates the schema: {after_errors[0]}")

    after_text = render(after)
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir) / "workflow.yaml"
        temp_path.write_text(after_text, encoding="utf-8")
        try:
            compiled_after = compile_workflow(temp_path)
        except WorkflowCompileError as exc:
            raise WorkflowEditError(f"edited workflow does not compile: {exc}") from exc

    before_order = list(compiled_before.topological_order)
    after_order = list(compiled_after.topological_order)
    if parsed["op"] == "node_order" and after_order == before_order:
        raise WorkflowEditError("no-op: execution order unchanged")

    return {
        "edit": parsed,
        "signature": edit_signature(parsed),
        "path": str(workflow_path),
        "before_text": before_text,
        "after_text": after_text,
        "before_sha256": sha256_text(before_text),
        "after_sha256": sha256_text(after_text),
        "before_order": before_order,
        "after_order": after_order,
        # ``render`` re-emits the whole document, so every comment and the
        # original layout are lost on write; apply_to_file refuses that unless
        # the caller passes allow_reformat=True.
        "comments_dropped": comment_lines(before_text),
    }


def apply_to_file(workflow_path: str | Path, result: Mapping[str, Any], *,
                  allow_reformat: bool = False) -> bool:
    """Write ``result['after_text']`` iff the file still matches its before hash.

    The write re-renders the whole document, which drops every YAML comment and
    the original layout (``rollback`` restores both byte-for-byte). It is refused
    with ``WorkflowEditError`` when the live file carries comments, unless the
    caller passes ``allow_reformat=True`` deliberately.
    """
    dropped = int(result.get("comments_dropped", 0) or 0)
    if dropped and not allow_reformat:
        raise WorkflowEditError(
            f"refusing to write {workflow_path}: the re-render would drop "
            f"{dropped} comment line(s) and the original layout; pass "
            "allow_reformat=True to accept (rollback restores the original bytes)"
        )
    if file_sha256(workflow_path) != result["before_sha256"]:
        return False
    Path(workflow_path).write_text(result["after_text"], encoding="utf-8")
    return True


def rollback(workflow_path: str | Path, result: Mapping[str, Any]) -> bool:
    """Restore ``result['before_text']`` iff the file still matches its after hash."""
    if file_sha256(workflow_path) != result["after_sha256"]:
        return False
    Path(workflow_path).write_text(result["before_text"], encoding="utf-8")
    return True
