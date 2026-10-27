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

TABS = [("readiness", "Readiness"), ("features", "Features"), ("settings", "Settings"),
        ("zed", "Zed wiring"), ("projects", "Projects"), ("health", "Health & exports")]

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
    from mini_ork.web.db import db_for
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
    """The engine's version.

    ``pyproject.toml`` carries no literal ``[project] version`` any more: it is
    dynamic (``version = {attr = "mini_ork.version.__version__"}``), so reading
    the TOML alone returned "—" on every checkout. ``mini_ork.version`` is the
    single source of truth; the installed distribution metadata is the fallback
    for an environment that has the package but no source tree.
    """
    try:
        from mini_ork.version import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        pass
    try:
        from importlib.metadata import version as dist_version

        return dist_version("mini-ork")
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
        # The project this window is showing keeps its row but not its Remove:
        # switching back would put it in the list again.
        acts = [] if is_current else [
            S.btn("Remove", S.project_forget(
                str(h.parent),
                confirm=f"Remove {h.parent.name} from the project list? The folder and its runs stay."),
                "ghost")]
        rows.append({"cells": [S.cell(h.parent.name, "text", b=is_current),
                               S.muted(str(h.parent).replace(os.path.expanduser("~"), "~", 1)), status],
                     "sel": is_current, "do": None if is_current else S.project(str(h.parent)),
                     "acts": acts})
    actions = [S.btn("Add project", S.project_pick(), "primary"),
               S.btn("Open registry", S.open_path(str(file)) if file.is_file() else None)]
    return S.table("Projects", [S.col(fr=1), S.col(fr=1.4), S.col(80)], ["project", "path", ""], rows,
                   full=True, actions=actions,
                   note=f"Click a project to switch this window to it; Remove forgets the entry and "
                        f"leaves the folder alone. Known homes come from "
                        f"{str(file).replace(os.path.expanduser('~'), '~', 1)} (shared with mini-ork serve).")


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


def _otel_status() -> dict[str, Any]:
    """The OTel / Langfuse health row, resolved through the vault chain.

    ``_credentials()`` returns ``None`` for both "off" and "half-configured",
    so a second lookup distinguishes the two. Any failure reads as not
    configured. Values are never rendered — only presence and the endpoint.
    """
    try:
        from mini_ork import otel_export

        creds = otel_export._credentials()
        if creds is not None:
            _pk, _sk, endpoint = creds
            return S.ok("OTel / Langfuse", f"each finished run is exported to {endpoint}")
        env = otel_export.vault_environment()
        half = bool(env.resolve("LANGFUSE_PUBLIC_KEY")) ^ bool(
            env.resolve("LANGFUSE_SECRET_KEY")
        )
    except Exception:  # noqa: BLE001 — a broken vault means "not configured" here
        half = False
    if half:
        return S.warn(
            "OTel / Langfuse",
            "only one of LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY is set"
            " — nothing is exported",
        )
    return S.dot(
        "OTel / Langfuse",
        "not configured · set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY",
    )


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
    exports.append(_otel_status())
    if os.environ.get("MINI_ORK_ON_EVENT"):
        exports.append(S.ok("Event hook", "MINI_ORK_ON_EVENT is set — every run/node event runs it (heartbeats only with MINI_ORK_ON_EVENT_HEARTBEATS=1)"))
    else:
        exports.append(S.dot("Event hook", "not configured · MINI_ORK_ON_EVENT"))
    out.append(S.lst("Exports", exports, full=True))
    return out


# ── configuration (features & settings) ────────────────────────────────────

def _secret_names() -> dict[str, str]:
    """Names from the credential store.

    Read for *presence* and source attribution only — a value from here is never
    rendered. A store with bad permissions raises by design, so this degrades to
    empty rather than blanking the page.
    """
    try:
        from mini_ork.dispatch.secrets import read_secret_exports
        return read_secret_exports()
    except Exception:  # noqa: BLE001 — an unreadable store is not a page error
        return {}


def _external_env(home: Path, data: dict[str, Any], secrets: dict[str, str]) -> dict[str, str]:
    """``os.environ`` minus what the two stores contributed.

    This process already loaded features.yaml and the secret store into the
    environment (``main._load_feature_defaults``). To say where a value *came
    from* we must subtract the stores' own contribution, or every knob would be
    misattributed to a real environment variable.
    """
    from mini_ork.features import store
    contributed = set(store.effective_env(store.store_path(home)).keys()) | set(secrets)
    return {name: value for name, value in os.environ.items() if name not in contributed}


def _raw_state(name: str, *, data: dict[str, Any], env_only: dict[str, str],
               secrets: dict[str, str]) -> tuple[str, str]:
    """``(value, source)`` for a raw env knob: env > features.yaml env: > secrets > default."""
    if env_only.get(name):
        return env_only[name], "env"
    if data.get("env", {}).get(name) is not None:
        return data["env"][name], "project"
    if secrets.get(name):
        return secrets[name], "secrets"
    return "", "default"


#: Where an effective value came from, in the words the page's reader uses —
#: both tabs name a source identically so a value's provenance reads the same
#: wherever it is shown.
_SOURCES = {"env": "environment", "project": "features.yaml", "secrets": "secrets",
            "default": "engine default"}

#: The blast radius of accepting premium, worded once so both the toggle's
#: confirm dialog and the composer's note say the same thing.
_OPT_IN = ("Opts this project into premium features for every future run — including wizard "
           "launches that name no premium feature.")


def _premium_confirm(feat: Any, value: str) -> str:
    return f"{feat.label} multiplies run cost by ×{feat.multiplier(value):g}. {_OPT_IN}"


def _feature_controls(feats: list[Any], states: dict[str, dict[str, Any]], *, title: str,
                      premium: bool) -> list[dict[str, Any]]:
    """Toggle chips for bool features, a composer for the ones carrying a value."""
    from mini_ork.features.registry import KIND_BOOL

    chips, composers = [], []
    for feat in feats:
        cur = states[feat.id]
        if feat.kind == KIND_BOOL:
            on = not cur["off"]
            confirm = _premium_confirm(feat, "1") if premium and not on else None
            chips.append({"t": feat.label, "on": on,
                          "do": S.cli("features", "set", feat.id, "off" if on else "on",
                                      home=False, confirm=confirm)})
            continue
        # A value carrying feature: the box's text is the value, so a premium one
        # needs the opt-in in its argv — the action path can pass no flag the
        # user cannot see, and a bare `set` would refuse it (exit 2) instead.
        args = ["features", "set", feat.id] + (["--accept-premium"] if premium else [])
        note = f"{feat.description} Type `off` to disable it."
        if premium:
            note = f"{note} {_OPT_IN}"
        # When off, the box suggests the wizard's value: the point of an empty
        # box is to show what one could type, not to restate that it is off.
        composers.append(S.composer(
            str(cur["value"] if not cur["off"] else feat.default),
            args, home=False, title=feat.label, note=note))
    out: list[dict[str, Any]] = []
    if chips:
        out.append(S.chips(title, chips, full=True))
    out.extend(composers)
    return out


def _features_tab(home: Path) -> list[dict[str, Any]]:
    from mini_ork.features import store
    from mini_ork.features.registry import CONTROLLABLE_FEATURES

    path = store.store_path(home)
    data = store.load(path)
    secrets = _secret_names()
    env_only = _external_env(home, data, secrets)
    feats = sorted(CONTROLLABLE_FEATURES.values(), key=lambda f: f.id)
    states = {f.id: store.current(f, environ=env_only, data=data, secrets=secrets) for f in feats}

    rows = []
    for feat in feats:
        cur = states[feat.id]
        premium = feat.tier == "premium"
        rows.append([
            S.cell(feat.label, "text", b=True),
            S.mono(feat.id),
            S.cell("premium" if premium else "baseline", "yellow" if premium else "green"),
            S.cell(f"×{cur['multiplier']:g}", "sub", mono=True),
            S.cell("off" if cur["off"] else cur["value"], "muted" if cur["off"] else "text"),
            S.cell(_SOURCES.get(cur["source"], cur["source"]), "sub"),
            S.cell(feat.default or "—", "dim", mono=True),
        ])
    table = S.table("Features",
                    [S.col(fr=1.3), S.col(fr=1.1), S.col(70), S.col(60), S.col(fr=0.8), S.col(85),
                     S.col(fr=0.7)],
                    ["feature", "id", "tier", "cost", "now", "from", "wizard"], rows, full=True,
                    note="`now` is the value in force right now and `from` says where it came "
                         "from. `cost` is this feature's own multiplier at that value — nothing "
                         "else. `wizard` is what the wizard *suggests*, which is not always what "
                         "the engine does when nothing is set: leave the code-arm scorer unset "
                         "and it is off, whatever the suggestion says.")

    baseline = [f for f in feats if f.tier != "premium"]
    premium_feats = [f for f in feats if f.tier == "premium"]
    sections: list[dict[str, Any]] = [table, S.kv("How this works", [
        ("File", str(path).replace(os.path.expanduser("~"), "~", 1), "text",
         "written by the controls below; loaded at startup below the real environment"),
        ("Precedence", "environment > features.yaml > secrets.local.sh > engine default", "text"),
        ("Scope", "these persist as this project's default for every future run", "text"),
    ], full=True)]
    sections += _feature_controls(baseline, states, title="Baseline — on / off", premium=False)
    if premium_feats:
        sections.append(S.callout(
            "Premium features",
            "Turning one of these on multiplies run cost beyond ×1.5 and records a project-wide "
            "opt-in: a **real** environment has to prove it before the feature runs and every "
            "future run inherits it, including wizard launches that name no premium feature.",
            tone="orange"))
        sections += _feature_controls(premium_feats, states, title="Premium — on / off", premium=True)
    return sections


def _settings_tab(home: Path) -> list[dict[str, Any]]:
    from mini_ork import settings_catalogue as settings
    from mini_ork.features import store

    data = store.load(store.store_path(home))
    secrets = _secret_names()
    env_only = _external_env(home, data, secrets)
    sections: list[dict[str, Any]] = []

    for group in settings.GROUPS:
        rows = []
        for setting in settings.settings_for(group):
            value, source = _raw_state(setting.name, data=data, env_only=env_only, secrets=secrets)
            shown = setting.default if source == "default" else value
            acts = ([S.btn("Clear", S.cli("features", "unset-env", setting.name, home=False), "ghost")]
                    if source == "project" else [])
            effect = setting.effect + (f" · {setting.note}" if setting.note else "")
            rows.append({"cells": [
                S.cell(setting.label, "text", b=True),
                S.mono(setting.name),
                S.cell(shown, "muted" if source == "default" else "text"),
                S.cell(_SOURCES.get(source, source), "sub"),
                S.cell(setting.default, "sub", mono=True),
                S.cell(effect, "sub"),
            ], "acts": acts})
        sections.append(S.table(group,
                                [S.col(fr=1.3), S.col(fr=1.7), S.col(80), S.col(60), S.col(60), S.col(fr=2)],
                                ["setting", "variable", "value", "from", "default", "effect"], rows,
                                full=True,
                                note="'default' is what the engine uses when the variable is unset, "
                                     "shown for reference — this page never writes it."))
        sections.append(S.composer(f"{group}: <NAME> <value>", ["features", "set-env"], home=False,
                                   title=f"Set a {group} value",
                                   note="Type a variable and its value, e.g. "
                                        "MO_DAILY_BUDGET_USD 20. Clear a project value with the "
                                        "row's button."))

    enabled = store.enabled_premium(data)
    accept, accept_src = _raw_state(store.ACCEPT_PREMIUM_ENV, data=data, env_only=env_only,
                                    secrets=secrets)
    drop = [S.btn("Drop the opt-in", S.cli("features", "unset-env", store.ACCEPT_PREMIUM_ENV,
                                           home=False), "warn")]
    if enabled:
        sections.append(S.callout(
            "Premium opt-in is on",
            f"MO_ACCEPT_PREMIUM is {accept_src}, so premium features are accepted project-wide. "
            f"Enabled: {', '.join(enabled)}. Every future run inherits this, including wizard "
            "launches that name no premium feature. Turning a feature off does **not** drop the "
            "opt-in — use the button below.", tone="orange", actions=drop))
    else:
        sections.append(S.lst("Premium opt-in", [
            S.warn("Accepted project-wide",
                   f"MO_ACCEPT_PREMIUM is {accept_src}, but no premium feature is on. "
                   "It still pre-accepts premium for every future wizard launch.",
                   acts=drop) if accept else
            S.dot("Not accepted",
                  "no premium feature is on, and MO_ACCEPT_PREMIUM is unset")], full=True,
            note="Set from a premium feature's toggle, which is where the cost is disclosed."))

    files = [("Providers", home / "config" / "providers.yaml"),
             ("Pricing", home / "config" / "pricing.yaml"),
             ("Cost advisor", home / "config" / "cost-advisor.yaml"),
             ("Scope patterns", home / "config" / "scope-patterns.yaml"),
             ("Agents", home / "config" / "agents.yaml")]
    items = []
    for label, path in files:
        shown = str(path).replace(os.path.expanduser("~"), "~", 1)
        acts = [S.btn("Open", S.open_path(str(path)), "ghost")] if path.is_file() else []
        items.append(S.dot(label, f"{shown} · {'present' if path.is_file() else 'not created yet'}", acts))
    items.append(S.dot("Credentials", "lane keys with `mini-ork providers configure <lane>` in a "
                                      "terminal; a PM source through the Board's Sources tab — "
                                      f"both refuse to take a value here. Backend in force: "
                                      f"{_vault_backend_label()}",
                       [S.btn("Lanes & providers", S.page_link("lanes"), "ghost"),
                        S.btn("Sources", S.page_link("kanban", "sources"), "ghost")]))
    sections.append(S.lst("Edited by hand", items, full=True,
                          note="These files are edited in your editor. mini-ork self-improve rewrites "
                               "agents.yaml; a credential is entered at a terminal prompt or through "
                               "the Board's Sources tab, and never through this page."))
    return sections


def _vault_backend_label() -> str:
    """The vault's backend chain, named for the Credentials row.

    Names and readiness only: ``mini_ork.dispatch.vault`` never reports a value
    — that is its whole contract — so this text is safe in a page payload. A
    vault config that cannot be read is named rather than hidden, because the
    row exists to tell a person where a credential would go.
    """
    try:
        from mini_ork.dispatch.vault import vault_environment

        statuses = vault_environment().status()
    except Exception as exc:  # noqa: BLE001 — a broken vault must not blank the page
        return f"unreadable ({type(exc).__name__}) — see config/vault.example.yaml"
    if not statuses:
        return "none configured"
    return ", ".join(
        item.backend if item.ready else f"{item.backend} (not ready)"
        for item in statuses
    )


# ── page ───────────────────────────────────────────────────────────────────

def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else "readiness"
    errors: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    if tab == "readiness":
        sections += S.guarded(errors, "mini-ork acp --setup", lambda: _readiness(home))
    elif tab == "features":
        sections += S.guarded(errors, "Features", lambda: _features_tab(home))
    elif tab == "settings":
        sections += S.guarded(errors, "Settings", lambda: _settings_tab(home))
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
