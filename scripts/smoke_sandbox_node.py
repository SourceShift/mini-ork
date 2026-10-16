#!/usr/bin/env python3
"""Live smoke for a sandbox node provider (costs a few cents of sandbox time).

Default mode creates one sandbox through the provider's ``create`` exactly as a
run would, then checks, through Daytona's preview proxy:

1. ``/v1/health`` answers and lists the control plane's engine sha (so the
   remote workspace will skip its own engine upload);
2. the node-agent accepts the minted bearer token and rejects a request
   without it;
3. ``attach`` re-finds the same node from its handle alone;
4. ``release`` deletes the sandbox, after which ``attach`` finds nothing.

``--per-node`` smoke-tests the ``MO_NODE_ISOLATION`` path instead, which is the
only thing that can prove the properties the unit tests can only model:

5. two nodes of one run get two DISTINCT sandboxes, and the cloud accepts the
   labels we generate for them (``mo.node`` on each, which is where an invalid
   character would fail the create before the sandbox ever boots);
6. re-acquiring a node attaches to its sandbox rather than minting a third;
7. ``release_all_nodes`` frees every node — checked by polling, because a
   cloud's delete is fire-and-forget and a same-second list still shows them.

Every sandbox is released in a ``finally`` and also carries a 15-minute TTL, so
an interrupted smoke cannot leave it running.

Usage (the provider's key from env, ``$MINI_ORK_HOME/config/secrets.local.sh``
or the vault):

    PYTHONPATH=. python3 scripts/smoke_sandbox_node.py --provider daytona|e2b [--engine-root DIR]
    PYTHONPATH=. python3 scripts/smoke_sandbox_node.py --per-node
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_ork.remote.providers import get_node_provider  # noqa: E402
from mini_ork.remote.providers import run_nodes  # noqa: E402
from mini_ork.remote.providers.sandbox_node import LABEL_NODE, LABEL_RUN  # noqa: E402


def _get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _check_endpoint(provider, node, run_id: str, sha: str, check) -> None:
    """The provider-level checks that hold for any endpoint, node or not."""
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

    sandbox = provider.get_sandbox(node.handle, os.environ)
    _, user_site = sandbox.run("python3 -m pip list --user 2>/dev/null | tail -n +3", timeout=60)
    user_site = user_site.strip()
    check("bootstrap leaves the user site untouched", not user_site,
          f"user-site packages: {user_site.splitlines()[:5] or 'none'}")

    again = provider.attach(node.handle, env=dict(os.environ))
    check("attach finds the same node",
          again is not None and again.url == node.url and again.token == node.token)


def _wait_gone(provider, handles: list[str], env, *, timeout: float = 120.0) -> bool:
    """Poll until the cloud stops listing every handle.

    A sandbox delete is fire-and-forget, so a list in the same second still
    shows the sandbox and would read as a leak. Poll rather than sleep-and-hope.
    """
    deadline = time.monotonic() + timeout
    while True:
        alive = [h for h in handles if provider.get_sandbox(h, env) is not None]
        if not alive:
            return True
        if time.monotonic() >= deadline:
            print(f"  still alive after {timeout:.0f}s: {[h[:8] for h in alive]}")
            return False
        time.sleep(3)


def _single(provider, args, env, sha: str, run_id: str, check) -> None:
    t0 = time.monotonic()
    node = provider.create(run_id=run_id, profile=None, env=env, engine_root=args.engine_root)
    print(f"created {node.name} handle={node.handle[:8]}… in {time.monotonic() - t0:.1f}s")
    try:
        _check_endpoint(provider, node, run_id, sha, check)
    finally:
        provider.release(node.handle, env=env)
    check("release deletes the sandbox", _wait_gone(provider, [node.handle], env))


def _per_node(provider, args, env, sha: str, run_id: str, check) -> None:
    """``MO_NODE_ISOLATION=1``: one sandbox per node, and none left behind."""
    run_dir = Path(tempfile.mkdtemp(prefix="mo-per-node-smoke-"))
    nodes = ("implementer_1", "reviewer_1")
    made: dict[str, str] = {}
    t0 = time.monotonic()

    def mine() -> list:
        """Only THIS smoke's sandboxes.

        ``list_sandboxes`` is the provider-wide TTL sweep — it returns every
        mini-ork sandbox on the account. Counting its raw length would read a
        run live elsewhere as a duplicate or a leak, so the "exactly the two"
        checks scope to this smoke's ``mo.run``.
        """
        return [sb for sb in provider.list_sandboxes(env)
                if dict(getattr(sb, "labels", None) or {}).get(LABEL_RUN) == run_id[:63]]

    try:
        for name in nodes:
            ep = run_nodes.acquire_node(provider, run_id=run_id, node_id=name,
                                        run_dir=run_dir, profile=None, env=env,
                                        engine_root=args.engine_root)
            made[name] = ep.handle
            print(f"  [{name}] name={ep.name} handle={ep.handle[:8]}…")
            if name == nodes[0]:
                # The per-node sandbox must satisfy everything a run-level one
                # does — it is booted by the same create, only a different name.
                _check_endpoint(provider, ep, run_id, sha, check)
        elapsed = time.monotonic() - t0
        print(f"created {len(nodes)} node sandboxes in {elapsed:.1f}s")

        check("two nodes got two sandboxes", len(set(made.values())) == 2,
              f"handles: {[h[:8] for h in made.values()]}")

        # The node label is what a cloud validates; a bad value fails the create.
        labels_ok = True
        for name, handle in made.items():
            labels = dict(getattr(provider.get_sandbox(handle, env), "labels", None) or {})
            print(f"  [{name}] mo.node={labels.get(LABEL_NODE)!r}")
            labels_ok = labels_ok and labels.get(LABEL_NODE) == name
        check("each sandbox carries its own mo.node label", labels_ok)

        cloud = mine()
        check("cloud lists exactly the two", len(cloud) == 2, f"listed {len(cloud)}")

        again = run_nodes.acquire_node(provider, run_id=run_id, node_id=nodes[0],
                                      run_dir=run_dir, profile=None, env=env,
                                      engine_root=args.engine_root)
        check("re-acquire attaches, never a third", again.handle == made[nodes[0]])
        check("no sandbox leaked by the re-acquire", len(mine()) == 2)

        records = {n: run_nodes.read_node_record(run_dir, n) for n in nodes}
        check("one record per node on disk",
              all(r and r.get("handle") == made[n] for n, r in records.items()))
    finally:
        released = run_nodes.release_all_nodes(run_dir, env)
    check("release_all_nodes freed both", released == 2, f"released {released}")
    check("no per-node sandbox survives release",
          _wait_gone(provider, list(made.values()), env))
    try:
        for leftovers in run_dir.glob("**/*"):
            if leftovers.is_file():
                leftovers.unlink()
        run_dir.rmdir()
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--provider", default="daytona", help="node provider to smoke (default daytona)")
    ap.add_argument("--engine-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--per-node", action="store_true",
                    help="smoke MO_NODE_ISOLATION=1 (two sandboxes, one per node)")
    args = ap.parse_args()

    env = dict(os.environ)
    env.setdefault("MO_SANDBOX_MAX_AGE", str(15 * 60))   # → ttl_minutes=15
    run_id = f"smoke-{uuid.uuid4().hex[:8]}"
    provider = get_node_provider(args.provider)
    sha = subprocess.run(["git", "-C", args.engine_root, "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")

    t0 = time.monotonic()
    print(f"provider={args.provider} run={run_id} mode={'per-node' if args.per_node else 'run-level'}")
    if args.per_node:
        _per_node(provider, args, env, sha, run_id, check)
    else:
        _single(provider, args, env, sha, run_id, check)
    print(f"total {time.monotonic() - t0:.1f}s")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"FAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
