"""``board node <run> <node> --view artifacts`` — what the node was given and produced.

Read-only helper. Builds ``{"inputs": [...], "outputs": [...]}`` lists where each
artifact is ``{"name", "path", "size", "kind", "preview", "from"}``. Only files
that exist on disk are included. JSON pretty-prints previews; text previews are
capped at :data:`PREVIEW_LINES_CAP` lines.

Lazy-imports :func:`mini_ork.ide_pages.node._resolve_session_path` and
:func:`mini_ork.ide_pages.node._report_paths` to break the import cycle with
``node.py`` (which already imports this module's sibling
``node_changes`` at module top — see ``node_changes.py:201`` for the same
lazy-import pattern).

The :data:`WRITE_TOOLS` set is local on purpose: ``node.py`` ``_EDIT_TOOLS``
(``{Edit, MultiEdit, apply_diff}``) is the stream view's narrow contract and
must NOT be widened. ``Write`` is rendered separately for the ``agent_edits``
diff view (kickoff §2 — whole content as ``+`` lines).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mini_ork.ide_pages.run import Run, Node

# ── caps (kickoff §1, §2 — fresh constants; do NOT reuse node_changes caps) ──

PREVIEW_LINES_CAP = 60
DIFF_LINES_CAP = 4_000

# File extension → kind (kickoff §1). No existing helper in node_changes or
# node matches this exact set; classify by suffix and fall back to "text".
_KIND_BY_SUFFIX = {
    ".md": "markdown",
    ".json": "json",
    ".patch": "diff",
    ".diff": "diff",
    ".log": "log",
}

# Tool names whose input carries an old/new body the ``agent_edits`` view
# renders as a diff. Mirrors ``node.py`` ``_EDIT_TOOLS`` but redefines it here
# so we can extend with ``Write`` without widening the stream view's narrow
# set (kickoff §2 — ``Write`` = whole content as ``+`` lines).
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "apply_diff"})
WRITE_TOOLS = frozenset({"Write"})

# Run-level inputs (kickoff §1): kickoff_path comes from run.row; the other
# two are the run-dir baseline files.
_RUN_LEVEL_FALLBACKS = ("plan.json", "context-pack.json")

# Edge types that pass data from parent → child (i.e. edges that contribute
# to the child's ``inputs`` list). Other edge types are control flow:
# ``escalates_to`` / ``retries`` / ``blocks`` are back-edges or guards,
# ``human_decision_gate`` / ``verifies_user_choice`` are gate semantics.
# A missing ``edge_type`` defaults to ``depends_on`` (back-compat for recipes
# that omit it).
_DATA_EDGE_TYPES = frozenset({"depends_on", "supplies_context_to", "verifies"})


def _normalize_ports(value: Any) -> list[str]:
    """Normalize a workflow node's ``inputs`` or ``outputs`` port declaration.

    Two shapes accepted per :data:`workflow.schema.json`:
      * ``inputs``  : list[str] | dict[name, {required: bool}]  (keys = names)
      * ``outputs`` : list[str | {name, kind, path, ...}]       (path/name → path)

    Returns run-dir-relative path/name strings (empty list on missing/wrong type).
    """
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item:
                out.append(item)
            elif isinstance(item, dict):
                # Prefer ``path`` (concrete file) over ``name`` (port identifier).
                p = item.get("path") or item.get("name")
                if p:
                    out.append(str(p))
        return out
    if isinstance(value, dict):
        # ``inputs: { source_corpus: { required: true } }`` → keys are names.
        return [str(k) for k in value.keys() if isinstance(k, str) and k]
    return []


def build_artifacts_view(run: Run, node: Node) -> dict[str, Any]:
    """One DAG node's ``artifacts`` view payload.

    Shape::

        {"inputs":  [{"name", "path", "size", "kind", "preview", "from"}, ...],
         "outputs": [{"name", "path", "size", "kind", "preview", "from"}, ...]}

    Each artifact's ``from`` is the producing node id (outputs) or ``"run"``
    (inputs). Only files that exist on disk are included. Order within each
    list is: declared workflow ports first, then by-node-kind fallback, then
    transcript-derived writes (deduplicated by absolute path).
    """
    return {
        "inputs": _inputs_for(run, node),
        "outputs": _outputs_for(run, node),
    }


# ── outputs ────────────────────────────────────────────────────────────────


def _outputs_for(run: Run, node: Node) -> list[dict[str, Any]]:
    """The node's outputs: declared workflow ports OR by-node-kind (declared
    WIN, suppressing the fallback), plus transcript ``Write`` tool calls.
    """
    nid = str(node.id)
    candidates: list[tuple[Path, str]] = list(_resolved_outputs_paths(run, node))

    # Transcript-derived Write tool calls (always added on top).
    for path in _write_paths_for(run, node):
        candidates.append((path, nid))

    return _dedup_artifacts(candidates)


# ── inputs ─────────────────────────────────────────────────────────────────


def _inputs_for(run: Run, node: Node) -> list[dict[str, Any]]:
    """The node's inputs: declared ports, parents' outputs, run-level inputs,
    plus any file the rendered prompt names that exists in the run dir.

    Review item #4: declared workflow inputs REPLACE the run-level fallback
    (parents' outputs + ``kickoff_path`` + ``plan.json`` / ``context-pack.json``).
    Without this, a node with ``inputs: [kickoff.md]`` would also see every
    run-level file (duplicate noise) AND every parent's output (leaking
    parent-child coupling into the declared-port contract). Prompt-named files
    are ALWAYS added on top (reviewer inputs include ``review-diff.patch``
    when its prompt names it, even if the node declared different inputs).
    """
    run_dir = run.run_dir
    nid = str(node.id)

    candidates: list[tuple[Path, str]] = []
    ports = _declared_ports(run, nid)
    declared_inputs = list(ports.get("inputs", []))

    if declared_inputs:
        # Declared inputs REPLACE the fallback (parents + run-level).
        for name in declared_inputs:
            candidates.append((run_dir / name, "run"))
    else:
        # Fallback: parents' outputs (DAG edges into this node — filtered by
        # edge_type in :func:`_parent_outputs`), then run-level baseline.
        for path, from_id in _parent_outputs(run, nid):
            candidates.append((path, from_id))
        kickoff = str((run.row or {}).get("kickoff_path") or "")
        if kickoff:
            candidates.append((Path(kickoff), "run"))
        for name in _RUN_LEVEL_FALLBACKS:
            candidates.append((run_dir / name, "run"))

    # Prompt-named files always included (reviewer special-case + generic scan).
    ntype = str(node.type or "")
    if ntype in ("reviewer", "eval", "judge", "synthesizer"):
        candidates.append((run_dir / "review-diff.patch", "run"))
    prompt_text = _rendered_prompt(run, node)
    if prompt_text:
        for path in _paths_named_in_text(prompt_text, run_dir):
            candidates.append((path, "run"))

    return _dedup_artifacts(candidates)


# ── shared artifact helpers ────────────────────────────────────────────────


def _dedup_artifacts(candidates: list[tuple[Path, str]]) -> list[dict[str, Any]]:
    """Resolve candidates to existing files, dedupe by absolute path, build dicts."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path, from_ in candidates:
        if not path.is_file():
            continue
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(_artifact_dict(path, from_=from_))
    return out


def _artifact_dict(path: Path, *, from_: str) -> dict[str, Any]:
    name = path.name
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    kind = _classify_kind(path)
    return {
        "name": name,
        "path": str(path),
        "size": size,
        "kind": kind,
        "preview": _preview(path, kind),
        "from": from_,
    }


def _classify_kind(path: Path) -> str:
    """Suffix-based kind (kickoff §1 taxonomy). Falls back to ``text``."""
    suffix = path.suffix.lower()
    return _KIND_BY_SUFFIX.get(suffix, "text")


def _preview(path: Path, kind: str) -> str:
    """First 60 lines for text/markdown/log/diff; JSON pretty-print otherwise.

    Empty string on read failure — the kickoff (run returned dicts, the view
    itself never crashes on a missing file because candidates are prefiltered
    with ``is_file()``).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if kind == "json":
        # Tolerant: strip leading non-JSON lines (DeprecationWarning prefix
        # mirrors ``node_changes._read_json``), pretty-print, cap at 60 lines.
        match = re.search(r"^\s*\{", text, re.MULTILINE)
        slice_text = text[match.start():] if match else text
        try:
            obj = json.loads(slice_text)
        except (json.JSONDecodeError, ValueError):
            return "\n".join(slice_text.splitlines()[:PREVIEW_LINES_CAP])
        pretty = json.dumps(obj, indent=2, sort_keys=True, default=str)
        return "\n".join(pretty.splitlines()[:PREVIEW_LINES_CAP])
    return "\n".join(text.splitlines()[:PREVIEW_LINES_CAP])


# ── workflow ports & parents ────────────────────────────────────────────────


def _declared_ports(run: Run, node_id: str) -> dict[str, list[str]]:
    """Parse ``workflow.yaml`` for ``node_id``'s declared ``inputs`` / ``outputs``.

    Returns ``{"inputs": [], "outputs": []}`` when the workflow has no such
    ports. ``Node`` (``run.py:46-60``) has no ports field, so we parse the
    YAML directly — adding fields to ``run.py`` would violate ``scope_allow``.

    Review item #1: both ``list[str]`` and the schema-shape
    ``list[{name, kind, path}]`` / ``dict[name, {required}]`` forms are
    normalized to a flat list of run-dir-relative path/name strings via
    :func:`_normalize_ports`.
    """
    recipe_dir = getattr(run, "recipe_dir", None)
    if recipe_dir is None:
        return {"inputs": [], "outputs": []}
    wf = _read_workflow(recipe_dir / "workflow.yaml")
    for n in wf.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        if str(n.get("name") or "") != node_id:
            continue
        return {
            "inputs": _normalize_ports(n.get("inputs")),
            "outputs": _normalize_ports(n.get("outputs")),
        }
    return {"inputs": [], "outputs": []}


def _resolved_outputs_paths(run: Run, node: Node) -> list[tuple[Path, str]]:
    """Declared OR by-kind outputs for ``node`` — review item #1+#2.

    Declared ``outputs`` ports REPLACE the by-kind fallback (they do not
    add to it): a workflow node with an ``outputs:`` list returns exactly
    that. When no declared outputs are present, the by-node-kind list
    (planner / researcher / reviewer / implementer / verifier / rollback /
    publisher) is used.
    """
    run_dir = run.run_dir
    nid = str(node.id)
    declared = list(_declared_ports(run, nid).get("outputs", []))
    if declared:
        return [(run_dir / name, nid) for name in declared]
    return list(_by_kind_outputs(run, node))


def _by_kind_outputs(run: Run, node: Node) -> list[tuple[Path, str]]:
    """The by-node-kind fallback list of ``(path, from_id)`` tuples.

    Reused by both ``_outputs_for`` and ``_parent_outputs`` so the same
    set of files is returned to a node's children whether the call site is
    the node itself or one of its descendants.
    """
    run_dir = run.run_dir
    nid = str(node.id)
    ntype = str(node.type or "")
    out: list[tuple[Path, str]] = []
    if ntype in ("planner", "decomposer"):
        out.append((run_dir / "plan.json", nid))
    elif ntype == "researcher":
        for p in _node_report_paths(run_dir, nid):
            out.append((p, nid))
        if _is_review_node(ntype):
            out.append((run_dir / f"review-{nid}.json", nid))
            out.append((run_dir / f"review-{nid}.json.stdout.md", nid))
    elif ntype in ("reviewer", "eval", "judge", "lens", "synthesizer"):
        out.append((run_dir / f"review-{nid}.json", nid))
        out.append((run_dir / f"review-{nid}.json.stdout.md", nid))
        for p in _node_report_paths(run_dir, nid):
            out.append((p, nid))
    elif ntype == "implementer":
        out.append((run_dir / "implementer-summary.json", nid))
        out.append((run_dir / f"impl-{nid}.log", nid))
        out.append((run_dir / "framework-edit.diff", nid))
    elif ntype in ("verifier", "test", "typecheck", "static_check"):
        stem = _verifier_stem(node)
        out.append((run_dir / f"verifier_{stem}.json", nid))
        out.append((run_dir / f"verifier-{stem}.log", nid))
        out.append((run_dir / f"verifier-{stem}.checks.tsv", nid))
        evidence_dir = run_dir / "evidence"
        if evidence_dir.is_dir():
            ev_logs = sorted(
                (p for p in evidence_dir.glob(f"{stem}*.log") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if ev_logs:
                out.append((ev_logs[0], nid))
        out.append((run_dir / "node-cmd" / f"verifier_{stem}.json", nid))
    elif ntype == "rollback":
        out.append((run_dir / "rolled-back.json", nid))
        out.append((run_dir / "salvage.json", nid))
        out.append((run_dir / "salvage.patch", nid))
    elif ntype == "publisher":
        for name in ("verdict.json", "run-verdict.json", "publish.json"):
            out.append((run_dir / name, nid))
    return out


def _parent_outputs(run: Run, node_id: str) -> list[tuple[Path, str]]:
    """Walk the workflow DAG for nodes that edge into ``node_id``; resolve
    each parent's outputs (declared OR by-kind fallback) with
    ``from=<parent id>`` (review item #1).

    The parent ``Node`` is looked up in ``run.nodes`` so ``_verifier_stem`` /
    ``_node_report_paths`` can see its ``prompt`` and ``id`` — those helpers
    rely on the dataclass, not on the workflow YAML.

    Review item #2: only data-flow edges (``depends_on`` /
    ``supplies_context_to`` / ``verifies``) count as parent → child inputs.
    Back-edges (``escalates_to`` / ``retries`` / ``blocks``) and gate edges
    (``human_decision_gate`` / ``verifies_user_choice``) are control flow
    and must NOT feed the child's inputs. Missing/empty ``edge_type``
    defaults to ``depends_on`` for back-compat with the test fixture
    (``implementer → reviewer`` has no ``edge_type`` and must still count).
    """
    recipe_dir = getattr(run, "recipe_dir", None)
    if recipe_dir is None:
        return []
    wf = _read_workflow(recipe_dir / "workflow.yaml")
    parent_ids: set[str] = set()
    for e in wf.get("edges") or wf.get("dependencies") or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("to") or "") != node_id:
            continue
        et = e.get("edge_type")
        if et in (None, ""):
            et = "depends_on"
        if et not in _DATA_EDGE_TYPES:
            continue
        f = str(e.get("from") or "")
        if f:
            parent_ids.add(f)
    out: list[tuple[Path, str]] = []
    for pid in parent_ids:
        parent_node = next((n for n in run.nodes if str(n.id) == pid), None)
        if parent_node is None:
            continue
        for path, from_id in _resolved_outputs_paths(run, parent_node):
            out.append((path, from_id))
    return out


def _read_workflow(path: Path) -> dict[str, Any]:
    """Tolerant YAML reader for ``workflow.yaml``."""
    if not path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — a typo in workflow.yaml must not break the view
        return {}
    return data if isinstance(data, dict) else {}


# ── transcript scan ─────────────────────────────────────────────────────────


def _write_paths_for(run: Run, node: Node) -> list[Path]:
    """Absolute paths from the agent's ``Write`` tool calls in its transcript,
    restricted to ``run_dir``.

    Uses the session resolver (``_resolve_session_path``) so a missing session
    silently returns ``[]``. Cycles are broken by the lazy import inside the
    helper.

    Review item #3: only paths that resolve INSIDE ``run_dir`` are returned.
    An agent that writes ``/path/to/repo/mini_ork/foo.py`` would otherwise
    leak repo source files into the artifacts view — they're not run
    artefacts, so they must NOT appear as the node's outputs.
    """
    session_path = _session_path_or_none(run, node)
    if session_path is None or not session_path.is_file():
        return []
    try:
        run_dir_resolved = run.run_dir.resolve()
    except OSError:
        run_dir_resolved = run.run_dir
    paths: list[Path] = []
    for entry in _read_jsonl_safe(session_path):
        if str(entry.get("type") or "") != "assistant":
            continue
        msg_raw = entry.get("message")
        msg: dict[str, Any] = msg_raw if isinstance(msg_raw, dict) else {}
        for block_raw in (msg.get("content") or []):
            block: dict[str, Any] = block_raw if isinstance(block_raw, dict) else {}
            if str(block.get("type") or "") != "tool_use":
                continue
            if str(block.get("name") or "") not in WRITE_TOOLS:
                continue
            inp_raw = block.get("input")
            inp: dict[str, Any] = inp_raw if isinstance(inp_raw, dict) else {}
            fp = str(inp.get("file_path") or inp.get("path") or "")
            if not fp:
                continue
            p = Path(fp)
            try:
                p_resolved = p.resolve()
            except OSError:
                continue
            # Must be inside run_dir (or equal). Repo source files written
            # by the agent live OUTSIDE run_dir and must be filtered out.
            if p_resolved == run_dir_resolved or run_dir_resolved in p_resolved.parents:
                paths.append(p)
    return paths


def _session_path_or_none(run: Run, node: Node) -> Path | None:
    """Lazy-import the resolver to avoid an import cycle (see module docstring)."""
    from mini_ork.ide_pages.node import _resolve_session_path  # lazy: breaks cycle
    try:
        return _resolve_session_path(run, node)
    except Exception:  # noqa: BLE001 — a missing resolver must not break the view
        return None


def _node_report_paths(run_dir: Path, node_id: str) -> list[Path]:
    """Lazy-import :func:`mini_ork.ide_pages.node._report_paths`."""
    from mini_ork.ide_pages.node import _report_paths  # lazy: breaks cycle
    try:
        return _report_paths(run_dir, node_id)
    except Exception:  # noqa: BLE001
        return []


def _read_jsonl_safe(path: Path) -> list[dict[str, Any]]:
    """Tolerant JSONL reader mirroring ``mini_ork.ide_pages.node._read_jsonl``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ── prompt-named paths ──────────────────────────────────────────────────────


def _rendered_prompt(run: Run, node: Node) -> str:
    """Best-effort rendered prompt: first user message of the agent's session.

    Falls back to empty string — the lazy import of the session resolver is
    isolated so a failure here does not break the inputs list (just skips the
    prompt-scan step).

    Review fix #3: takes the loaded ``Run`` directly (the previous version
    looked at ``node._run`` which was never set; the lazy-import chain
    silently produced ``""`` and the prompt-scan branch was unreachable).
    """
    from mini_ork.ide_pages.node import _resolve_session_path  # lazy
    try:
        session_path = _resolve_session_path(run, node)
    except Exception:  # noqa: BLE001
        return ""
    if session_path is None or not session_path.is_file():
        return ""
    for entry in _read_jsonl_safe(session_path):
        if str(entry.get("type") or "") != "user":
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    return str(b.get("text") or "")
        return ""
    return ""


def _paths_named_in_text(text: str, run_dir: Path) -> list[Path]:
    """Pick out bare names / ``run_dir``-relative paths the prompt mentions.

    Only paths whose name matches a file in the run dir are returned. Heuristic:
    extract word-shaped tokens ending in ``.md/.json/.log/.diff/.patch/.txt``,
    or tokens beginning with ``runs/<id>/`` (or just ``<run_id>/``); resolve
    against ``run_dir`` when ``is_file()``.
    """
    candidates: list[Path] = []
    seen: set[str] = set()
    for tok in _PATH_TOKEN_RE.findall(text):
        if tok in seen:
            continue
        seen.add(tok)
        # Run-dir relative: strip leading "runs/<id>/" or "<run_id>/" if present
        stripped = re.sub(r"^(?:runs/[^/\s]+/|[^/\s]+/)?", "", tok)
        # Try the bare token first, then the stripped form.
        for name in (tok, stripped):
            p = run_dir / name
            if p.is_file() and str(p) not in seen:
                candidates.append(p)
                seen.add(str(p))
                break
    return candidates


_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.+\-]+\.(?:md|json|log|diff|patch|txt)")

# ── helpers consumed by node_changes for the agent_edits diff view ──────────


def edit_tool_calls(session_path: Path | None) -> list[tuple[str, str, dict[str, Any]]]:
    """Return ``(tool_name, file_path, input)`` tuples for each Edit / MultiEdit /
    Write tool call in the transcript, in order. Used by
    :func:`mini_ork.ide_pages.node_changes.build_changes_view` to render
    ``agent_edits``.
    """
    if session_path is None or not session_path.is_file():
        return []
    out: list[tuple[str, str, dict[str, Any]]] = []
    for entry in _read_jsonl_safe(session_path):
        if str(entry.get("type") or "") != "assistant":
            continue
        msg_raw = entry.get("message")
        msg: dict[str, Any] = msg_raw if isinstance(msg_raw, dict) else {}
        for block_raw in (msg.get("content") or []):
            block: dict[str, Any] = block_raw if isinstance(block_raw, dict) else {}
            if str(block.get("type") or "") != "tool_use":
                continue
            name = str(block.get("name") or "")
            if name not in (EDIT_TOOLS | WRITE_TOOLS):
                continue
            inp_raw = block.get("input")
            inp: dict[str, Any] = inp_raw if isinstance(inp_raw, dict) else {}
            fp = str(inp.get("file_path") or inp.get("path") or "")
            out.append((name, fp, inp))
    return out


def render_edit_diff(tool_name: str, file_path: str, inp: dict[str, Any]) -> str:
    """Render one edit-family tool call as ``- old`` / ``+ new`` lines.

    Full length, no per-line cap. The kickoff caps the *total* at
    :data:`DIFF_LINES_CAP` lines (in the caller); this helper renders one call
    fully so the caller can truncate the joined text.
    """
    lines: list[str] = []
    label = file_path or "<unnamed>"
    if tool_name == "Edit":
        old = str(inp.get("old_string") or "")
        new = str(inp.get("new_string") or "")
        lines.append(f"--- {label}")
        lines.append("+++ (Edit)")
        for ln in old.splitlines():
            lines.append(f"- {ln}")
        for ln in new.splitlines():
            lines.append(f"+ {ln}")
    elif tool_name == "MultiEdit":
        edits = inp.get("edits") or []
        if not isinstance(edits, list):
            edits = []
        lines.append(f"--- {label}")
        lines.append(f"+++ (MultiEdit · {len(edits)} edit(s))")
        for idx, edit in enumerate(edits, 1):
            if not isinstance(edit, dict):
                continue
            old = str(edit.get("old_string") or "")
            new = str(edit.get("new_string") or "")
            lines.append(f"@@ edit {idx} @@")
            for ln in old.splitlines():
                lines.append(f"- {ln}")
            for ln in new.splitlines():
                lines.append(f"+ {ln}")
    elif tool_name == "Write":
        content = str(inp.get("content") or "")
        lines.append("--- /dev/null")
        lines.append(f"+++ {label}")
        for ln in content.splitlines():
            lines.append(f"+ {ln}")
    else:
        # apply_diff: render the raw diff argument when present, else a one-liner.
        diff_text = str(inp.get("diff") or inp.get("patch") or "")
        if diff_text:
            lines.append(diff_text)
        else:
            lines.append(f"@@ {tool_name} (no inline diff) @@")
    return "\n".join(lines)


# ── predicate ──────────────────────────────────────────────────────────────


def _is_review_node(ntype: str) -> bool:
    return ntype in {"reviewer", "eval", "judge", "lens", "synthesizer"}


def _verifier_stem(node: Node) -> str:
    """``Path(node.prompt).stem`` when ``.py``; else ``node.id`` — mirrors
    :func:`mini_ork.ide_pages.node_changes._verifier_items`."""
    prompt = str(node.prompt or "")
    if prompt.endswith(".py"):
        return Path(prompt).stem
    return node.id