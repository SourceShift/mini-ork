"""``mini-ork doctor`` as a gate, not a status dump.

The old command printed rows and returned 0 whatever it found, so a script (and
the installer) could not tell "ready" from "one credential away". A doctor that
is always green is decoration. This module turns the same checks into a verdict:

* ``ready``     — nothing is failing; exit 0.
* ``restartable`` — the only failures are ones a restart fixes (a serve process
  running older code than the engine tree on disk); exit 1.
* ``needs_person`` — something needs a human: a missing binary, an unmigrated
  database, no usable lane credential, an unset required profile knob; exit 2.

Every check is a dict ``{id, section, label, status, blocking, detail, fix}``
where ``status`` is ``ok`` | ``warn`` | ``fail`` and ``blocking`` classifies a
``fail`` as ``person`` or ``restart``. The verdict and its single ``Next:
<command>`` line are derived, never hand-written, so the IDE Setup page and any
CI job can read the same document via ``--json``.

A check that cannot be evaluated is a ``warn``, not a silent ``ok``: an
unreadable database or an unresponsive ``ps`` is not evidence that things are
fine. The one exception is a missing serve process — not running the UI is a
supported way to use mini-ork, so that stays ``ok``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Iterable, Mapping

from mini_ork import profile as profile_mod

#: verdict -> process exit code. Stable: scripts branch on these.
EXIT_CODES = {"ready": 0, "restartable": 1, "needs_person": 2}

_TAGS = {"ok": "[OK]      ", "warn": "[WARN]    ", "fail": "[FAIL]    "}

#: The binaries mini-ork itself shells out to. A missing one is a person's job.
_REQUIRED_TOOLS = ("sqlite3", "git", "curl", "python3")
#: Lane CLIs. mini-ork runs without either of them if a gateway lane is set up,
#: so their absence warns rather than blocks.
_OPTIONAL_TOOLS = (("claude", "anthropic"), ("codex", "codex"))
#: Environment variables a lane can take its credential from, family first.
_LANE_KEYS = (
    ("glm", "GLM_API_KEY"),
    ("kimi", "KIMI_API_KEY"),
    ("minimax", "MINIMAX_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
)


def _check(check_id: str, section: str, label: str, status: str, detail: str,
           *, blocking: str | None = None, fix: str = "") -> dict:
    return {
        "id": check_id,
        "section": section,
        "label": label,
        "status": status,
        "blocking": blocking if status == "fail" else None,
        "detail": detail,
        "fix": fix,
    }


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #

def _tool_checks(which) -> list[dict]:
    out = []
    for tool in _REQUIRED_TOOLS:
        found = which(tool)
        out.append(_check(
            f"tool:{tool}", "Tools", tool,
            "ok" if found else "fail",
            f"{tool}" if found else f"{tool} not on PATH (mini-ork shells out to it)",
            blocking=None if found else "person",
            fix="" if found else f"install {tool} and put it on PATH",
        ))
    return out


def _home_checks(home: str, env: Mapping[str, str]) -> list[dict]:
    out = [
        _check("home", "Engine", "MINI_ORK_HOME", "ok",
               f"MINI_ORK_HOME={env.get('MINI_ORK_HOME') or home}"),
    ]
    db = env.get("MINI_ORK_DB") or ""
    if db:
        out.append(_check("db-path", "Engine", "MINI_ORK_DB", "ok", f"MINI_ORK_DB={db}"))
    else:
        out.append(_check(
            "db-path", "Engine", "MINI_ORK_DB", "warn",
            "MINI_ORK_DB unset (default: $MINI_ORK_HOME/state.db)",
            fix=f"export MINI_ORK_DB={os.path.join(home, 'state.db')}",
        ))

    state = db or os.path.join(home, "state.db")
    if not os.path.isdir(home):
        out.append(_check(
            "home-exists", "Engine", "home directory", "fail",
            f"no mini-ork home at {home}",
            blocking="person", fix="mini-ork init",
        ))
        return out
    if not os.path.isfile(state):
        out.append(_check(
            "db-exists", "Engine", "state.db", "fail",
            f"home exists but {state} does not — nothing has been initialized here",
            blocking="person", fix="mini-ork init",
        ))
    else:
        out.append(_check("db-exists", "Engine", "state.db", "ok", f"{state} present"))
    return out


def _engine_checks(root: str, home: str) -> list[dict]:
    """Which engine is running and which one last wrote this home.

    Two engines can serve one project (the IDE's serve, the copy vendored in
    ``.mini-ork``) and they drift; the commit separates two checkouts of one
    release, the recorded DB version separates a home from an engine that never
    migrated it.
    """
    from mini_ork.cli.main import _db_engine_version, _engine_commit, engine_version

    version = engine_version(root)
    commit = _engine_commit(root)
    out = [_check(
        "engine-root", "Engine", "engine root", "ok",
        f"root: {root} (v{version or '?'}{f' @{commit}' if commit else ''})",
    )]
    recorded = _db_engine_version(home)
    if not recorded:
        out.append(_check(
            "db-version", "Engine", "home DB version", "fail",
            "home DB records no engine version (never migrated by a versioned "
            "engine, or no schema_migrations row)",
            blocking="person", fix="mini-ork migrate",
        ))
    elif not version:
        out.append(_check(
            "db-version", "Engine", "home DB version", "warn",
            f"home DB last migrated by v{recorded}; this engine's version could "
            "not be read, so drift cannot be ruled out",
            fix="run `mini-ork version` from the engine you mean to use",
        ))
    elif recorded == version:
        out.append(_check(
            "db-version", "Engine", "home DB version", "ok",
            f"home DB last migrated by v{recorded} (this engine)",
        ))
    else:
        out.append(_check(
            "db-version", "Engine", "home DB version", "fail",
            f"home DB last migrated by v{recorded}, this engine is v{version} — "
            "a newer engine has written this home",
            blocking="person", fix="mini-ork migrate",
        ))
    return out


def _provider_checks(which, resolved: Mapping[str, str],
                     store_error: str = "") -> list[dict]:
    out = []
    for tool, family in _OPTIONAL_TOOLS:
        if which(tool):
            out.append(_check(f"cli:{family}", "Provider preflight", family, "ok",
                              f"{family} ({tool} CLI present)"))
        else:
            out.append(_check(f"cli:{family}", "Provider preflight", family, "warn",
                              f"{family} ({tool} CLI missing)",
                              fix=f"install the {tool} CLI to use the {family} lane"))
    if store_error:
        out.append(_check(
            "credential-store", "Provider preflight", "credential store", "warn",
            f"local credential store unavailable: {store_error}",
            fix="check $MINI_ORK_HOME/config/secrets.local.sh",
        ))

    usable = []
    for family, var in _LANE_KEYS:
        if resolved.get(var):
            usable.append(family)
            out.append(_check(f"key:{family}", "Provider preflight", family, "ok",
                              f"{family} (${var} set)"))
        else:
            out.append(_check(f"key:{family}", "Provider preflight", family, "warn",
                              f"{family} (${var} unset; run: mini-ork providers configure {family})",
                              fix=f"mini-ork providers configure {family}"))

    # The aggregate is the only provider row that can block: a run with no
    # usable credential dies at the first lane, ~19 minutes in, silently.
    if usable:
        out.append(_check(
            "provider-key", "Provider preflight", "usable lane credential", "ok",
            f"{len(usable)} usable: {', '.join(usable)}",
        ))
    else:
        out.append(_check(
            "provider-key", "Provider preflight", "usable lane credential", "fail",
            "no lane credential found — every run would fail at the first dispatch",
            blocking="person", fix="mini-ork providers configure minimax",
        ))
    return out


def _vault_checks(env: Mapping[str, str]) -> list[dict]:
    """Advisory only. The simple tier has no vault, and that is legitimate."""
    from mini_ork.dispatch.vault import VaultError, vault_environment

    try:
        environment = vault_environment(dict(env))
    except VaultError as exc:
        return [_check("vault", "Secret vault", "vault config", "warn",
                       f"vault config: {exc}", fix="mini-ork vault status")]
    out = []
    for backend_status in environment.status():
        out.append(_check(
            f"vault:{backend_status.backend}", "Secret vault", backend_status.backend,
            "ok" if backend_status.ready else "warn",
            f"{backend_status.backend}: {backend_status.detail}",
            fix="" if backend_status.ready else "mini-ork vault status",
        ))
    gap = environment.protection_gap()
    if gap:
        out.append(_check("vault-gap", "Secret vault", "protection gap", "warn", gap,
                          fix="mini-ork vault status"))
    return out


def _profile_checks(name: str, root: str, env: Mapping[str, str],
                    inherited: Mapping[str, str] | None = None) -> list[dict]:
    """The laptop tier's required knobs — fail closed, never a silent empty."""
    from mini_ork.profile import ProfileError, profile_path

    inherited = env if inherited is None else inherited
    try:
        singles, groups = profile_mod.required(name, root)
        applied = profile_mod.defaults(name, root)
    except ProfileError as exc:
        return [_check(
            "profile", "Profile", name, "fail", str(exc),
            blocking="person", fix=f"create {profile_path(name, root)}",
        )]

    out = []
    for key, reason in singles:
        if str(env.get(key) or "").strip():
            out.append(_check(f"profile:{key}", "Profile", key, "ok", f"${key} set"))
        else:
            why = f" — {reason}" if reason else ""
            out.append(_check(
                f"profile:{key}", "Profile", key, "fail",
                f"${key} is required by profile {name!r} and is unset{why}",
                blocking="person",
                fix=f"export {key}=<value>  (see {profile_path(name, root)})",
            ))
    for group in groups:
        present = [key for key in group if str(env.get(key) or "").strip()]
        if present:
            out.append(_check(
                f"profile:{'+'.join(group)}", "Profile", group[0], "ok",
                f"one of {', '.join(group)} set ({present[0]})",
            ))
        else:
            out.append(_check(
                f"profile:{'+'.join(group)}", "Profile", group[0], "fail",
                f"profile {name!r} needs one of {', '.join(group)}",
                blocking="person", fix=f"mini-ork providers configure {group[0].split('_')[0].lower()}",
            ))
    overridden = [key for key in applied if str(inherited.get(key) or "").strip()]
    out.append(_check(
        "profile-defaults", "Profile", "defaults", "ok",
        f"{len(applied)} knob(s) defined; {len(overridden)} set by the environment",
    ))
    return out


def _newest_source_mtime(root: str) -> float:
    """Newest mtime under ``mini_ork/`` — the code a serve process must have loaded."""
    newest = 0.0
    base = os.path.join(root, "mini_ork")
    for dirpath, _, filenames in os.walk(base):
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, filename)))
            except OSError:
                continue
    return newest


def _serve_started() -> tuple[int, float] | None:
    """``(pid, start_epoch)`` of a running ``mini-ork serve``, or None."""
    try:
        found = subprocess.run(["pgrep", "-f", "mini-ork serve"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in found.stdout.split():
        pid = int(line) if line.isdigit() else 0
        if not pid:
            continue
        try:
            started = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=5)
            stamp = started.stdout.strip()
            when = time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        return pid, when
    return None


def _serve_checks(root: str, started=None) -> list[dict]:
    probe = _serve_started if started is None else started
    running = probe()
    if not running:
        return [_check("serve", "Serve", "serve process", "ok",
                       "no serve process running (the UI is optional)")]
    pid, when = running
    newest = _newest_source_mtime(root)
    if newest and when < newest:
        ago = (newest - when) / 60.0
        return [_check(
            "serve", "Serve", "serve process", "fail",
            f"serve (pid {pid}) started before the newest engine source "
            f"({ago:.0f} min older) — it is serving code that is no longer on disk",
            blocking="restart", fix="restart the server: mini-ork serve",
        )]
    return [_check("serve", "Serve", "serve process", "ok",
                   f"serve running (pid {pid}), started after the newest source")]


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #

def run(home: str | None = None, root: str | None = None, *,
        profile: str | None = None, env: Mapping[str, str] | None = None,
        which=shutil.which, serve_probe=None) -> dict:
    """Every check plus the verdict derived from them. Pure: reads, never writes."""
    environ = dict(os.environ if env is None else env)
    if profile:
        environ[profile_mod.PROFILE_ENV] = profile   # the flag beats the env
    inherited = dict(environ)                        # before defaults are filled
    home = home or environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    root = root or environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))

    applied: dict[str, str] = {}
    active = profile_mod.active(environ)
    checks: list[dict] = []
    checks += _tool_checks(which)
    checks += _home_checks(home, environ)
    if os.path.isdir(home):
        checks += _engine_checks(root, home)
    # Credentials and the vault are properties of the environment, not the
    # home: a laptop with no `.mini-ork` yet should still learn it has no key
    # before it runs `init` and then hits the wall at the first dispatch.
    from mini_ork.dispatch.providers import provider_environment
    from mini_ork.dispatch.secrets import SecretStoreError

    store_error = ""
    try:
        resolved: Mapping[str, str] = provider_environment(dict(environ))
    except SecretStoreError as exc:
        resolved, store_error = dict(environ), str(exc)
    checks += _provider_checks(which, resolved, store_error)
    checks += _vault_checks(environ)
    if active:
        # Profile defaults fill only unset knobs, and the required-knob check
        # reads the FILLED environment — exactly what a dispatched run sees.
        try:
            applied = profile_mod.fill(environ, name=active, root=root)
        except profile_mod.ProfileError:
            applied = {}  # the profile check below reports the reason
        # A key persisted in the local store (not exported) still counts for the
        # profile's "one provider key" requirement: the check reads what a
        # dispatched lane would actually see.
        view: Mapping[str, str] = {**environ, **resolved}
        checks += _profile_checks(active, root, view, inherited)
    checks += _serve_checks(root, serve_probe)

    failures = [c for c in checks if c["status"] == "fail"]
    person = [c for c in failures if c["blocking"] != "restart"]
    restart = [c for c in failures if c["blocking"] == "restart"]
    if person:
        verdict, chosen = "needs_person", person[0]
    elif restart:
        verdict, chosen = "restartable", restart[0]
    else:
        verdict, chosen = "ready", None

    return {
        "verdict": verdict,
        "next": chosen["fix"] if chosen else "",
        "profile": active,
        "applied_defaults": applied,
        "checks": checks,
    }


def render(result: Mapping) -> str:
    """The human report. Sections in check order; the last line is the verdict."""
    lines: list[str] = ["=== mini-ork doctor ==="]
    section = None
    for check in result["checks"]:
        if check["section"] != section:
            if section is not None:
                lines.append("")
            section = check["section"]
            lines.append(f"{section}:")
        lines.append(f"  {_TAGS[check['status']]}{check['detail']}")
    lines.append("")
    lines.append(f"verdict: {result['verdict']}")
    if result["next"]:
        lines.append(f"Next: {result['next']}")
    return "\n".join(lines) + "\n"


def build_parser():
    """The one declaration of this command's flags.

    ``main`` parses with it, and so does ``tests/unit/test_ide_pages_actions.py``
    — every ``{"cli": [...]}`` a page offers must parse against the parser the
    command actually uses, and an inline ``ArgumentParser`` is invisible to it.
    """
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--home")
    parser.add_argument("-h", "--help", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None, root: str | None = None) -> int:
    import sys

    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:
        return 2
    if args.help:
        sys.stdout.write(
            "mini-ork doctor — a verdict, not a status dump.\n\n"
            "Usage:\n  mini-ork doctor [--json] [--profile <name>] [--home <dir>]\n\n"
            "Exit 0 ready, 1 a restart fixes it, 2 a person needs to act.\n"
            "The last line is `Next: <command>` for the first failing check.\n")
        return 0
    result = run(home=args.home, root=root, profile=args.profile)
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(render(result))
    return EXIT_CODES[result["verdict"]]
