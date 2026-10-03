"""First-run readiness + interactive setup for the ACP agent.

Called from ``mini-ork acp --setup`` and from ``MiniOrkAcpAgent.authenticate``.
Never launches an LLM; never reads or returns secret values (the only field
read out of ``claude auth status --json`` is the boolean ``loggedIn``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable

# Module-level seams (kickoff §setup.py). Tests monkeypatch these by attribute
# name so the function bodies never reach the real ``claude`` binary.
_run = subprocess.run

# Worker roles whose lanes we check during the ``lanes`` step.
_LANE_ROLES: tuple[str, ...] = ("implementer", "reviewer", "planner")

# Subscription lanes check ``claude auth status --json``; everyone else uses
# the dispatch provider credentials.
_SUBSCRIPTION_LANES: frozenset[str] = frozenset({"opus", "sonnet"})

# Cap on the ``claude auth status --json`` subprocess (seconds).
_CLAUDE_AUTH_TIMEOUT_S = 15


@dataclass
class Check:
    """One row of the readiness report.

    ``detail`` is one plain line for the user. ``fix`` is the command or step
    that resolves the failure; an empty string means "cannot auto-fix".
    """

    name: str  # "project", "orchestrator", "lanes"
    ok: bool
    detail: str
    fix: str = ""


def readiness(cwd: Path, *, lane: str | None = None) -> list[Check]:
    """Return the three readiness checks without prompting.

    Each check is independent and never raises — a misconfigured
    ``MINI_ORK_HOME`` or a missing ``claude`` binary is reported as a
    ``Check(ok=False)``, not propagated. The orchestrator's subscription
    check returns ``loggedIn`` only; the email / org fields of the same
    output are deliberately discarded.
    """

    project_home = cwd / ".mini-ork"
    home = _resolve_home_for(cwd)
    return [
        _check_project(project_home),
        _check_orchestrator(home, lane=lane),
        _check_lanes(home),
    ]


def run_interactive(
    cwd: Path,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Print each check, offer to fix failures, return 0 on full pass.

    Defaults: ``stdin=sys.stdin``, ``stdout=sys.stderr``. ``stderr`` is the
    safe default because ``mini-ork acp --setup`` may be piped, and the
    helper output must not corrupt the pipe (the run-mode ACP wire is the
    actual stdout).
    """

    in_f = stdin if stdin is not None else sys.stdin
    out_f = stdout if stdout is not None else sys.stderr

    out_f.write("mini-ork first-run setup\n")
    out_f.write("Checking project, orchestrator login, and worker-lane keys.\n\n")

    # Loop until every check passes or the user declines to fix something.
    while True:
        checks = readiness(cwd)
        _print_checks(checks, out_f)
        if all(c.ok for c in checks):
            return 0
        failing = [c for c in checks if not c.ok and c.fix]
        if not failing:
            # What is left cannot be fixed from here (e.g. install Claude Code).
            out_f.write("\nSetup incomplete — the steps above need you. Re-run: mini-ork acp --setup\n")
            return 1
        if not _ask_fix(in_f, out_f):
            out_f.write("\nSetup incomplete. Re-run later: mini-ork acp --setup\n")
            return 1
        # Re-check after each batch of fixes; bail out if the user said no
        # to any remaining failing fix.
        still_failing: list[Check] = []
        for c in failing:
            if not _run_fix(c, cwd, out_f):
                still_failing.append(c)
        if still_failing:
            out_f.write("\nStill failing:\n")
            _print_checks(still_failing, out_f)
            out_f.write("\nSetup incomplete. Re-run later: mini-ork acp --setup\n")
            return 1


# ── individual checks ────────────────────────────────────────────────────────


def _check_project(home: Path) -> Check:
    if home.is_dir():
        return Check(
            name="project",
            ok=True,
            detail=f"project ok ({home})",
        )
    return Check(
        name="project",
        ok=False,
        detail=f".mini-ork not found at {home}",
        fix="mini-ork init",
    )


def _check_orchestrator(home: Path, *, lane: str | None) -> Check:
    """Subscription lanes check ``claude auth status``; others check keys."""
    chosen = lane
    if chosen is None:
        chosen = _default_lane(home)
    if chosen in _SUBSCRIPTION_LANES:
        return _check_subscription_claude()
    return _check_provider_credentials(chosen, role="orchestrator", home=home)


def _check_lanes(home: Path) -> Check:
    """Aggregate the worker-role lanes (implementer / reviewer / planner)."""
    lane_map = _load_lane_map(home)
    rows: list[Check] = []
    missing: list[str] = []
    for role in _LANE_ROLES:
        model = lane_map.get(role)
        if not model:
            # Role not configured for this home; skip silently.
            continue
        c = _check_provider_credentials(model, role=role, home=home)
        rows.append(c)
        if not c.ok:
            missing.append(role)
    if not rows:
        return Check(
            name="lanes",
            ok=True,
            detail="no worker lanes configured",
        )
    if not missing:
        detail = "lanes ok (" + ", ".join(r.name.split(":", 1)[-1] for r in rows) + ")"
        return Check(name="lanes", ok=True, detail=detail)
    detail = f"missing credentials for lanes: {', '.join(missing)}"
    fix_lines = [f"mini-ork providers configure {lane_map[m]}" for m in missing]
    return Check(name="lanes", ok=False, detail=detail, fix=" && ".join(fix_lines))


def _check_subscription_claude() -> Check:
    """Run ``claude auth status --json`` and check ``loggedIn`` only.

    The ``loggedIn`` field is the only thing we return. Email, org, and any
    other field of the same output are deliberately discarded — readiness
    is not allowed to leak auth-account metadata.
    """
    if shutil.which("claude") is None:
        # Nothing to run yet: installing Claude Code is the user's step.
        return Check(
            name="orchestrator",
            ok=False,
            detail="claude CLI not found — install Claude Code, then run `claude auth login`",
        )
    try:
        proc = _run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_AUTH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            name="orchestrator",
            ok=False,
            detail=f"claude auth status failed: {exc}",
            fix="claude auth login",
        )
    raw = (proc.stdout or "").strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return Check(
            name="orchestrator",
            ok=False,
            detail="claude auth status returned non-JSON output",
            fix="claude auth login",
        )
    if not isinstance(data, dict) or not data.get("loggedIn"):
        return Check(
            name="orchestrator",
            ok=False,
            detail="claude is not logged in (subscription lane)",
            fix="claude auth login",
        )
    return Check(name="orchestrator", ok=True, detail="claude subscription login OK")


def _check_provider_credentials(model: str, *, role: str, home: Path) -> Check:
    """Use the dispatch helpers; never read or return key values."""
    try:
        health = _lane_health(model, home)
    except (FileNotFoundError, ValueError) as exc:
        return Check(
            name=f"lanes:{role}:{model}",
            ok=False,
            detail=f"{model}: {exc}",
            fix=f"mini-ork providers configure {model}",
        )
    if health.ok:
        return Check(
            name=f"lanes:{role}:{model}",
            ok=True,
            detail=f"{model}: credentials present",
        )
    return Check(
        name=f"lanes:{role}:{model}",
        ok=False,
        detail=f"{model}: {health.reason}",
        fix=f"mini-ork providers configure {model}",
    )


# ── helpers (seams) ─────────────────────────────────────────────────────────


def _resolve_home_for(cwd: Path) -> Path:
    """The project's ``.mini-ork`` when present, else ``MINI_ORK_HOME``, else it anyway."""
    candidate = cwd / ".mini-ork"
    if candidate.is_dir():
        return candidate
    env_home = os.environ.get("MINI_ORK_HOME")
    return Path(env_home) if env_home else candidate


def _default_lane(home: Path) -> str:
    """Mirror ``MiniOrkAcpAgent._default_orchestrator_lane`` (try / fallback)."""
    try:
        from mini_ork.acp_orchestrator.config import default_lane
    except Exception:  # noqa: BLE001 — readiness never raises
        return "opus"
    try:
        return default_lane(home)
    except Exception:  # noqa: BLE001 — readiness never raises
        return "opus"


def _load_lane_map(home: Path) -> dict[str, str]:
    """Read ``agents.yaml`` via ``load_lanes(home)``; fall back to {}."""
    try:
        from mini_ork.web.recipes import load_lanes
    except Exception:  # noqa: BLE001 — readiness never raises
        return {}
    try:
        return dict(load_lanes(home))
    except Exception:  # noqa: BLE001 — readiness never raises
        return {}


def _lane_health(model: str, home: Path):
    """``mini-ork providers status``'s check: the engine registry (shadowed by
    the project's), keys from the shell or the project's secret store. The
    agent process never sourced that store, so the shell alone would report
    every configured gateway lane as missing."""
    from mini_ork.context import run_context_scope
    from mini_ork.dispatch.providers import lane_health, mini_ork_root
    from mini_ork.dispatch.secrets import read_secret_exports, secret_store_path

    env = {**os.environ, "MINI_ORK_HOME": str(home)}
    try:
        stored = dict(read_secret_exports(secret_store_path(env)))
    except Exception:  # noqa: BLE001 — an unreadable store reads as "no stored keys"
        stored = {}
    with run_context_scope({"MINI_ORK_HOME": str(home)}):
        return lane_health(model, str(mini_ork_root(None)), environment={**stored, **os.environ})


def _repo_root() -> Path:
    """The engine checkout (where ``bin/mini-ork`` and ``config/`` live) — not
    the user's project, which is the cwd when Zed launches the agent."""
    from mini_ork.dispatch.providers import mini_ork_root

    return Path(mini_ork_root(None))


# ── interactive plumbing ─────────────────────────────────────────────────────


def _print_checks(checks: Iterable[Check], out: IO[str]) -> None:
    for c in checks:
        mark = "✓" if c.ok else "✗"
        out.write(f"{mark} {c.name} — {c.detail}\n")
    out.write("\n")


def _ask_fix(stdin: IO[str], out: IO[str]) -> bool:
    """Prompt the user once; return True on 'y', False on 'n' / anything else."""
    out.write("Fix now? [y/N] ")
    out.flush()
    line = stdin.readline()
    if not line:
        return False
    return line.strip().lower() == "y"


def _run_fix(check: Check, cwd: Path, out: IO[str]) -> bool:
    """Execute the check's fix string as a subprocess chain. Return success."""
    out.write(f"running: {check.fix}\n")
    out.flush()
    # Multiple commands chained with `` && `` must run in sequence, each
    # inheriting the cwd so providers configure / init land in the right tree.
    parts = [p.strip() for p in check.fix.split("&&")]
    env = dict(os.environ)
    env.setdefault("MINI_ORK_ROOT", str(_repo_root()))
    for part in parts:
        argv = _argv_for_fix(part)
        if argv is None:
            out.write(f"  unknown fix command: {part}\n")
            return False
        try:
            rc = _run(argv, cwd=str(cwd), env=env).returncode
        except OSError as exc:
            out.write(f"  could not run {argv[0]!r}: {exc}\n")
            return False
        if rc != 0:
            return False
    return True


def _argv_for_fix(command: str) -> list[str] | None:
    """Map a fix string to an argv list. ``mini-ork`` uses the launcher path."""
    head, *rest = command.split()
    if head == "mini-ork":
        # ``[sys.executable, <engine>/bin/mini-ork, ...rest]`` — the launcher
        # script invokes the venv interpreter cleanly (kickoff §run_interactive).
        launcher = _repo_root() / "bin" / "mini-ork"
        return [sys.executable, str(launcher), *rest]
    if head in {"claude"}:
        return command.split()
    return None


__all__ = ["Check", "readiness", "run_interactive"]