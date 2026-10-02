"""remote-nodes-12: environment profiles, the setup-image cache, network levels.

Loader + cache + session argv run with fakes; one daemon-gated test drives the
real setup -> commit -> run cycle that the first cut could never complete (it
ran setup with --rm and then committed the base IMAGE name).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mini_ork.remote.environments import LookalikeSecretError, load_profile
from mini_ork.remote.node_agent.images import ImageCache
from mini_ork.remote.node_agent.sessions import SessionManager

REPO = Path(__file__).resolve().parents[2]


def _homes(tmp_path, template: str, shadow: str | None = None) -> dict:
    root, home = tmp_path / "root", tmp_path / "home"
    (root / "config" / "environments").mkdir(parents=True)
    (home / "config" / "environments").mkdir(parents=True)
    (root / "config" / "environments" / "p.yaml").write_text(template)
    if shadow is not None:
        (home / "config" / "environments" / "p.yaml").write_text(shadow)
    shutil.copytree(REPO / "schemas", root / "schemas")
    return {"MINI_ORK_ROOT": str(root), "MINI_ORK_HOME": str(home)}


TEMPLATE = """\
node: hetzner-1
image: mini-ork/agent-node:latest
setup: |
  apt-get install -y jq
resources: {cpus: 2, memory: 4g}
"""


# --------------------------------------------------------------------------- loader


def test_shadow_overriding_resources_keeps_template_image_and_setup(tmp_path):
    env = _homes(tmp_path, TEMPLATE, shadow="resources: {cpus: 8}\n")
    prof = load_profile("p", env=env)
    assert prof.image == "mini-ork/agent-node:latest"
    assert "apt-get install -y jq" in prof.setup
    assert prof.resources["cpus"] == 8


def test_secret_looking_env_key_is_rejected(tmp_path):
    env = _homes(tmp_path, TEMPLATE + "env: {OPENAI_API_KEY: sk-x}\n")
    with pytest.raises(LookalikeSecretError, match="secrets"):
        load_profile("p", env=env)


def test_unknown_field_fails_schema_validation(tmp_path):
    env = _homes(tmp_path, TEMPLATE + "colour: blue\n")
    with pytest.raises(ValueError, match="schema"):
        load_profile("p", env=env)


# --------------------------------------------------------------------------- image cache


class _FakeDocker:
    """Records docker argv; ``setup_rc`` decides the setup container's fate."""

    def __init__(self, setup_rc=0):
        self.calls: list[list[str]] = []
        self.setup_rc = setup_rc
        self.images: set[str] = set()

    def run(self, argv):
        self.calls.append(list(argv))
        rc, out = 0, ""
        if argv[:2] == ["docker", "commit"]:
            self.images.add(argv[3])
        elif argv[:3] == ["docker", "image", "inspect"]:
            rc = 0 if argv[-1] in self.images else 1
        return subprocess.CompletedProcess(argv, rc, out, "")

    def setup(self, argv, log_path, timeout_s):
        self.calls.append(list(argv))
        log_path.write_text("setting up\n" + ("" if self.setup_rc == 0 else "E: package not found\n"))
        return self.setup_rc


def test_setup_runs_once_commits_the_container_and_then_hits_the_cache(tmp_path):
    fake = _FakeDocker()
    cache = ImageCache(tmp_path, _run_fn=fake.run, _run_setup_fn=fake.setup)
    first = cache.prepare(base_image="debian:bookworm-slim", setup="true", platform="linux/arm64")
    assert first.tag and not first.cached
    run = next(c for c in fake.calls if c[:2] == ["docker", "run"])
    container = run[run.index("--name") + 1]
    assert "--rm" not in run                                    # it must survive to be committed
    assert ["docker", "commit", container, first.tag] in fake.calls
    assert ["docker", "rm", "-f", container] in fake.calls
    setups = sum(1 for c in fake.calls if c[:2] == ["docker", "run"])
    second = cache.prepare(base_image="debian:bookworm-slim", setup="true", platform="linux/arm64")
    assert second.cached and second.tag == first.tag
    assert sum(1 for c in fake.calls if c[:2] == ["docker", "run"]) == setups   # no second setup


def test_failing_setup_leaves_no_tag_returns_the_log_tail_and_cleans_up(tmp_path):
    fake = _FakeDocker(setup_rc=100)
    cache = ImageCache(tmp_path, _run_fn=fake.run, _run_setup_fn=fake.setup)
    res = cache.prepare(base_image="debian:bookworm-slim", setup="apt-get install nope",
                        platform="linux/arm64")
    assert res.tag == "" and not res.cached
    assert "package not found" in res.log_tail
    assert not any(c[:2] == ["docker", "commit"] for c in fake.calls)
    run = next(c for c in fake.calls if c[:2] == ["docker", "run"])
    assert ["docker", "rm", "-f", run[run.index("--name") + 1]] in fake.calls


# --------------------------------------------------------------------------- network + resources


def _session_fake():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        out = "cid-" + str(len(calls)) if argv[:2] == ["docker", "run"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    return calls, run


def test_allowlist_session_gets_an_internal_network_and_a_proxy_and_cleans_both(tmp_path):
    calls, run = _session_fake()
    mgr = SessionManager(tmp_path, runtime="docker", engine_root=tmp_path, _run_fn=run)
    sess = mgr.create(run_id="r", image="alpine:latest", network="allowlist",
                      allow_domains=["pypi.org"], resources={"cpus": 2, "memory": "4g"})
    flat = [" ".join(c) for c in calls]
    assert any(c.startswith("docker network create --internal mo-int-") for c in flat)
    proxy_runs = [c for c in flat if c.startswith("docker run") and "mo-proxy-" in c]
    assert proxy_runs, flat
    session_run = next(c for c in calls if c[:2] == ["docker", "run"] and "mo-sess-" in " ".join(c))
    joined = " ".join(session_run)
    assert "--network mo-int-" in joined and "HTTP_PROXY=" in joined
    assert "--cpus 2" in joined and "--memory 4g" in joined
    assert "--v " not in joined and " -v " in joined     # the mount flag (a '--v' typo broke every session)
    calls.clear()
    mgr.delete(sess.sid)
    flat = [" ".join(c) for c in calls]
    assert any(c.startswith("docker rm -f") and "proxy" not in c for c in flat)
    assert any(c.startswith("docker network rm mo-int-") for c in flat)
    assert sum(1 for c in flat if c.startswith("docker rm -f")) >= 2   # session + proxy


def test_full_network_session_argv_is_unchanged(tmp_path):
    calls, run = _session_fake()
    mgr = SessionManager(tmp_path, runtime="docker", engine_root=tmp_path, _run_fn=run)
    mgr.create(run_id="r2", image="alpine:latest")
    flat = " ".join(" ".join(c) for c in calls)
    assert "--network" not in flat and "network create" not in flat and "HTTP_PROXY" not in flat


# --------------------------------------------------------------------------- live


def _docker_ok() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="docker daemon not available")
def test_live_setup_is_committed_and_runs_from_the_cached_image(tmp_path):
    """The whole cycle on a real daemon: setup writes a file, the committed
    image carries it, and a second prepare is a cache hit."""
    base = "mini-ork/agent-node:latest"
    if subprocess.run(["docker", "image", "inspect", base], capture_output=True).returncode != 0:
        pytest.skip(f"{base} not built (bash docker/agent-node/build.sh)")
    cache = ImageCache(tmp_path)
    arch = subprocess.run(["docker", "version", "--format", "{{.Server.Arch}}"],
                          capture_output=True, text=True).stdout.strip()
    setup = 'echo "prepared-by-setup" > "$HOME/mo-setup-proof"'
    res = cache.prepare(base_image=base, setup=setup, platform=f"linux/{arch}", timeout_s=120)
    try:
        assert res.tag, res.log_tail
        out = subprocess.run(["docker", "run", "--rm", res.tag, "sh", "-c", 'cat "$HOME/mo-setup-proof"'],
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "prepared-by-setup", out.stderr
        again = cache.prepare(base_image=base, setup=setup, platform=f"linux/{arch}")
        assert again.cached and again.tag == res.tag
    finally:
        if res.tag:
            subprocess.run(["docker", "rmi", "-f", res.tag], capture_output=True)
    leftovers = subprocess.run(["docker", "ps", "-aq", "--filter", "name=mo-prep-"],
                               capture_output=True, text=True).stdout.strip()
    assert not leftovers, "setup container left behind"
