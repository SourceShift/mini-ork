"""Artifact-based completion helpers for LLM nodes.

An LLM node's dispatch reports ``(rc, text)``; that self-report is a handshake,
not the deliverable. When the agent wrote every declared output artifact but the
handshake failed (watchdog rc=124 after the files landed, a malformed final
message), the executor used to fail the node and cascade-skip its dependents.
These helpers recompute the verdict from the artifacts instead, the same
principle the deterministic verifiers already encode.

Beyond the generic checks (regular, non-empty, fresh, JSON-parseable), a
registry of per-kind *completion contracts* adds shape checks for well-known
artifact basenames (``implementer-summary.json``, ``plan.json``, ``*.diff`` /
``*.patch``). A contract is ``fn(path, since_mtime) -> str | None`` returning
``None`` when the artifact proves delivery or a reason string when it does not;
unknown basenames keep exactly the generic checks. Contracts are registered via
``register_completion_contract`` (mirroring ``gate_registry``), so new kinds land
without editing the dispatcher.

Everything here is side-effect free except ``write_json_atomic`` and
``normalize_implementer_summary_file``, which rewrite one JSON file atomically.
"""
from __future__ import annotations

import contextvars
import fnmatch
import json
import os
import stat
import tempfile
from typing import Any, Callable, Iterable, Mapping

import yaml

ARTIFACT_COMPLETION_LOG = "[ok] node completed via artifact check (self-report missing/invalid)"


def _str_entries(value: Any) -> list[str]:
    """Non-empty string entries of ``value`` when it is a list, else []."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry]


def normalize_implementer_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``summary`` in the canonical tier-verifier shape.

    The recursive-validate-impl verifiers gate on ``ready_for_tier1 is True``
    and a non-empty ``touched_files``. Agents (and, until K5, the engine's own
    summary writer) also emit ``files_changed``, so:

    * ``touched_files`` missing/empty → taken from a non-empty ``files_changed``;
      absent with nothing to infer from → ``[]``.
    * ``ready_for_tier1`` that is not a bool → ``True`` only when
      ``touched_files`` is non-empty, else ``False``. An explicit bool from the
      agent is preserved, so a real "not ready" is never masked.

    Every other key is kept. Idempotent on its own output.
    """
    normalized = dict(summary)
    if not _str_entries(normalized.get("touched_files")):
        changed = _str_entries(normalized.get("files_changed"))
        if changed:
            normalized["touched_files"] = changed
        elif "touched_files" not in normalized:
            normalized["touched_files"] = []
    if not isinstance(normalized.get("ready_for_tier1"), bool):
        normalized["ready_for_tier1"] = bool(_str_entries(normalized.get("touched_files")))
    return normalized


def write_json_atomic(path: str, payload: Any) -> None:
    """Write ``payload`` to ``path`` via a same-directory temp file + os.replace,
    so a concurrent verifier never reads a half-written summary."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def normalize_implementer_summary_file(path: str) -> bool:
    """Normalize an on-disk implementer summary in place.

    Rewrites only when normalization changed something. Returns True when the
    file holds a valid, normalized summary; False (file untouched) when it is
    missing, unreadable, not a JSON object, or cannot be rewritten.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(summary, dict):
        return False
    normalized = normalize_implementer_summary(summary)
    if normalized == summary:
        return True
    try:
        write_json_atomic(path, normalized)
    except OSError:
        return False
    return True


# ── Completion-contract registry ────────────────────────────────────────────
#
# The generic checks (regular / non-empty / fresh / JSON-parseable) cannot tell
# a well-formed fabrication from a real deliverable: an agent that writes
# ``{"files_changed": []}`` to implementer-summary.json, or a prose blob to a
# ``.patch``, passes them. Contracts add the per-kind shape a real deliverable
# must have. They mirror ``mini_ork.gates.gate_registry``: a module-level
# registry plus a ``register_*`` entry point, so a new artifact kind lands
# without editing the dispatcher.

#: A contract maps an artifact path + the dispatch-start mtime to a reason
#: string when the artifact does NOT prove delivery, else ``None``.
ContractFn = Callable[[str, float], "str | None"]

_COMPLETION_CONTRACTS: list[tuple[str, ContractFn]] = []


def register_completion_contract(pattern: str, fn: ContractFn) -> None:
    """Register (or replace) the completion contract for artifact basenames
    matching the ``fnmatch`` glob ``pattern``.

    ``fn`` is called as ``fn(path, since_mtime)`` and returns ``None`` when the
    artifact proves the node delivered, or a reason string (the caller prefixes
    the path) when it does not. A later registration for the same pattern
    replaces the earlier one in place; the first matching pattern wins.
    """
    for index, (existing, _fn) in enumerate(_COMPLETION_CONTRACTS):
        if existing == pattern:
            _COMPLETION_CONTRACTS[index] = (pattern, fn)
            return
    _COMPLETION_CONTRACTS.append((pattern, fn))


def _completion_contract_for(basename: str) -> ContractFn | None:
    """The first registered contract whose pattern matches ``basename``, if any."""
    for pattern, fn in _COMPLETION_CONTRACTS:
        if fnmatch.fnmatch(basename, pattern):
            return fn
    return None


# The target repo a relative path in «implementer-summary.json» resolves
# against. ``declared_artifacts_ok`` binds it for the duration of its check (an
# explicit ``repo_root`` wins over ``MO_TARGET_CWD``, else cwd) so the contract
# keeps the documented ``fn(path, since_mtime)`` signature. A contextvar — not
# a module global — because concurrent runs share the process.
_CONTRACT_REPO_ROOT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mini_ork_contract_repo_root", default=""
)


def _effective_repo_root(repo_root: str | None) -> str:
    """The target repo for relative-path checks: explicit ``repo_root`` >
    ``MO_TARGET_CWD`` > cwd (absolute)."""
    if repo_root:
        return os.path.abspath(repo_root)
    from mini_ork.context import ENV_TARGET_CWD, context_env

    env_root = context_env(ENV_TARGET_CWD)
    return os.path.abspath(env_root) if env_root else os.getcwd()


def _implementer_summary_contract(path: str, since_mtime: float) -> str | None:
    """A summary whose claimed files actually exist (and are fresh).

    ``normalize_implementer_summary`` finds the claims (``touched_files`` /
    ``files_changed``). An empty claim list only completes when the agent
    *honestly abstains* — ``ready_for_tier1`` is exactly ``False`` AND a
    non-empty ``block_reason`` explains why (a "no repro" report is a real
    deliverable). Otherwise every claimed relative path must exist as a regular
    file under the target repo, modified at or after ``since_mtime``; an
    absolute path is shape-checked only (run directories move between hosts).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
    except (OSError, ValueError) as exc:
        return f"implementer-summary.json contract: unreadable ({exc})"
    if not isinstance(summary, dict):
        return "implementer-summary.json contract: not a JSON object"
    normalized = normalize_implementer_summary(summary)

    block_reason = normalized.get("block_reason")
    if normalized.get("ready_for_tier1") is False and isinstance(block_reason, str) and block_reason.strip():
        return None  # honest abstention: nothing claimed, so nothing to verify

    claimed = _str_entries(normalized.get("touched_files"))
    if not claimed:
        return "implementer-summary.json contract: files_changed/touched_files is empty"

    repo_root = _CONTRACT_REPO_ROOT.get()
    for entry in claimed:
        if os.path.isabs(entry):
            continue
        if not repo_root:
            return (f"implementer-summary.json contract: cannot verify relative path "
                    f"{entry!r} — no target repo root")
        resolved = os.path.join(repo_root, entry)
        try:
            info = os.stat(resolved)
        except OSError:
            return (f"implementer-summary.json contract: claimed file {entry!r} "
                    f"is missing under the target repo")
        if not stat.S_ISREG(info.st_mode):
            return f"implementer-summary.json contract: claimed file {entry!r} is not a regular file"
        if info.st_mtime < since_mtime:
            return f"implementer-summary.json contract: claimed file {entry!r} is stale"
    return None


def _plan_json_contract(path: str, since_mtime: float) -> str | None:
    """A plan is a JSON object with a non-empty list of object ``steps``."""
    try:
        with open(path, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError) as exc:
        return f"plan.json contract: unreadable ({exc})"
    if not isinstance(plan, dict):
        return "plan.json contract: not a JSON object"
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return "plan.json contract: steps is empty"
    if not all(isinstance(step, dict) for step in steps):
        return "plan.json contract: steps entries must be objects"
    return None


def _patch_contract(path: str, since_mtime: float) -> str | None:
    """A diff/patch carries at least one ``--- `` / ``+++ `` header pair.

    Prose-only or truncated output does not, so it cannot stand in for a
    deliverable just because the file is non-empty and fresh.
    """
    del since_mtime  # freshness is the generic check's job
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return f"patch contract: unreadable ({exc})"
    lines = text.splitlines()
    if not any(line.startswith("--- ") for line in lines) or not any(
        line.startswith("+++ ") for line in lines
    ):
        return "patch contract: missing a '--- '/'+++ ' header pair"
    return None


register_completion_contract("implementer-summary.json", _implementer_summary_contract)
register_completion_contract("plan.json", _plan_json_contract)
register_completion_contract("*.diff", _patch_contract)
register_completion_contract("*.patch", _patch_contract)


def _default_artifact_violation(path: str, since_mtime: float) -> str | None:
    """The generic per-path check shared by every artifact kind: a regular,
    non-empty file modified at/after ``since_mtime``, JSON-parseable if it ends
    in ``.json``. Returns the reason suffix (``"missing"``, ``"empty"``, …) or
    ``None`` when the path passes."""
    try:
        info = os.stat(path)
    except OSError:
        return "missing"
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_size <= 0:
        return "empty"
    if info.st_mtime < since_mtime:
        return "stale (written before this dispatch started)"
    if path.endswith(".json"):
        try:
            with open(path, encoding="utf-8") as handle:
                json.load(handle)
        except (OSError, ValueError) as exc:
            return f"invalid JSON ({exc})"
    return None


def _evaluate_declared_artifacts(
    paths: Iterable[str], *, since_mtime: float, repo_root: str | None = None
) -> tuple[bool, str, dict[str, str]]:
    """The shared engine behind ``declared_artifacts_ok`` / ``_report``.

    Returns ``(ok, reason, report)`` where ``report`` maps each checked
    basename to ``"pass"`` (or the fail reason for the first violation).
    """
    paths = [os.fspath(path) for path in paths]
    if not paths:
        return False, "no_declared_artifacts", {}
    report: dict[str, str] = {}
    token = _CONTRACT_REPO_ROOT.set(_effective_repo_root(repo_root))
    try:
        for path in paths:
            basename = os.path.basename(path)
            violation = _default_artifact_violation(path, since_mtime)
            if violation is None:
                contract = _completion_contract_for(basename)
                if contract is not None:
                    violation = contract(path, since_mtime)
            if violation is not None:
                report[basename] = violation
                return False, f"{path}: {violation}", report
            report[basename] = "pass"
    finally:
        _CONTRACT_REPO_ROOT.reset(token)
    return True, "ok", report


def declared_artifacts_ok(
    paths: Iterable[str], *, since_mtime: float, repo_root: str | None = None
) -> tuple[bool, str]:
    """Check that every declared artifact proves the node delivered.

    Each path must be a regular file, non-empty, modified at or after
    ``since_mtime`` (the dispatch-start marker — so a previous recursion
    iteration's artifacts cannot stand in for this one's), and parse as JSON
    when it ends in ``.json``. A path whose basename matches a registered
    completion contract (see ``register_completion_contract``) must also satisfy
    that contract's shape; unknown basenames keep exactly the generic checks.
    Returns ``(ok, reason)``; the reason names the first violating path.

    ``repo_root`` is the target repo relative claims in
    «implementer-summary.json» resolve against; it defaults to ``MO_TARGET_CWD``
    (else cwd) so the existing call site stays source-compatible.
    """
    ok, reason, _report = _evaluate_declared_artifacts(
        paths, since_mtime=since_mtime, repo_root=repo_root
    )
    return ok, reason


def declared_artifacts_report(
    paths: Iterable[str], *, since_mtime: float, repo_root: str | None = None
) -> dict[str, str]:
    """Per-artifact completion outcome, keyed by basename.

    ``"pass"`` for a checked artifact that delivered (or its fail reason for the
    first violation). Used to surface contract outcomes to the run-event
    ``node.artifact_completion`` payload without changing the ``(ok, reason)``
    return of ``declared_artifacts_ok``.
    """
    _ok, _reason, report = _evaluate_declared_artifacts(
        paths, since_mtime=since_mtime, repo_root=repo_root
    )
    return report


def node_strict_handshake(compiled_workflow: Any, workflow_path: str, node_id: str) -> bool:
    """Whether ``node_id`` opted out of artifact-based completion.

    Reads the compiled node when available; otherwise the raw workflow YAML
    (legacy callers without a compiled graph). Any lookup failure → False.
    """
    nodes = getattr(compiled_workflow, "nodes", None)
    if isinstance(nodes, Mapping) and node_id in nodes:
        return getattr(nodes[node_id], "strict_handshake", False) is True
    if not workflow_path or not os.path.isfile(workflow_path):
        return False
    try:
        with open(workflow_path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return False
    raw_nodes = document.get("nodes") if isinstance(document, Mapping) else None
    for raw in raw_nodes if isinstance(raw_nodes, list) else []:
        if isinstance(raw, Mapping) and str(raw.get("name") or "").strip() == node_id:
            return raw.get("strict_handshake") is True
    return False
