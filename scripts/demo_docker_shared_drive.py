#!/usr/bin/env python3
"""Live demo: two mini-ork agents in two Docker containers sharing one drive.

Runs the exact mechanism ``run_minimal`` uses when
``MO_SANDBOX_BACKEND=docker``: it resolves a per-agent ``Workspace`` and
bind-mounts one shared host directory into each container at ``/workspace``.
Agent A (container 1) writes a file; Agent B (container 2, a *different*
environment) reads it back — proving "each agent in its own env, all sharing
one directory."

Usage:
    python3 scripts/demo_docker_shared_drive.py [--image alpine:latest]

Requires a running Docker daemon. Cleans up its containers and the temp drive.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

# Make the repo importable when run as a plain script (no editable install).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_ork.runtime.agent_workspace import resolve_agent_workspace  # noqa: E402


def _bind_visible_dir(image: str) -> str | None:
    """Return a bind-visible host dir (colima does not share /var/folders)."""
    exe = shutil.which("docker")
    if not exe:
        return None
    for base in (tempfile.gettempdir(), os.path.expanduser("~")):
        try:
            probe = tempfile.mkdtemp(prefix=".mo-drive-", dir=base)
        except OSError:
            continue
        r = subprocess.run(
            [exe, "run", "--rm", "-v", f"{probe}:/probe", image,
             "sh", "-c", "echo ok > /probe/sentinel"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.exists(os.path.join(probe, "sentinel")):
            os.remove(os.path.join(probe, "sentinel"))
            return probe
        shutil.rmtree(probe, ignore_errors=True)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.environ.get("MO_SANDBOX_IMAGE", "alpine:latest"))
    args = ap.parse_args()

    if not shutil.which("docker"):
        print("docker CLI not found — start Docker/colima first.", file=sys.stderr)
        return 2

    drive = _bind_visible_dir(args.image)
    if not drive:
        print("could not find a docker-bind-visible directory.", file=sys.stderr)
        return 2

    env = {
        "MO_SANDBOX_BACKEND": "docker",
        "MO_SANDBOX_IMAGE": args.image,
        "MO_SHARED_DRIVE_ROOT": drive,
    }
    print(f"shared drive (host): {drive}")
    print(f"image: {args.image}\n")

    try:
        # --- Agent A: its own container, writes to the shared drive ---------
        ws_a, cwd_a = resolve_agent_workspace(drive, env=env)
        ws_a.up()
        print(f"[agent A] container {ws_a._name} up; exec_cwd={cwd_a}")
        try:
            ws_a.exec(
                "echo 'artifact written by agent A' > /workspace/shared.txt",
                cwd="/workspace", timeout=30,
            )
            rc, whoami = ws_a.exec("hostname", cwd="/workspace", timeout=10)
            print(f"[agent A] wrote /workspace/shared.txt (container id {whoami.strip()})")
        finally:
            ws_a.down()
            print(f"[agent A] container {ws_a._name} down (cattle)")

        # --- Agent B: a DIFFERENT container, reads what A wrote -------------
        ws_b, cwd_b = resolve_agent_workspace(drive, env=env)
        ws_b.up()
        print(f"\n[agent B] container {ws_b._name} up; exec_cwd={cwd_b}")
        try:
            rc, out = ws_b.exec("cat /workspace/shared.txt", cwd="/workspace", timeout=30)
            _, whoami_b = ws_b.exec("hostname", cwd="/workspace", timeout=10)
            print(f"[agent B] container id {whoami_b.strip()} reads shared.txt:")
            print(f"           {out.strip()!r}")
        finally:
            ws_b.down()
            print(f"[agent B] container {ws_b._name} down (cattle)")

        ok = (
            rc == 0
            and out.strip() == "artifact written by agent A"
            and ws_a._name != ws_b._name
        )
        print("\n" + ("PASS" if ok else "FAIL") +
              ": two distinct containers shared one drive"
              f" (A={ws_a._name}, B={ws_b._name})")
        # The drive survives both containers — it is the pet.
        print(f"drive persists after teardown: {os.path.exists(os.path.join(drive, 'shared.txt'))}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(drive, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
