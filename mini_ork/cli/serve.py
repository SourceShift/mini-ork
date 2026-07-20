"""Python port of bin/mini-ork-serve — boot the local observability UI.

Strangler-fig parity port of the launcher. The server itself is already Python
(``mini_ork.web.app:app``); this replicates the bash entrypoint's arg parsing,
preflight checks (state.db present, fastapi+uvicorn importable), and the final
``uvicorn`` exec. Returns an int rc (like the bash exit codes) so it's testable
without actually binding a port; ``run_server`` is split out so tests can assert
the composed uvicorn argv without launching it.
"""
from __future__ import annotations

import importlib.util
import os
import sys

_USAGE = """Usage: mini-ork serve [--port N] [--host ADDR] [--home DIR] [--reload]

Boot the local read-only observability UI. Binds 127.0.0.1:7090 by default.

Options:
  --port N      HTTP port (default 7090)
  --host ADDR   Bind address (default 127.0.0.1 — use 0.0.0.0 to expose)
  --home DIR    Override .mini-ork home (default: $MINI_ORK_HOME or ./.mini-ork)
  --reload      Enable uvicorn auto-reload (dev only)
  --help        This message

Requirements:
  Run make web-deps from the repo root.

The UI bundle (web/dist) is auto-served when built. Without it, the API
remains usable at /api/v1/... and a JSON hint is returned at /.
"""


class _Exit(Exception):
    def __init__(self, code: int):
        self.code = code


def _parse(argv: list[str]) -> dict:
    opts = {"port": "7090", "host": "127.0.0.1", "home": "", "reload": ""}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_USAGE)
            raise _Exit(0)
        elif a == "--port":
            opts["port"] = argv[i + 1]; i += 2
        elif a == "--host":
            opts["host"] = argv[i + 1]; i += 2
        elif a == "--home":
            opts["home"] = argv[i + 1]; i += 2
        elif a == "--reload":
            opts["reload"] = "--reload"; i += 1
        else:
            sys.stderr.write(f"Unknown flag: {a}\n")
            sys.stdout.write(_USAGE)
            raise _Exit(2)
    return opts


def _deps_present() -> bool:
    return (importlib.util.find_spec("fastapi") is not None
            and importlib.util.find_spec("uvicorn") is not None)


def uvicorn_argv(host: str, port: str, reload_flag: str) -> list[str]:
    cmd = [sys.executable, "-m", "uvicorn", "mini_ork.web.app:app",
           "--host", host, "--port", port, "--log-level", "info"]
    if reload_flag:
        cmd.append(reload_flag)
    return cmd


def main(argv: list[str] | None = None, *, root: str | None = None,
         _exec: bool = True) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    try:
        opts = _parse(argv)
    except _Exit as e:
        return e.code

    if opts["home"]:
        os.environ["MINI_ORK_HOME"] = opts["home"]
    resolved_home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db_path = os.path.join(resolved_home, "state.db")
    if not os.path.isfile(db_path):
        sys.stderr.write(f"mini-ork serve: no state.db at {db_path}\n")
        sys.stderr.write("  Run 'mini-ork init' first, or pass --home to point elsewhere.\n")
        return 1
    if not _deps_present():
        sys.stderr.write("mini-ork serve: missing 'fastapi' or 'uvicorn'\n")
        sys.stderr.write("  Run 'make web-deps' from the repo root, or follow docs/UI.md.\n")
        return 1

    sys.stdout.write(
        "→ mini-ork serve\n"
        f"  host : {opts['host']}:{opts['port']}\n"
        f"  home : {resolved_home}\n"
        f"  db   : {db_path}\n"
        f"  ui   : http://{opts['host']}:{opts['port']}/  (api: /api)\n\n")
    cmd = uvicorn_argv(opts["host"], opts["port"], opts["reload"])
    if not _exec:
        return 0
    os.chdir(root)
    os.execvp(cmd[0], cmd)  # replaces process, mirroring bash `exec`


if __name__ == "__main__":
    raise SystemExit(main())
