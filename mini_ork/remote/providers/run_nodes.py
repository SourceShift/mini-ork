"""Per-node cloud sandboxes — one sandbox per workflow node.

:func:`~mini_ork.remote.providers.base.acquire_run_node` gives a run ONE node
that every dispatch in the run shares. This module is that same mechanism keyed
on ``(run_id, node_id)`` instead: with ``MO_NODE_ISOLATION=1`` every workflow
node gets its own sandbox, so a node's agent, the secrets it was scoped, and
its process tree never share a machine with a sibling node. That is the
security property the shared sandbox cannot give: today one sandbox hosts the
whole run, so the reviewer's machine also holds the implementer's key.

What does NOT change is the control plane. The plan, the run dir, ``state.db``,
the UI, git and the publisher all stay local, and **the local worktree is the
hub between nodes** — a node's replica syncs down into it at teardown
(``RemoteWorkspace._sync_down``) and the next node syncs up from it
(``RemoteWorkspace._sync_up``). A sequential recipe therefore needs no cloud
filesystem to hand work from one node to the next; the handoff crosses
sandboxes through the control plane, which is already the only writer to the
worktree. Parallel fan-out (two nodes writing the same path at once) still
needs the content-addressed transport in
``docs/architecture/artifact-transport.md`` — per-node isolation does not
depend on it, and does not deliver it.

Record layout. The run-level record stays where it was, untouched, at
``<run_dir>/.node-provider.json``. Each node's record goes in its own
directory, ``<run_dir>/.nodes/<node_id>/.node-provider.json``, with its own
``.node-provider.lock`` next to it. That is the whole trick: the base module's
lock and record helpers are already path-parameterized, so handing them a
per-node directory makes them per-node with no new locking code to get wrong.
The *directory* is the key.

Two writers can race here exactly as they do at run level — several dispatch
processes of one run, and a `kill_run` teardown beside them. The per-node lock
serializes the check-then-act pair (attach-or-create), and
:func:`release_all_nodes` sweeps every node's record rather than trusting the
caller to name them.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from mini_ork.remote.providers.base import (
    # The per-node lock IS the run-level lock, handed a per-node directory.
    # Importing the private names is deliberate: a second copy of an flock
    # protocol (and of the atomic record write beside it) is how the two
    # drift, and the drift would show up as a duplicated sandbox, not a test
    # failure.
    _RunLock,
    _write_record,
    NodeEndpoint,
    NodeProvider,
    get_node_provider,
    node_record_path,
    read_run_node_record,
)

__all__ = [
    "ENV_NODE_ID",
    "ENV_NODE_ISOLATION",
    "NodeIsolatedProvider",
    "acquire_node",
    "attach_node",
    "isolated_node",
    "isolation_enabled",
    "list_node_ids",
    "node_dir",
    "node_id_for",
    "nodes_dir",
    "read_node_record",
    "release_all_nodes",
    "release_node",
    "safe_node_id",
]


class NodeIsolatedProvider(NodeProvider, Protocol):
    """A :class:`NodeProvider` that can also mint a sandbox for one node.

    Declared here, not by widening ``base.NodeProvider``, for two reasons. The
    base contract should keep saying exactly what *every* provider must honor;
    and per-node sandboxing is a capability a provider opts into, so a provider
    written before it must stay valid — it simply does not get per-node
    sandboxes, and :func:`acquire_node` fails loudly rather than quietly
    handing it a shared one. ``SandboxNodeProvider`` and every cloud built on
    it satisfy this structurally.
    """

    def create(self, *, run_id: str, profile: Any, env: Mapping[str, str],
               engine_root: str, node_id: str = "") -> NodeEndpoint: ...

#: Truthy in the control plane's env to give every workflow node its own
#: sandbox. Off (absent) keeps the historical one-sandbox-per-run behavior.
ENV_NODE_ISOLATION = "MO_NODE_ISOLATION"
#: Set by the execute loop before every node dispatch — the node this process
#: is working on. Absent outside a dispatch (an ad-hoc CLI call).
ENV_NODE_ID = "MO_NODE_ID"

NODES_DIR = ".nodes"
_MAX_NODE_ID = 100
#: The characters the run dir already spells a node with (``dispatch.providers``
#: / ``acp.agent``). Kept as one compiled pattern so :func:`safe_node_id` and
#: those writers cannot drift apart by a character.
_NODE_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# --- policy -------------------------------------------------------------------


def isolation_enabled(env: Mapping[str, str]) -> bool:
    """Whether per-node sandboxing is asked for.

    Deliberately a plain env flag, and deliberately failing *closed* to the
    shared sandbox: an unreadable or absent value keeps today's behavior, so a
    deployment that never sets it cannot silently start paying for N sandboxes.
    """
    return (env.get(ENV_NODE_ISOLATION) or "").strip().lower() in _TRUTHY


def node_id_for(env: Mapping[str, str]) -> str:
    """The node this process is dispatching, or ``""`` outside a dispatch.

    ``env`` first (the dispatch layer passes a scoped mapping), then the
    run-scoped context — :func:`mini_ork.context.context_env` reads the
    contextvar binding before ``os.environ``, so a workspace built from a scoped
    env snapshot taken before the per-node publish still gets the right node.
    This mirrors the established reader in ``dispatch/providers.py`` rather than
    rolling a second one.
    """
    explicit = (env.get(ENV_NODE_ID) or "").strip()
    if explicit:
        return explicit
    from mini_ork.context import context_env

    return context_env(ENV_NODE_ID).strip()


def isolated_node(env: Mapping[str, str]) -> str:
    """The node this process gets its OWN sandbox for, or ``""`` for the shared one.

    The one place the two halves of the policy meet, so the session registry
    (which must key on the node) and the workspace (which must resolve the node)
    can never disagree about whether isolation is on. Empty means "one sandbox
    per run", exactly as before this module existed.

    A flag with no node to name yields ``""`` rather than raising: a dispatch
    outside a node (an ad-hoc CLI call, a check that runs between nodes) has no
    node to isolate and belongs on the run-level sandbox — the historical
    behavior, and the one that cannot strand a process with nowhere to run.
    """
    return node_id_for(env) if isolation_enabled(env) else ""


def safe_node_id(node_id: str) -> str:
    """A node id mapped to a single filesystem-safe path segment.

    The mapping is the one the run dir already spells a node with for its
    ``agent-<node>.stream.jsonl`` / ``.live.jsonl`` files
    (``dispatch.providers``, ``acp.agent``: ``[^A-Za-z0-9._-] -> "_"``), so a
    node's sandbox directory is named like the files beside it rather than
    becoming a fourth, divergent spelling of the same node.

    A node id is untrusted input to a path join, and the mapping answers that
    structurally: every separator becomes ``_``, so the result is one segment
    that cannot leave the run dir. Two shapes still have to be refused — an id
    that maps to nothing (``"   "``), and one that maps to only dots/hyphens
    (``".."``, which is a real parent-directory reference, dots being safe
    characters) — and both raise.

    Sanitizing is lossy: ``implementer:0`` and ``implementer_0`` map to the same
    string. For a stream file that is a cosmetic clash; for a sandbox key it
    means two nodes silently share one machine, which is the failure this module
    exists to prevent. So an id that had to be changed also carries a digest of
    its original form. Ids that are already safe — every id in practice — come
    back unchanged, keeping them identical to the artifact index's names, and
    the function is idempotent on its own output so a directory name read back
    by :func:`list_node_ids` round-trips.
    """
    node_id = node_id.strip()
    slug = _NODE_ID_UNSAFE.sub("_", node_id)
    if not slug or not slug.strip(".-"):
        raise ValueError(f"per-node sandbox: unusable node id {node_id!r}")
    if slug == node_id:
        return slug[:_MAX_NODE_ID]
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug[: _MAX_NODE_ID - len(digest) - 1]}-{digest}"


# --- record layout ------------------------------------------------------------


def nodes_dir(run_dir: str | os.PathLike[str]) -> Path:
    """``<run_dir>/.nodes`` — the parent of every per-node record directory."""
    return Path(run_dir) / NODES_DIR


def node_dir(run_dir: str | os.PathLike[str], node_id: str) -> Path:
    """``<run_dir>/.nodes/<node_id>`` — one node's record + lock directory."""
    return nodes_dir(run_dir) / safe_node_id(node_id)


def read_node_record(run_dir: str | os.PathLike[str], node_id: str) -> dict[str, Any] | None:
    """This node's recorded sandbox (``{provider, handle, run_id, node_id}``),
    or ``None`` when absent or unreadable."""
    return read_run_node_record(node_dir(run_dir, node_id))


def list_node_ids(run_dir: str | os.PathLike[str]) -> list[str]:
    """Every node with a record on disk, sorted. The authoritative list of what
    this run started — read from the filesystem, never from an env flag, so a
    teardown in a process that never saw the flag still finds them all."""
    base = nodes_dir(run_dir)
    try:
        found = [p.name for p in base.iterdir()
                 if p.is_dir() and node_record_path(p).exists()]
    except OSError:
        return []
    return sorted(found)


# --- the three verbs ----------------------------------------------------------


def acquire_node(provider: NodeIsolatedProvider, *, run_id: str, node_id: str,
                 run_dir: str | os.PathLike[str], profile: Any,
                 env: Mapping[str, str], engine_root: str) -> NodeEndpoint:
    """This node's sandbox: attach to its recorded one, or create it once.

    The per-node twin of :func:`acquire_run_node`, with the same protocol under
    this node's own lock: a record naming this provider whose sandbox still
    exists is attached; a record whose sandbox is gone (TTL-expired, deleted)
    is replaced by a fresh one; no record means create.

    A missing ``node_id`` raises rather than falling back to the run-level
    sandbox: silently sharing one sandbox would look like success and deliver
    the opposite of what the caller asked for.
    """
    if not run_id:
        raise ValueError("acquire_node requires a run_id")
    if not node_id:
        raise ValueError(
            "acquire_node requires a node_id — call acquire_run_node for the "
            "run-level sandbox, or set MO_NODE_ID")
    if not str(run_dir or "").strip():
        raise ValueError(
            f"node provider {provider.name!r} needs the run dir to share one node "
            "per (run, node) across the run's processes (MINI_ORK_RUN_DIR is unset)")
    nd = node_dir(run_dir, node_id)
    with _RunLock(nd, env):
        record = read_run_node_record(nd)
        if record and record.get("provider") == provider.name and record.get("handle"):
            node = provider.attach(str(record["handle"]), env=env)
            if node is not None:
                return node
        node = provider.create(run_id=run_id, profile=profile, env=env,
                               engine_root=engine_root, node_id=node_id)
        if node.handle:
            _write_record(nd, {
                "provider": provider.name, "handle": node.handle, "run_id": run_id,
                "node_id": node_id,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **({"replaced": record.get("handle")} if record else {}),
            })
        return node


def attach_node(run_dir: str | os.PathLike[str], env: Mapping[str, str],
                node_id: str) -> NodeEndpoint | None:
    """Re-find one node's sandbox without creating one — the teardown path."""
    if not node_id or not str(run_dir or "").strip():
        return None
    record = read_node_record(run_dir, node_id)
    if not record or not record.get("handle"):
        return None
    return get_node_provider(str(record["provider"])).attach(str(record["handle"]), env=env)


def release_node(run_dir: str | os.PathLike[str], env: Mapping[str, str],
                 node_id: str) -> bool:
    """Destroy one node's sandbox and drop its record. Best-effort, idempotent:
    ``True`` when a record existed and ``release`` ran without raising."""
    nd = node_dir(run_dir, node_id)
    record = read_run_node_record(nd)
    if not record or not record.get("handle"):
        return False
    try:
        get_node_provider(str(record["provider"])).release(str(record["handle"]), env=env)
    except Exception:  # noqa: BLE001 — teardown never raises; the TTL reaper is the net
        return False
    try:
        node_record_path(nd).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def release_all_nodes(run_dir: str | os.PathLike[str], env: Mapping[str, str]) -> int:
    """Free every per-node sandbox this run started; returns how many were freed.

    The run-level ``release_run_node`` cannot see these — they are keyed on
    ``(run_id, node_id)``, not on the run. Called unconditionally at run end
    and by ``kill_run``: the records on disk decide what to free, so a run whose
    flag was set for the dispatcher but not the teardown still frees everything
    (and an unfinished run that never wrote a record frees nothing)."""
    if not str(run_dir or "").strip():
        return 0
    released = 0
    for node_id in list_node_ids(run_dir):
        try:
            if release_node(run_dir, env, node_id):
                released += 1
        except Exception:  # noqa: BLE001 — one bad node must not strand the rest
            continue
    return released
