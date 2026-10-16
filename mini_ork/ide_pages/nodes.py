"""Nodes & sandboxes — where runs execute.

Reads the node registry (``config/nodes.yaml``, template + live), the
environment profiles and the node providers they name (Daytona, E2B, Modal, …),
the per-run workspace-session markers and node-provider records, the run
events that recorded where each node ran, and the labelled Docker sandboxes.
Never probes a node or a cloud API while building: ping, doctor and
sandbox-gc are buttons.
"""
from __future__ import annotations

import importlib.util
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
from mini_ork.runtime.workspace_session import read_session_marker

TABS = [("nodes", "Nodes"), ("env", "Environments & placement"), ("sandboxes", "Sandboxes")]

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
    """Workspace-session markers of recent runs (``backend``, ``session_id``, ``node``…).

    One row per run, from the run-level marker or — for a run whose nodes each
    got their own sandbox (``MO_NODE_ISOLATION``) — the first per-node one, so a
    per-node run still shows a node here rather than dropping out of the table.
    """
    out = []
    for r in _rows(home, "SELECT id, status FROM task_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                   (_MARKER_SCAN,)):
        data = read_session_marker(home / "runs" / str(r["id"]))
        if data:
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


# ── providers ──────────────────────────────────────────────────────────────

def _provider_meta(name: str) -> dict[str, Any]:
    """What the page can say about a node provider without touching the network:
    its kind, whether its SDK is importable, and the credential NAMES it needs."""
    from mini_ork.remote.providers import get_node_provider

    try:
        prov = get_node_provider(name)
    except Exception as exc:  # noqa: BLE001 — an unknown provider is a row, not a crash
        return {"name": name, "kind": "unknown provider", "error": str(exc).splitlines()[0],
                "sdk_module": "", "sdk_ok": False, "credential_keys": ()}
    sdk = str(getattr(prov, "sdk_module", "") or "")
    return {"name": name, "kind": str(getattr(prov, "kind", "") or ""), "error": "",
            "sdk_module": sdk,
            "sdk_ok": (importlib.util.find_spec(sdk) is not None) if sdk else True,
            "credential_keys": tuple(getattr(prov, "credential_keys", ()) or ())}


def _credential_sources(home: Path, keys: tuple[str, ...]) -> dict[str, str]:
    """``{key: where}`` for each credential NAME: ``env``, ``secrets.local.sh``,
    ``vault.yaml`` (a configured backend, not resolved here) or ``""`` (missing).

    Reads the SELECTED home's store, not the server process's: the UI may be
    served from another checkout whose ``MINI_ORK_HOME`` is a different project.
    Only names are inspected; no value is returned or displayed."""
    from mini_ork.dispatch.secrets import read_secret_exports

    stored: set[str] = set()
    store = Path(home) / "config" / "secrets.local.sh"
    if store.is_file():
        try:
            stored = set(read_secret_exports(store))
        except Exception:  # noqa: BLE001 — an unreadable store reads as empty
            stored = set()
    vault = (Path(home) / "config" / "vault.yaml").is_file()
    out: dict[str, str] = {}
    for key in keys:
        if (os.environ.get(key) or "").strip():
            out[key] = "env"
        elif key in stored:
            out[key] = "secrets.local.sh"
        elif vault:
            out[key] = "vault.yaml"
        else:
            out[key] = ""
    return out


def _readiness(home: Path, meta: dict[str, Any]) -> tuple[str, str]:
    """``(text, tone)`` for a provider: SDK first, then credentials."""
    if meta.get("error"):
        return meta["error"][:120], "red"
    if not meta["sdk_ok"]:
        return f"SDK missing — pip install 'mini-ork[{meta['name']}]'", "red"
    missing = [k for k, where in _credential_sources(home, meta["credential_keys"]).items()
               if not where]
    if missing:
        return f"needs {', '.join(missing)}", "red"
    return "ready", "green"


def _profile_runners(home: Path) -> list[tuple[str, Any, str]]:
    """``(profile, loaded profile or None, provider name)`` for every profile."""
    from mini_ork.remote.environments import list_profiles, load_profile
    from mini_ork.remote.providers import provider_name_for

    env = _env(home)
    out: list[tuple[str, Any, str]] = []
    for name in list_profiles(env=env):
        try:
            prof = load_profile(name, env=env)
        except Exception:  # noqa: BLE001
            out.append((name, None, ""))
            continue
        out.append((name, prof, provider_name_for(prof, {})))
    return out


def _cloud_providers(home: Path) -> dict[str, Any]:
    from mini_ork.remote.providers import known_node_providers

    used: dict[str, list[str]] = {}
    for name, _prof, provider in _profile_runners(home):
        if provider:
            used.setdefault(provider, []).append(name)
    live = _node_records(home)
    rows: list[Any] = []
    for name in known_node_providers():
        if name == "static":
            continue
        meta = _provider_meta(name)
        text, tone = _readiness(home, meta)
        creds = _credential_sources(home, meta["credential_keys"])
        cred_cell = (", ".join(f"{k} ({w})" if w else f"{k} (missing)" for k, w in creds.items())
                     or "—")
        n_live = sum(1 for r in live if r["provider"] == name and r["live"])
        rows.append({"cells": [S.mono(name), S.muted(meta["kind"] or "—"), S.cell(text, tone),
                               S.muted(cred_cell), S.mono(", ".join(used.get(name, [])) or "—"),
                               S.mono(n_live)]})
    return S.table("Cloud providers", [S.col(80), S.col(fr=1, min=140), S.col(fr=1, min=120),
                                       S.col(fr=1, min=140), S.col(110), S.col(50)],
                   ["provider", "kind", "ready", "credentials", "profiles", "live"], rows, full=True,
                   note="A profile with provider: <name> runs its nodes in a fresh sandbox of that "
                        "cloud, one per run. Credentials are names only; nothing here calls the "
                        "provider. Run the doctor on a profile to prove it end to end.")


def _node_records(home: Path) -> list[dict[str, Any]]:
    """Nodes a provider created and has not released yet (``.node-provider.json``)."""
    live = _live_runs(home)
    out = []
    for r in _rows(home, "SELECT id, status FROM task_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                   (_MARKER_SCAN,)):
        path = home / "runs" / str(r["id"]) / ".node-provider.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("provider"):
            out.append({"run_id": str(r["id"]), "status": r.get("status"),
                        "provider": str(data["provider"]), "handle": str(data.get("handle") or ""),
                        "created_at": str(data.get("created_at") or ""),
                        "live": str(r["id"]) in live})
    return out


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
                     "do": S.cli("nodes", "ping", name, home=False)})
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
                 actions=[S.btn("Run doctor", S.cli("nodes", "doctor", "--no-llm", home=False), "primary")])


def _remote_run_hosts(home: Path, limit: int = 8) -> list[dict[str, Any]]:
    """Recent runs whose nodes executed remotely, newest first, from the
    ``node_start`` events (``placement: remote``, ``node_host``). Durable: the
    session marker a remote run writes is deleted when the run ends, so a
    finished cloud run would otherwise vanish from this page."""
    rows = _rows(home, "SELECT run_id, payload_json, created_at FROM run_events "
                       "WHERE event_type = 'node_start' AND payload_json LIKE '%remote%' "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 2000")
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("placement") != "remote":
            continue
        run_id = str(row["run_id"])
        if run_id in seen:
            continue
        host = str(payload.get("node_host") or "")
        seen[run_id] = {"run_id": run_id, "host": host,
                        "provider": host.split(":", 1)[0] if ":" in host else "node",
                        "at": row.get("created_at")}
        if len(seen) >= limit:
            break
    if seen:
        marks = ",".join("?" * len(seen))
        for r in _rows(home, f"SELECT id, status FROM task_runs WHERE id IN ({marks})", tuple(seen)):
            seen[str(r["id"])]["status"] = r.get("status")
    return list(seen.values())


def _remote_runs(home: Path, markers: list[dict[str, Any]]) -> dict[str, Any]:
    leased = _live_runs(home)
    sessions = {str(m["run_id"]): m for m in markers if m.get("backend") == "remote"}
    runs = _remote_run_hosts(home)
    # A live session may not have logged a node_start yet: its marker counts too.
    known = {r["run_id"] for r in runs}
    for run_id, m in sessions.items():
        if run_id in known:
            continue
        node = m.get("node") if isinstance(m.get("node"), dict) else {}
        host = str(node.get("name") or "")
        runs.insert(0, {"run_id": run_id, "host": host, "status": m.get("status"),
                        "provider": str(m.get("provider") or (host.split(":", 1)[0] if ":" in host
                                                                   else host or "node"))})
    items = []
    for r in runs:
        live = r["run_id"] in leased
        marker = sessions.get(r["run_id"]) or {}
        where = r["host"] or "?"
        detail = [str(r.get("status") or "—"), f"on {where}"]
        if marker.get("session_id"):
            detail.append(f"session {marker['session_id']}")
        items.append(S.item(f"{_short(r['run_id'])} · {r['provider']}", " · ".join(detail),
                            m="●" if live else "•", mc="blue" if live else "sub",
                            acts=[S.btn("Open run", S.open_run(r["run_id"]), "ghost")]))
    title = "Remote runs" if len(runs) != 1 else f"Remote run · {_short(runs[0]['run_id'])}"
    return S.lst(title, items or [S.dot("No remote runs", "runs execute locally until a run picks "
                                                          "--placement remote --env <profile>")])


# ── environments ───────────────────────────────────────────────────────────

def _profiles(home: Path) -> dict[str, Any]:
    from mini_ork.remote.environments import load_profile

    env = _env(home)
    rows: list[Any] = []
    for name, prof, provider in _profile_runners(home):
        if prof is None:
            try:
                load_profile(name, env=env)
                why = "invalid"
            except Exception as exc:  # noqa: BLE001 — an invalid profile is a row, not a crash
                why = str(exc).splitlines()[0][:140]
            rows.append([S.mono(name), S.muted("—"), S.cell(why, "red"), S.cell("invalid", "red")])
            continue
        cloud = bool(provider) and provider != "static"
        options = dict(getattr(prof, "provider_options", None) or {})
        bits = [prof.image or (f"{provider} default image" if cloud else "host toolchain")]
        bits += [f"{k} {v}" for k, v in options.items() if v not in (None, "", {}, [])]
        bits.append(f"network {prof.network}")
        if prof.resources:
            bits.append(" ".join(f"{k} {v}" for k, v in prof.resources.items()))
        if prof.secrets:
            bits.append(f"{len(prof.secrets)} secret name(s)")
        if prof.setup:
            bits.append("setup script")
        if cloud:
            meta = _provider_meta(provider)
            runner: Any = S.cell(f"{provider} · {meta['kind'] or 'sandbox per run'}", "blue")
            text, tone = _readiness(home, meta)
            ready: Any = S.cell(text, tone)
        else:
            runner = S.muted(prof.node or "local")
            ready = S.muted("—")
        rows.append({"cells": [S.mono(name), runner, " · ".join(bits), ready],
                     "do": S.cli("nodes", "doctor", "--env", name, "--no-llm", home=False)})
    if not rows:
        rows = [[S.muted("No profiles"), "", S.muted("add config/environments/<name>.yaml under the home"), ""]]
    return S.table("Environment profiles", [S.col(90), S.col(fr=1, min=150), S.col(fr=2), S.col(fr=1, min=110)],
                   ["profile", "runner", "contents", "ready"], rows, full=True,
                   note="Pick one per run: mini-ork run <recipe> <kickoff> --placement remote --env <profile>. "
                        "Click a profile to run its doctor. The live file under the home merges per key "
                        "over the engine template.")


def _placement() -> dict[str, Any]:
    return S.lst("Placement policy", [
        S.dot("A run executes where its --env profile points: a provider's sandbox, a node, or this machine"),
        S.dot("A cloud provider creates one sandbox per run and deletes it when the run ends",
              "a wall-clock TTL and sandbox-gc catch any it misses"),
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
                   actions=[S.btn("sandbox-gc", S.cli("sandbox-gc", confirm="Remove sandboxes older than the TTL?", home=False),
                                  "primary")])


def _cloud_sandboxes(home: Path) -> dict[str, Any]:
    records = _node_records(home)
    rows: list[Any] = []
    orphans = 0
    for r in records:
        if r["live"]:
            state: Any = S.cell("running", "blue")
        else:
            state = S.cell("not released", "red")
            orphans += 1
        rows.append({"cells": [S.mono(r["provider"]), S.mono(r["handle"][:20] or "—"),
                               S.muted(_short(r["run_id"])), state, S.muted(r["created_at"] or "—")],
                     "do": S.open_run(r["run_id"])})
    if not rows:
        rows = [[S.muted("None"), "", "", "", ""]]
        note = ("No cloud sandbox is held by a run. A provider deletes each run's sandbox when the "
                "run ends.")
    else:
        note = (f"{orphans} not released by a finished run: sandbox-gc reaps them, and the "
                "provider's TTL deletes them anyway." if orphans else
                "Held by running runs; each is deleted when its run ends.")
    return S.table("Cloud sandboxes", [S.col(80), S.col(fr=1, min=120), S.col(100), S.col(100), S.col(150)],
                   ["provider", "sandbox", "run", "state", "created"], rows, full=True, note=note,
                   actions=[S.btn("sandbox-gc (all providers)",
                                  S.cli("sandbox-gc", confirm="Delete sandboxes older than the TTL, local and cloud?",
                                        home=False), "primary")])


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
        sections += S.guarded(errors, "Cloud providers", lambda: _cloud_providers(home))
    elif tab == "env":
        sections += S.guarded(errors, "Environment profiles", lambda: _profiles(home))
        sections += S.guarded(errors, "Placement policy", _placement)
    else:
        markers = _markers(home)
        sections += S.guarded(errors, "Docker sandboxes", lambda: _sandboxes(home, markers, time.time()))
        sections += S.guarded(errors, "Cloud sandboxes", lambda: _cloud_sandboxes(home))
    return S.page("nodes", "Nodes & sandboxes",
                  "Where runs execute: this machine, a node you run, or a cloud sandbox per run "
                  "(Daytona, E2B, Modal). Remote nodes get the target tree and a run-dir mirror; only "
                  "the dispatched lane's secrets cross the wire.",
                  chips_=[chip],
                  actions=[S.btn("Add node", S.open_path(str(add_target)) if add_target.is_file() else None)],
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)
