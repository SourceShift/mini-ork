"""Setup & health — mini-ork wired into this project and this editor.

Readiness reuses ``mini_ork.acp.setup``'s checks (they never launch a model and
never return a secret value). Zed wiring reads the settings file leniently and
shows the mini-ork entries with their env *names* only. Health runs the
in-process garden checks (well under a second) and reads versions; exports are
reported by presence of their env, never their values.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("readiness", "Readiness"), ("zed", "Zed wiring"), ("projects", "Projects"),
        ("health", "Health & exports")]

_WORKER_ROLES = ("implementer", "reviewer", "planner")
_SERVE_URL = "http://127.0.0.1:7090"


def _engine_root() -> Path:
    return Path(os.environ.get("MINI_ORK_ROOT") or Path(__file__).resolve().parents[2])


def _rows(home: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    db = home / "state.db"
    if not db.is_file():
        return []
    # `db_for` opens the same WAL db the other pages do; a ro URI without the
    # `-shm` sidecar fails and the page renders empty.
    from mini_ork.web.deps import db_for
    state = db_for(home)
    try:
        return state.rows(sql, params)
    except sqlite3.OperationalError:
        return []


def _migrations(home: Path) -> tuple[int, str]:
    count = _rows(home, "SELECT COUNT(*) AS n FROM schema_migrations")
    latest = _rows(home, "SELECT filename FROM schema_migrations WHERE filename GLOB '[0-9]*' "
                         "ORDER BY filename DESC LIMIT 1")
    n = int(count[0]["n"]) if count else 0
    name = str(latest[0]["filename"]).split("_", 1)[0] if latest else ""
    return n, name


def _zed_settings() -> tuple[str, dict[str, Any] | None, str | None]:
    from mini_ork.cli.zed_cmd import _load_settings, _settings_path

    path = _settings_path()
    if not os.path.isfile(path):
        return path, None, "not found"
    data, err = _load_settings(path, lenient=True)
    return path, data, err


def _entry(data: dict[str, Any] | None, group: str) -> dict[str, Any] | None:
    block = (data or {}).get(group)
    entry = block.get("mini-ork") if isinstance(block, dict) else None
    return entry if isinstance(entry, dict) else None


def _version() -> str:
    try:
        import tomllib

        doc = tomllib.loads((_engine_root() / "pyproject.toml").read_text(encoding="utf-8"))
        return str(doc.get("project", {}).get("version") or "—")
    except Exception:  # noqa: BLE001
        return "—"


# ── readiness ──────────────────────────────────────────────────────────────

def _lane_row(home: Path) -> dict[str, Any]:
    """Worker lanes. A role's lane may be a fallback list (``glm,minimax``): the
    role is ready when any lane in it has credentials."""
    from mini_ork.acp import setup as acp_setup

    lane_map = acp_setup._load_lane_map(home)
    ready, missing, fixes = [], [], []
    for role in _WORKER_ROLES:
        spec_ = str(lane_map.get(role) or "").strip()
        if not spec_:
            continue
        lanes = [lane.strip() for lane in spec_.split(",") if lane.strip()]
        checks = [acp_setup._check_provider_credentials(lane, role=role, home=home) for lane in lanes]
        if any(c.ok for c in checks):
            ready.append(role)
        else:
            missing.append(f"{role} ({spec_})")
            fixes += [f"mini-ork providers configure {lane}" for lane in lanes[:1]]
    if not ready and not missing:
        return S.dot("Worker lanes", "no worker lanes configured in agents.yaml")
    if missing:
        return S.bad("Worker lanes: missing credentials", "; ".join(missing) + " — fix: " + " && ".join(fixes))
    return S.ok("Worker lanes", ", ".join(ready) + " have keys")


def _readiness(home: Path) -> dict[str, Any]:
    from mini_ork.acp import setup as acp_setup

    items = []
    project = acp_setup._check_project(home)
    if project.ok:
        n, latest = _migrations(home)
        items.append(S.ok(".mini-ork/ found", f"state.db at migration {latest or '—'} · {n} applied"))
    else:
        items.append(S.bad(".mini-ork/ missing", f"{project.detail} — fix: {project.fix}"))
    orch = acp_setup._check_orchestrator(home, lane=None)
    items.append(S.ok("Orchestrator login", orch.detail) if orch.ok
                 else S.bad("Orchestrator login", orch.detail + (f" — fix: {orch.fix}" if orch.fix else "")))
    if importlib.util.find_spec("acp") is not None:
        items.append(S.ok("acp extra importable"))
    else:
        items.append(S.bad("acp extra missing", "pip install 'mini-ork[acp]'"))
    items.append(_lane_row(home))
    hook = home / "worktree-setup.sh"
    if hook.is_file():
        items.append(S.ok("worktree-setup.sh present", "runs inside every new task worktree",
                          [S.btn("Open", S.open_path(str(hook)), "ghost")]))
    else:
        items.append(S.warn(".mini-ork/worktree-setup.sh missing", "new worktrees will not copy .env or "
                                                                   "install dependencies"))
    _path, data, _err = _zed_settings()
    agent = _entry(data, "agent_servers")
    env = agent.get("env") if agent else None
    if agent and isinstance(env, dict) and env.get("PATH"):
        items.append(S.ok("PATH captured for Dock launches", "agent_servers.mini-ork carries the shell PATH"))
    elif agent:
        items.append(S.warn("PATH not captured", "Zed started from the Dock will not find claude — "
                                                 "re-run mini-ork zed setup from a terminal"))
    else:
        items.append(S.bad("Zed not wired", "agent_servers.mini-ork is missing — run mini-ork zed setup"))
    try:
        from mini_ork import automations

        sched = automations.scheduler_status(home)
    except Exception as exc:  # noqa: BLE001
        sched = {"installed": False, "error": str(exc)}
    if sched.get("installed"):
        items.append(S.ok(f"Scheduler installed · {sched.get('platform') or ''}".rstrip(" ·"),
                          f"last tick {sched.get('last_tick')}" if sched.get("last_tick") else "no tick yet"))
    else:
        items.append(S.warn("Scheduler not installed", "automations will not fire on their own",
                            [S.btn("Turn on", S.cli("automations", "scheduler", "install"), "primary")]))
    return S.lst("mini-ork acp --setup", items, full=True,
                 note="Never launches a model and never reads secret values.")


# ── zed wiring ─────────────────────────────────────────────────────────────

def _redacted(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry with env *names* only — an env block may hold keys."""
    out = dict(entry)
    if isinstance(out.get("env"), dict):
        out["env"] = {k: "…" for k in out["env"]}
    return out


def _zed(home: Path) -> list[dict[str, Any]]:
    path, data, err = _zed_settings()
    shown = path.replace(os.path.expanduser("~"), "~", 1)
    if data is None:
        return [S.lst(shown, [S.bad("Settings unreadable", err or "unknown error")], full=True)]
    agent, context = _entry(data, "agent_servers"), _entry(data, "context_servers")
    launcher = (agent or context or {}).get("command") or "—"
    executable = isinstance(launcher, str) and os.path.isabs(launcher) and os.access(launcher, os.X_OK)
    backups = sorted(Path(path).parent.glob(Path(path).name + ".bak-*"))
    items = [
        ("agent_servers.mini-ork", "present" if agent else "missing", "green" if agent else "red"),
        ("context_servers.mini-ork", "present" if context else "missing", "green" if context else "red"),
        ("Launcher", launcher, "text" if executable else "yellow",
         "executable" if executable else "not an executable absolute path"),
        ("Backup", backups[-1].name if backups else "none", "text"),
    ]
    note = ("To re-run or undo, use a terminal: mini-ork zed setup [--layout] · mini-ork zed uninstall. "
            "(From here they would pin this project's home into every Zed thread.)")
    sections = [S.kv(shown, items, full=True, note=note,
                     actions=[S.btn("Re-run setup", None), S.btn("Agentic layout", None),
                              S.btn("Uninstall", None, "danger"),
                              S.btn("Open settings", S.open_path(path), "ghost")])]
    lines: list[tuple[str, str]] = []
    for group, entry in (("agent_servers", agent), ("context_servers", context)):
        if entry:
            lines.append((f'"{group}": {{ "mini-ork": {json.dumps(_redacted(entry), ensure_ascii=False)} }}', "body"))
    sections.append(S.code("Entries written", lines or [("No mini-ork entries in settings.json", "dim")],
                           full=True))
    return sections


# ── projects ───────────────────────────────────────────────────────────────

def _registry_file() -> Path:
    env = os.environ.get("MINI_ORK_PROJECTS_FILE")
    return Path(env).expanduser() if env else Path.home() / ".config" / "mini-ork" / "projects.json"


def _projects(home: Path) -> dict[str, Any]:
    file = _registry_file()
    homes: list[Path] = []
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        homes = [Path(h).expanduser().resolve() for h in data.get("projects", []) if isinstance(h, str)]
    except (OSError, ValueError, AttributeError):
        homes = []
    current = home.resolve()
    if current not in homes:
        homes.insert(0, current)
    rows = []
    for h in homes:
        is_current = h == current
        status = (S.cell("current", "green") if is_current
                  else S.muted("ready") if (h / "state.db").is_file() else S.cell("missing", "red"))
        rows.append({"cells": [S.cell(h.parent.name, "text", b=is_current),
                               S.muted(str(h.parent).replace(os.path.expanduser("~"), "~", 1)), status],
                     "sel": is_current, "do": None if is_current else S.reveal(str(h.parent))})
    actions = [S.btn("Add project", None, "primary"),
               S.btn("Open registry", S.open_path(str(file)) if file.is_file() else None)]
    return S.table("Projects", [S.col(fr=1), S.col(fr=1.4), S.col(80)], ["project", "path", ""], rows,
                   full=True, actions=actions,
                   note=f"Known homes come from {str(file).replace(os.path.expanduser('~'), '~', 1)} "
                        "(shared with mini-ork serve).")


# ── health & exports ───────────────────────────────────────────────────────

def _garden(home: Path) -> tuple[dict[str, Any], dict[str, int]]:
    from mini_ork.cli import garden as g

    class _Capture(g.Findings):
        def __init__(self) -> None:
            super().__init__()
            self.items: list[tuple[str, str, str]] = []

        def _emit(self, tag: str, msg: str, fix: str) -> None:
            self.items.append((tag.strip(" []"), msg, fix))

    root = str(_engine_root())
    f = _Capture()
    for check, arg in ((g._check_collisions, root), (g._check_sizes, root),
                       (g._check_stale_runs, str(home)), (g._check_orphan_stashes, str(home.parent)),
                       (g._check_env_docs, root), (g._check_inert_mechanisms, str(home))):
        check(arg, f)
    items = []
    for tag, msg, fix in f.items:
        if tag in ("error", "warning") and len(items) < 8:
            maker = S.bad if tag == "error" else S.warn
            items.append(maker(msg, f"fix: {fix}"))
    groups: dict[str, int] = {}
    for tag, msg, _fix in f.items:
        if tag == "info":
            key = msg.split(":", 1)[0]
            groups[key] = groups.get(key, 0) + 1
    for key, n in sorted(groups.items(), key=lambda kv: -kv[1])[:4]:
        items.append(S.dot(key if n == 1 else f"{key} · ×{n}", "info"))
    if not items:
        items = [S.ok("No drift", "garden: clean")]
    counts = {"errors": f.errors, "warnings": f.warnings, "infos": f.infos}
    return S.lst("garden · drift", items, note=f"{f.errors} error(s), {f.warnings} warning(s), {f.infos} info"), counts


def _health(home: Path) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}
    counts: dict[str, int] = {}

    def garden() -> dict[str, Any]:
        section, c = _garden(home)
        counts.update(c)
        return section

    out = S.guarded(errors, "garden · drift", garden)
    n, latest = _migrations(home)
    drift = ("—", "sub") if not counts else (
        (f"{counts['errors']} error(s)", "red") if counts["errors"]
        else (f"{counts['warnings']} warning(s)", "yellow") if counts["warnings"] else ("none", "green"))
    out.append(S.kv("Versions", [("mini-ork", _version()),
                                 ("Migrations", f"{n} applied", "text", f"latest {latest}" if latest else None),
                                 ("Config drift", drift[0], drift[1])],
                    actions=[S.btn("Update", None)]))
    exports = [S.dot("Web UI", f"mini-ork serve · {_SERVE_URL}", [S.btn("Open", S.url(_SERVE_URL))])]
    if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
        endpoint = os.environ.get("LANGFUSE_OTLP_ENDPOINT") or "Langfuse default endpoint"
        exports.append(S.ok("OTel / Langfuse", f"exporting to {endpoint}"))
    else:
        exports.append(S.dot("OTel / Langfuse", "not configured · set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY"))
    if os.environ.get("MINI_ORK_ON_EVENT"):
        exports.append(S.ok("Event hook", "MINI_ORK_ON_EVENT is set — node lifecycle events go to its command"))
    else:
        exports.append(S.dot("Event hook", "not configured · MINI_ORK_ON_EVENT"))
    out.append(S.lst("Exports", exports, full=True))
    return out


# ── page ───────────────────────────────────────────────────────────────────

def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else "readiness"
    errors: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    if tab == "readiness":
        sections += S.guarded(errors, "mini-ork acp --setup", lambda: _readiness(home))
    elif tab == "zed":
        sections += S.guarded(errors, "Zed wiring", lambda: _zed(home))
    elif tab == "projects":
        sections += S.guarded(errors, "Projects", lambda: _projects(home))
    else:
        sections += S.guarded(errors, "Health & exports", lambda: _health(home))
    return S.page("setup", "Setup & health",
                  "Getting mini-ork wired into this project and this editor, and keeping it healthy.",
                  actions=[S.btn("Run all checks", S.set_args(checked=str(int(time.time()))))],
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)
