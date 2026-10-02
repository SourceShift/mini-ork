"""``mini-ork nodes`` — list, ping and doctor-check remote agent nodes
(remote-nodes-13).

* ``ls``            — the registered nodes (``config/nodes.yaml``).
* ``ping <node>``   — one ``/v1/health`` probe; exit 0 when the node answers.
* ``doctor``        — an ordered checklist for a run's environment, with a fix
                      hint per failure; exit 0 only when every check passed.

``doctor`` drives the production seams rather than re-implementing them: the
remote workspace factory (``--env`` binds node, image, resources, network),
``RemoteWorkspace.up()`` (its ``remote.setup.step`` records ARE checks 3-5),
``exec`` for the in-session toolchain, and ``providers.dispatch_model`` into
the doctor's own session for the per-lane auth smoke — so a lane whose key is
bad fails here exactly as it would in a run, minus the long timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

__all__ = ["main"]

CHECKS: tuple[str, ...] = (
    "node reachable",
    "token accepted",
    "engine sha present or uploaded",
    "image prepared",
    "session up",
    "toolchain inside the session",
    "per-lane auth smoke",
    "network level effective",
    "session down",
)

AUTH_SMOKE_TIMEOUT_S = 30.0
AUTH_SMOKE_PROMPT = "Reply with the single word OK."
# The interpreter the in-session probe imports mini_ork with (the agent image
# puts the engine on its PYTHONPATH).
PYTHON = "python3"
# CLIs a lane kind needs inside the session.
_LANE_KIND_TOOLS = {"anthropic-native": "claude", "anthropic-compat": "claude",
                    "codex-native": "codex", "openai-compat": "codex",
                    "opencode-native": "opencode"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-ork nodes",
                                     description="List, ping, or doctor-check agent nodes.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("ls", help="list registered nodes")
    ping = sub.add_parser("ping", help="probe one node's /v1/health")
    ping.add_argument("node")
    doctor = sub.add_parser("doctor", help="ordered preflight for an environment")
    doctor.add_argument("--env", default="", help="environment profile (as for run --env)")
    doctor.add_argument("--recipe", default="", help="smoke the lanes this recipe's roles use")
    doctor.add_argument("--lane", action="append", default=[],
                        help="smoke this providers.yaml lane (repeatable)")
    doctor.add_argument("--no-llm", action="store_true", help="skip the per-lane auth smoke")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.action == "ls":
        return _action_ls()
    if args.action == "ping":
        return _action_ping(args.node)
    return doctor(env_name=args.env, recipe=args.recipe, lanes=args.lane, no_llm=args.no_llm)


def _action_ls() -> int:
    from mini_ork.remote.nodes import load_registry

    for name, entry in (load_registry() or {}).items():
        print(f"{name}\t{entry.get('url') or ''}\tmax_sessions={int(entry.get('max_sessions') or 1)}")
    return 0


def _action_ping(node_name: str) -> int:
    import urllib.error
    import urllib.request

    from mini_ork.remote.nodes import load_registry

    url = str(((load_registry() or {}).get(node_name) or {}).get("url") or "")
    if not url:
        print(f"node {node_name!r} is not in config/nodes.yaml", file=sys.stderr)
        return 2
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/v1/health", timeout=5) as resp:
            body = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"{node_name}: unreachable ({exc})")
        return 1
    print(f"{node_name}: ok (sessions={body.get('sessions', '?')}, docker_ok={body.get('docker_ok')})")
    return 0


# --------------------------------------------------------------------------- doctor


class _Report:
    def __init__(self, out: Callable[[str], None]) -> None:
        self._out = out
        self.failed = False

    def line(self, idx: int, status: str, detail: str) -> None:
        self._out(f"{idx}. [{status}]\t{CHECKS[idx - 1]}\t{detail}")
        if status == "FAIL":
            self.failed = True


def _http_code(exc: BaseException) -> int:
    m = re.search(r"HTTP (\d{3})", str(exc))
    return int(m.group(1)) if m else 0


def _doctor_run_dir(env: Mapping[str, str], run_id: str) -> str:
    """A run dir with pinned roots, so the smoke dispatch maps paths exactly
    as a real run's would (and the session marker has a home)."""
    from dataclasses import asdict

    from mini_ork.runtime.run_roots import resolve_run_roots

    home = env.get("MINI_ORK_HOME") or ".mini-ork"
    run_dir = Path(home) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    roots = resolve_run_roots(str(run_dir), env=env)
    (run_dir / "run_profile.json").write_text(json.dumps({"roots": asdict(roots)}) + "\n")
    return str(run_dir)


def _recipe_lanes(recipe: str, env: Mapping[str, str]) -> list[str]:
    """The providers.yaml lanes a recipe's roles resolve to (agents.yaml policy)."""
    import yaml

    from mini_ork.dispatch.llm_dispatch import resolve_lane_family, resolve_lane_model

    root = env.get("MINI_ORK_RECIPE_ROOT") or env.get("MINI_ORK_ROOT") or os.getcwd()
    wf = Path(root) / "recipes" / recipe / "workflow.yaml"
    nodes = (yaml.safe_load(wf.read_text(encoding="utf-8")) or {}).get("nodes") or []
    home = env.get("MINI_ORK_HOME", "")
    lanes: list[str] = []
    for node in nodes:
        if not isinstance(node, dict) or not node.get("type"):
            continue
        lane = node.get("model_lane") or resolve_lane_model(node["type"], env.get("MINI_ORK_ROOT", ""), home)
        lane = resolve_lane_family(str(lane), env.get("MINI_ORK_ROOT", ""), home)
        if lane and lane not in lanes:
            lanes.append(lane)
    return lanes


def doctor(*, env_name: str = "", recipe: str = "", lanes: Sequence[str] = (),
           no_llm: bool = False, out: Callable[[str], None] = print) -> int:
    """Run the ordered checks, stopping at the first failure, and always leave
    no session behind (check 9 verifies that). 0 only when everything passed."""
    from mini_ork.context import context_env_snapshot
    from mini_ork.runtime.backends.remote import _factory
    from mini_ork.runtime.workspace_session import close_run_session

    report = _Report(out)
    run_id = f"doctor-{uuid.uuid4().hex[:8]}"
    env = {**context_env_snapshot(), "MINI_ORK_RUN_ID": run_id, "MO_PLACEMENT": "remote"}
    if env_name:
        env["MO_NODE_ENV"] = env_name
    try:
        env["MINI_ORK_RUN_DIR"] = _doctor_run_dir(env, run_id)
        ws = _factory(env=env)
    except Exception as exc:  # noqa: BLE001 — node/profile resolution is check 1's job
        report.line(1, "FAIL", f"{exc} — check config/nodes.yaml, the profile's node: and "
                               "its token_env")
        return 1
    state = {"registered": False, "provisioned": False}
    try:
        rc = _checks(ws, env, run_id, report, state, recipe=recipe, lanes=lanes, no_llm=no_llm)
    finally:
        if state["registered"]:
            close_run_session(run_id)
        else:
            try:
                ws.down()
            except Exception:  # noqa: BLE001
                pass
    if rc == 0:   # 9. session down — verified on the node
        try:
            ws._json("GET", f"/v1/sessions/{run_id}", retries=1)
            report.line(9, "FAIL", "the session is still on the node after teardown")
        except Exception as exc:  # noqa: BLE001
            report.line(9, "PASS" if _http_code(exc) == 404 else "FAIL",
                        "torn down" if _http_code(exc) == 404 else f"teardown unconfirmed: {exc}")
    return 1 if report.failed else rc


def _checks(ws, env: dict, run_id: str, report: _Report, state: dict, *, recipe: str,
            lanes: Sequence[str], no_llm: bool) -> int:
    from mini_ork.context import run_context_scope
    from mini_ork.dispatch import providers
    from mini_ork.dispatch.models import DispatchRequest
    from mini_ork.runtime.workspace_session import register_run_session

    where = ws._node.url
    steps: dict[str, dict] = {}
    ws.on_setup_step = lambda rec: steps.__setitem__(rec["step"], rec)

    # 1. reachable (health is unauthenticated)
    try:
        health = ws._json("GET", "/v1/health", retries=1)
    except Exception as exc:  # noqa: BLE001
        report.line(1, "FAIL", f"{where}: {exc} — is `mini-ork node-agent` running and its port "
                               "open to this machine?")
        return 1
    report.line(1, "PASS", f"{where} (node-agent {health.get('version', '?')})")

    # 2. token — an authenticated route; 404 means authenticated, no such session
    try:
        ws._json("GET", f"/v1/sessions/{run_id}", retries=1)
    except Exception as exc:  # noqa: BLE001
        if _http_code(exc) != 404:
            report.line(2, "FAIL", f"{exc} — the node's token does not match ${ws._token_env} here")
            return 1
    report.line(2, "PASS", "bearer token accepted")

    # 3-5. provisioning, reported from the workspace's own setup steps
    try:
        ws.up()
    except Exception as exc:  # noqa: BLE001
        _report_setup(report, steps, exc)
        return 1
    _report_setup(report, steps, None)
    register_run_session(run_id, "remote", ws, env=env)
    state["registered"] = True

    # 6. the toolchain the lanes need, inside the session
    smoke = list(lanes) or (_recipe_lanes(recipe, env) if recipe else [])
    registry = providers._load_providers_registry(env.get("MINI_ORK_ROOT"))
    kinds = {str((registry.get(lane) or {}).get("kind") or "") for lane in smoke}
    tools = sorted({_LANE_KIND_TOOLS[k] for k in kinds if k in _LANE_KIND_TOOLS})
    probe = " && ".join([f"command -v {t} >/dev/null" for t in tools]
                        + [f"{PYTHON} -c 'import mini_ork'"])
    rc, output = ws.exec(probe, cwd="/workspace/target", timeout=60)
    if rc != 0:
        report.line(6, "FAIL", f"`{probe}` rc={rc}: {output.strip()[-300:]} — rebuild the agent "
                               "image (docker/agent-node/build.sh)")
        return 1
    report.line(6, "PASS", ", ".join(tools + ["mini_ork importable"]))

    # 7. per-lane auth smoke through the real dispatch, inside this session
    if no_llm:
        report.line(7, "SKIP", "--no-llm")
    elif not smoke:
        report.line(7, "SKIP", "no lanes to smoke (pass --recipe or --lane)")
    else:
        bound = {k: env[k] for k in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MO_PLACEMENT",
                                     "MO_NODE_ENV") if k in env}
        with run_context_scope(bound):
            for lane in smoke:
                res = providers.dispatch_model(DispatchRequest(
                    model=lane, prompt=AUTH_SMOKE_PROMPT, timeout_s=AUTH_SMOKE_TIMEOUT_S,
                    max_turns=1), env.get("MINI_ORK_ROOT"))
                if not res.ok:
                    error = (res.error or "").strip()
                    timed_out = res.rc == 124 or "timed out" in error.lower() or "timeout" in error.lower()
                    hint = (f"auth: no response in {AUTH_SMOKE_TIMEOUT_S:.0f} s — check the key"
                            if timed_out else f"auth: rc={res.rc} {error[-300:]}".rstrip())
                    report.line(7, "FAIL", f"lane {lane}: {hint}")
                    return 1
        report.line(7, "PASS", f"{len(smoke)} lane(s) answered: {', '.join(smoke)}")

    # 8. the network level the node actually applied
    want = (getattr(ws._profile, "network", "") or "full") if ws._profile is not None else "full"
    got = ws._json("GET", f"/v1/sessions/{run_id}")
    if (got.get("network") or "full") != want:
        report.line(8, "FAIL", f"the profile asks for {want!r}; the session runs "
                               f"{got.get('network')!r}")
        return 1
    report.line(8, "PASS", want if want != "allowlist"
                else f"allowlist ({len(got.get('allow_domains') or [])} domains)")
    return 0


_SETUP_CHECK = {"engine": 3, "image_prepare": 4, "health": 5, "session_up": 5,
                "initial_sync": 5, "post_sync": 5}
_SETUP_HINT = {
    "health": "the node is at max_sessions — wait, raise max_sessions, or end a run",
    "engine": "engine upload failed — is this checkout clean (or MO_REMOTE_ALLOW_DIRTY_ENGINE=1)?",
    "image_prepare": "the profile's setup script failed — see the log tail above",
    "session_up": "the node could not start a session — check `docker info` on the node",
    "initial_sync": "the target tree could not be synced — run from the target checkout or "
                    "set MO_TARGET_CWD",
    "post_sync": "the mo-home upload failed",
}


def _report_setup(report: _Report, steps: Mapping[str, dict], exc: BaseException | None) -> None:
    failed = next((s for s, rec in steps.items() if rec.get("status") == "fail"), None)
    if exc is not None and failed is None:
        report.line(5, "FAIL", str(exc)[:300])
        return
    for idx in (3, 4, 5):
        names = [s for s, i in _SETUP_CHECK.items() if i == idx]
        if failed in names:
            detail = str(steps[failed].get("detail") or exc or "")[:300]
            report.line(idx, "FAIL", f"{failed}: {detail} — {_SETUP_HINT[failed]}")
            return
        if idx == 4 and "image_prepare" not in steps:
            report.line(4, "PASS", "no setup script (base image)")
            continue
        oks = [f"{s} {steps[s].get('ms', 0)}ms" for s in names if steps.get(s, {}).get("status") == "ok"]
        report.line(idx, "PASS", ", ".join(oks) or "ok")


if __name__ == "__main__":
    sys.exit(main())
