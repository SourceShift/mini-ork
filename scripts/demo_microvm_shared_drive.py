#!/usr/bin/env python3
"""Live demo: two mini-ork agents in two microVMs sharing one drive.

Runs the exact mechanism ``run_minimal`` uses when ``MO_SANDBOX_BACKEND=microvm``:
it resolves a per-agent ``Workspace`` (a hardware-isolated microVM via the
microsandbox SDK) and bind-mounts one shared host directory into each microVM at
``/workspace``. Agent A (microVM 1) writes a file; Agent B (microVM 2, a
*different* microVM) reads it back — proving "each agent in its own env, all
sharing one directory," now with hardware isolation and the same server that
runs locally or in the cloud.

Usage:
    python3 scripts/demo_microvm_shared_drive.py [--image alpine:latest]

Requires the microsandbox SDK (``pip install microsandbox``) and a reachable
``msb server`` (start one with ``msb server --dev``). Cleans up its microVMs
(cattle) and the temp drive.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile

# Make the repo importable when run as a plain script (no editable install).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_ork.runtime.agent_workspace import resolve_agent_workspace  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--image", default=os.environ.get("MO_SANDBOX_IMAGE", "alpine:latest")
    )
    args = ap.parse_args()

    if importlib.util.find_spec("microsandbox") is None:
        print(
            "microsandbox SDK not installed — `pip install microsandbox` and "
            "start an `msb server` first.",
            file=sys.stderr,
        )
        return 2

    # microsandbox binds whatever host dir it is given; unlike docker's colima
    # /var/folders gap, no bind-visibility probe is needed. Prefer $HOME.
    base = os.path.expanduser("~")
    try:
        drive = tempfile.mkdtemp(prefix=".mo-microvm-drive-", dir=base)
    except OSError:
        drive = tempfile.mkdtemp(prefix=".mo-microvm-drive-")

    env = {
        "MO_SANDBOX_BACKEND": "microvm",
        "MO_SANDBOX_IMAGE": args.image,
        "MO_SHARED_DRIVE_ROOT": drive,
    }
    print(f"shared drive (host): {drive}")
    print(f"image: {args.image}\n")

    try:
        # --- Agent A: its own microVM, writes to the shared drive -----------
        ws_a, cwd_a = resolve_agent_workspace(drive, env=env)
        try:
            ws_a.up()
        except RuntimeError as exc:
            print(f"microVM boot failed (is `msb server` running?): {exc}",
                  file=sys.stderr)
            return 2
        print(f"[agent A] microVM {ws_a._name} up; exec_cwd={cwd_a}")
        try:
            ws_a.exec(
                "echo 'artifact written by agent A' > /workspace/shared.txt",
                cwd="/workspace", timeout=30,
            )
            _, whoami = ws_a.exec("hostname", cwd="/workspace", timeout=10)
            print(f"[agent A] wrote /workspace/shared.txt (vm id {whoami.strip()})")
        finally:
            ws_a.down()
            print(f"[agent A] microVM {ws_a._name} down (cattle)")

        # --- Agent B: a DIFFERENT microVM, reads what A wrote ---------------
        ws_b, cwd_b = resolve_agent_workspace(drive, env=env)
        ws_b.up()
        print(f"\n[agent B] microVM {ws_b._name} up; exec_cwd={cwd_b}")
        try:
            rc, out = ws_b.exec("cat /workspace/shared.txt", cwd="/workspace", timeout=30)
            _, whoami_b = ws_b.exec("hostname", cwd="/workspace", timeout=10)
            print(f"[agent B] vm id {whoami_b.strip()} reads shared.txt:")
            print(f"           {out.strip()!r}")
        finally:
            ws_b.down()
            print(f"[agent B] microVM {ws_b._name} down (cattle)")

        ok = (
            rc == 0
            and out.strip() == "artifact written by agent A"
            and ws_a._name != ws_b._name
        )
        print("\n" + ("PASS" if ok else "FAIL") +
              ": two distinct microVMs shared one drive"
              f" (A={ws_a._name}, B={ws_b._name})")
        # The drive survives both microVMs — it is the pet.
        print(f"drive persists after teardown: {os.path.exists(os.path.join(drive, 'shared.txt'))}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(drive, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
