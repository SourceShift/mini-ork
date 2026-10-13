#!/usr/bin/env python3
"""Live smoke for the Daytona node provider (costs a few cents of sandbox time).

Creates one sandbox through ``DaytonaProvider.create`` exactly as a run would,
then checks, through Daytona's preview proxy:

1. ``/v1/health`` answers and lists the control plane's engine sha (so the
   remote workspace will skip its own engine upload);
2. the node-agent accepts the minted bearer token and rejects a request
   without it;
3. ``attach`` re-finds the same node from its handle alone;
4. ``release`` deletes the sandbox, after which ``attach`` finds nothing.

The sandbox is released in a ``finally`` and also carries a 15-minute TTL, so
an interrupted smoke cannot leave it running.

Usage (key from env, ``$MINI_ORK_HOME/config/secrets.local.sh`` or the vault):

    PYTHONPATH=. python3 scripts/smoke_daytona_node.py [--engine-root DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_ork.remote.providers import get_node_provider  # noqa: E402


def _get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--engine-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    env = dict(os.environ)
    env.setdefault("MO_SANDBOX_MAX_AGE", str(15 * 60))   # → ttl_minutes=15
    run_id = f"smoke-{uuid.uuid4().hex[:8]}"
    provider = get_node_provider("daytona")
    sha = subprocess.run(["git", "-C", args.engine_root, "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")

    t0 = time.monotonic()
    node = provider.create(run_id=run_id, profile=None, env=env, engine_root=args.engine_root)
    print(f"created {node.name} handle={node.handle[:8]}… in {time.monotonic() - t0:.1f}s")
    try:
        status, body = _get(f"{node.url}/v1/health", dict(node.headers))
        health = json.loads(body or b"{}") if status == 200 else {}
        shas = [e.get("sha") for e in health.get("engine_shas", [])]
        check("health via preview proxy", status == 200, f"HTTP {status}")
        check("engine pre-staged", sha in shas, f"want {sha[:12]}, node has {[s[:12] for s in shas]}")

        authed = {**node.headers, "Authorization": f"Bearer {node.token}"}
        status, _ = _get(f"{node.url}/v1/sessions/{run_id}", authed)
        check("bearer accepted", status == 404, f"HTTP {status} (404 = authenticated, no session)")
        status, _ = _get(f"{node.url}/v1/sessions/{run_id}", dict(node.headers))
        check("no bearer rejected", status == 401, f"HTTP {status}")

        from mini_ork.remote.providers import daytona as dt

        sandbox = dt._client(env).get(node.handle)
        user_site = (sandbox.process.exec("python3 -m pip list --user 2>/dev/null | tail -n +3",
                                          timeout=60).result or "").strip()
        check("bootstrap leaves the user site untouched", not user_site,
              f"user-site packages: {user_site.splitlines()[:5] or 'none'}")

        again = provider.attach(node.handle, env=env)
        check("attach finds the same node",
              again is not None and again.url == node.url and again.token == node.token)
    finally:
        provider.release(node.handle, env=env)
    time.sleep(3)
    gone = provider.attach(node.handle, env=env)
    check("release deletes the sandbox", gone is None)
    print(f"total {time.monotonic() - t0:.1f}s")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
