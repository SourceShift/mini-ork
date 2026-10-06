"""Nodes & sandboxes — where runs execute.

Reads the node registry (``config/nodes.yaml``, template + live), the
environment profiles, the per-run workspace-session markers and the labelled
Docker sandboxes. Never probes a node while building: ping and doctor are
buttons.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("nodes", "Nodes"), ("env", "Environments & placement"), ("sandboxes", "Sandboxes")]

_MARKER = ".workspace-session.json"
_MARKER_SCAN = 120
_DOCKER_TIMEOUT_S = 0.5
_SANDBOX_LABEL = "mo.sandbox=1"


def _engine_root() -> Path:
    return Path(os.environ.get("MINI_ORK_ROOT") or Path(__file__).resolve().parents[2])


def _env(home: Path) -> dict[str, str]:
    return {"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": str(_engine_root())}


def _rows(home: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    db = home / "state.db"
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _markers(home: Path) -> list[dict[str, Any]]:
    """Workspace-session markers of recent runs (``backend``, ``session_id``, ``node``…)."""
    out = []
    for r in _rows(home, "SELECT id, status FROM task_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                   (_MARKER_SCAN,)):
        path = home / "runs" / str(r["id"]) / _MARKER
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append({**data, "run_id": data.get("run_id") or r["id"], "status": r.get("status")})
    return out


def _short(run_id: str) -> str:
    parts = str(run_id).split("-")
    if len(parts) == 3 and parts[0] == "run" and parts[1].isdigit():
        return f"run-{parts[2][:6]}"
    return run_id if len(run_id) <= 28 else run_id[:27] + "…"


def _registry(home: Path) -> dict[str, dict]:
    from mini_ork.remote.nodes import load_registry

    return load_registry(env=_env(home))


# ── nodes ──────────────────────────────────────────────────────────────────

def _live_runs(home: Path) -> set[str]:
    """Runs holding an unexpired lease — the ones a dispatcher is driving now.

    ``task_runs.status`` alone is not liveness: a crashed run stays
    ``executing`` forever."""
    return {str(r["run_id"]) for r in _rows(home, "SELECT run_id FROM run_leases WHERE expires_at > ?",
                                            (int(time.time()),))}


def _nodes_table(home: Path, registry: dict[str, dict], markers: list[dict[str, Any]]) -> dict[str, Any]:
    live = _live_runs(home)
    remote_by_node: dict[str, int] = {}
    remote_live: set[str] = set()
    for m in markers:
        node = (m.get("node") or {}).get("name") if isinstance(m.get("node"), dict) else None
        if m.get("backend") == "remote" and node and str(m["run_id"]) in live:
            remote_by_node[node] = remote_by_node.get(node, 0) + 1
            remote_live.add(str(m["run_id"]))
    local_runs = len(live - remote_live)
    machine = "this Mac" if platform.system() == "Darwin" else "this machine"
    rows: list[Any] = [[S.mono(f"local · {machine}"), S.cell("online", "green"), S.muted("local"),
                        S.mono(local_runs), S.mono("—"), S.muted("—")]]
    for name, entry in sorted(registry.items()):
        entry = entry if isinstance(entry, dict) else {}
        rows.append({"cells": [S.mono(name), S.muted("not probed"), S.muted("node-agent"),
                               S.mono(remote_by_node.get(name, 0)), S.mono("—"),
                               S.muted(entry.get("image") or entry.get("url") or "—")],
                     "do": S.cli("nodes", "ping", name)})
    note = ("Click a node to ping its /v1/health. Nothing is probed when this page opens."
            if registry else "No remote nodes registered — runs execute on this machine.")
    actions = []
    if registry:
        actions.append(S.btn("Ping all", None, "default"))
    return S.table("Nodes", [S.col(fr=1, min=140), S.col(70), S.col(90), S.col(50), S.col(60), S.col(fr=1)],
                   ["node", "state", "kind", "runs", "latency", "image"], rows, full=True,
                   note=note, actions=actions)


def _doctor(registry: dict[str, dict]) -> dict[str, Any]:
    from mini_ork.cli.nodes import CHECKS

    target = sorted(registry)[0] if registry else "default environment"
    items = [S.dot(check, "not run") for check in CHECKS]
    return S.lst(f"Doctor · {target}", items,
                 note="An ordered preflight for an environment, with a fix hint per failure.",
                 actions=[S.btn("Run doctor", S.cli("nodes", "doctor", "--no-llm"), "primary")])


def _remote_runs(home: Path, markers: list[dict[str, Any]]) -> dict[str, Any]:
    remote = [m for m in markers if m.get("backend") == "remote"]
    leased = _live_runs(home)
    items = []
    for m in remote[:8]:
        node = m.get("node") if isinstance(m.get("node"), dict) else {}
        status = str(m.get("status") or "—")
        live = str(m["run_id"]) in leased
        items.append(S.item(f"{_short(str(m['run_id']))} · {node.get('name') or '?'}",
                            f"{status} · session {m.get('session_id') or '—'} · since {m.get('created_at') or '—'}",
                            m="●" if live else "•", mc="blue" if live else "sub",
                            acts=[S.btn("Open run", S.open_run(str(m["run_id"])), "ghost")]))
    title = "Remote runs" if len(remote) != 1 else f"Remote run · {_short(str(remote[0]['run_id']))}"
    return S.lst(title, items or [S.dot("No remote runs", "runs execute locally until a run picks an "
                                                          "environment bound to a node (--env)")])


# ── environments ───────────────────────────────────────────────────────────

def _profiles(home: Path) -> dict[str, Any]:
    from mini_ork.remote.environments import list_profiles, load_profile

    env = _env(home)
    rows: list[Any] = []
    for name in list_profiles(env=env):
        try:
            prof = load_profile(name, env=env)
        except Exception as exc:  # noqa: BLE001 — an invalid profile is a row, not a crash
            rows.append([S.mono(name), S.cell(str(exc).splitlines()[0][:140], "red"), S.cell("invalid", "red")])
            continue
        bits = [prof.image or "host toolchain", f"network {prof.network}"]
        if prof.resources:
            bits.append(" ".join(f"{k} {v}" for k, v in prof.resources.items()))
        if prof.secrets:
            bits.append(f"{len(prof.secrets)} secret name(s)")
        if prof.setup:
            bits.append("setup script")
        rows.append([S.mono(name), " · ".join(bits), S.muted(prof.node or "local")])
    if not rows:
        rows = [[S.muted("No profiles"), S.muted("add config/environments/<name>.yaml under the home"), ""]]
    return S.table("Environment profiles", [S.col(100), S.col(fr=1), S.col(90)],
                   ["profile", "contents", "placement"], rows, full=True,
                   note="Pick one per run with --env. The live file under the home merges per key "
                        "over the engine template.")


def _placement() -> dict[str, Any]:
    return S.lst("Placement policy", [
        S.dot("A run executes where its --env profile's node is; no node means this machine"),
        S.dot("A remote spawn carries only the dispatched lane's secrets", "never the control plane's ambient set"),
        S.dot("Profiles may name secrets, never values", "an env: key shaped like a secret is rejected"),
        S.dot("Network level per profile", "full, or an allowlist that admits the lane endpoints"),
    ])


# ── sandboxes ──────────────────────────────────────────────────────────────

def _docker_sandboxes() -> list[dict[str, str]] | None:
    """Labelled sandbox containers; ``None`` when Docker is absent or slow."""
    if shutil.which("docker") is None:
        return None
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", f"label={_SANDBOX_LABEL}",
             "--format", "{{.ID}}\t{{.Names}}\t{{.CreatedAt}}\t{{.RunningFor}}"],
            capture_output=True, text=True, timeout=_DOCKER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows.append({"id": parts[0], "name": parts[1], "created": parts[2], "age": parts[3]})
    return rows


def _sandboxes(home: Path, markers: list[dict[str, Any]], now: float) -> dict[str, Any]:
    from mini_ork.runtime.sandbox_reaper import DEFAULT_MAX_AGE_S, _parse_created_at

    containers = _docker_sandboxes()
    by_session = {str(m.get("session_id")): m for m in markers if m.get("session_id")}
    rows: list[Any] = []
    leaked = 0
    for c in containers or []:
        marker = next((m for sid, m in by_session.items()
                       if sid and (sid.startswith(c["id"]) or c["id"].startswith(sid[:12]) or sid == c["name"])), None)
        created = _parse_created_at(c["created"])
        old = created is not None and now - created > DEFAULT_MAX_AGE_S
        if marker:
            run_cell: Any = S.muted(_short(str(marker["run_id"])))
        elif old:
            run_cell = S.cell("leaked", "red")
            leaked += 1
        else:
            run_cell = S.muted("—")
        row: dict[str, Any] = {"cells": [S.mono(c["name"]), run_cell, S.muted(c["age"])]}
        if marker:
            row["do"] = S.open_run(str(marker["run_id"]))
        rows.append(row)
    if containers is None:
        note = "Docker is not available (or did not answer in time); no sandboxes listed."
        rows = [[S.muted("Docker unavailable"), "", ""]]
    elif not rows:
        note = "No sandbox containers are running."
        rows = [[S.muted("None running"), "", ""]]
    else:
        hours = DEFAULT_MAX_AGE_S // 3600
        note = (f"The reaper sweeps instances older than {hours} h; {leaked} waiting." if leaked
                else f"The reaper sweeps instances older than {hours} h; none waiting.")
    return S.table("Docker sandboxes", [S.col(fr=1), S.col(90), S.col(110)], ["sandbox", "run", "age"],
                   rows, full=True, note=note,
                   actions=[S.btn("sandbox-gc", S.cli("sandbox-gc", confirm="Remove sandboxes older than the TTL?"),
                                  "primary")])


# ── page ───────────────────────────────────────────────────────────────────

def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else "nodes"
    errors: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    try:
        registry = _registry(home)
    except Exception as exc:  # noqa: BLE001
        registry = {}
        errors["registry"] = f"{type(exc).__name__}: {exc}"
    chip = (S.chip(f"{len(registry)} remote · not probed", "sub") if registry
            else S.chip("local only", "sub"))
    live = Path(home) / "config" / "nodes.yaml"
    template = _engine_root() / "config" / "nodes.yaml.example"
    add_target = live if live.is_file() else template
    if tab == "nodes":
        markers = _markers(home)
        sections += S.guarded(errors, "Nodes", lambda: _nodes_table(home, registry, markers))
        sections += S.guarded(errors, "Doctor", lambda: _doctor(registry))
        sections += S.guarded(errors, "Remote runs", lambda: _remote_runs(home, markers))
    elif tab == "env":
        sections += S.guarded(errors, "Environment profiles", lambda: _profiles(home))
        sections += S.guarded(errors, "Placement policy", _placement)
    else:
        markers = _markers(home)
        sections += S.guarded(errors, "Docker sandboxes", lambda: _sandboxes(home, markers, time.time()))
    return S.page("nodes", "Nodes & sandboxes",
                  "Where runs execute. Remote nodes get the target tree and a run-dir mirror; secrets "
                  "marked local-only never cross the wire.",
                  chips_=[chip],
                  actions=[S.btn("Add node", S.open_path(str(add_target)) if add_target.is_file() else None)],
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)
