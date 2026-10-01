"""Tests for the mini-ork agent-node image contract (remote-nodes epic 04).

The static half parses ``docker/agent-node/Dockerfile`` as text and asserts the
runtime invariants that ``DockerWorkspace`` and the node-agent depend on. It
runs everywhere, no daemon required, so it gates the default CI lane.

The daemon-gated half runs ``build.sh`` (single-arch, host platform) and then
``smoke.sh`` against the built image with the local checkout mounted as the
engine. It mirrors ``tests/unit/test_docker_workspace.py``'s colima-aware
gating (``@requires_docker``, ``bind_drive_root``) and skips cleanly when
docker is unavailable.

Lint invariant: this file must not require an editable install; pytest's
``pythonpath = ["."]`` (pyproject.toml:74) covers ``import mini_ork``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO_ROOT / "docker" / "agent-node" / "Dockerfile"
SMOKE_SH = REPO_ROOT / "docker" / "agent-node" / "smoke.sh"
BUILD_SH = REPO_ROOT / "docker" / "agent-node" / "build.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Use the same default test image as tests/unit/test_docker_workspace.py:27 so
# a `docker pull alpine:latest` works on bare CI without baking our own.
TEST_IMAGE = os.environ.get("MO_SANDBOX_TEST_IMAGE", "alpine:latest")


# ---------------------------------------------------------------------------
# helpers (copied from test_docker_workspace.py to keep this epic's file
# surface self-contained — test_docker_workspace's _* helpers are private
# and the kickoff's "Files in scope" does not include test_docker_workspace)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        r = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


def _image_ready(image: str) -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    if (
        subprocess.run(
            [exe, "image", "inspect", image], capture_output=True, text=True
        ).returncode
        == 0
    ):
        return True
    try:
        return (
            subprocess.run(
                [exe, "pull", image],
                capture_output=True,
                text=True,
                timeout=180,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _bind_visible_dir(base: str) -> str | None:
    exe = shutil.which("docker")
    if not exe:
        return None
    try:
        probe = tempfile.mkdtemp(prefix=".mo-bindprobe-", dir=base)
    except OSError:
        return None
    try:
        r = subprocess.run(
            [
                exe,
                "run",
                "--rm",
                "-v",
                f"{probe}:/probe",
                TEST_IMAGE,
                "sh",
                "-c",
                "echo ok > /probe/sentinel",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode == 0 and os.path.exists(os.path.join(probe, "sentinel")):
            return probe
    except Exception:
        pass
    shutil.rmtree(probe, ignore_errors=True)
    return None


_DOCKER = _docker_available()
requires_docker = pytest.mark.skipif(
    not _DOCKER, reason="docker daemon not available"
)


@pytest.fixture
def bind_drive_root(tmp_path):
    if not _image_ready(TEST_IMAGE):
        pytest.skip(f"test image {TEST_IMAGE} unavailable/unpullable")
    chosen: str | None = None
    for base in (str(tmp_path), os.path.expanduser("~")):
        chosen = _bind_visible_dir(base)
        if chosen:
            break
    if not chosen:
        pytest.skip("no docker-bind-visible directory (colima shares neither tmp nor $HOME)")
    try:
        yield chosen
    finally:
        shutil.rmtree(chosen, ignore_errors=True)


# ---------------------------------------------------------------------------
# Dockerfile parsing (used by the static tests below)
# ---------------------------------------------------------------------------


def _read_dockerfile() -> str:
    if not DOCKERFILE.is_file():
        pytest.fail(f"Dockerfile missing at {DOCKERFILE}")
    return DOCKERFILE.read_text(encoding="utf-8")


def _normalize_dockerfile(text: str) -> str:
    """Collapse shell line-continuations (``\\<newline><indent>``) to single
    spaces so ``RUN`` and ``ENV`` directives that wrap across multiple lines
    parse as one logical line."""
    return re.sub(r"\\\s*\n\s*", " ", text)


def _dockerfile_user_block(text: str) -> str:
    """Return the last ``USER`` directive's argument."""
    users = re.findall(r"^\s*USER\s+(\S+)", text, flags=re.MULTILINE)
    if not users:
        return ""
    return users[-1]


def _dockerfile_env_value(text: str, name: str) -> str | None:
    """Return the value of the LAST ``ENV <name>=<value>`` directive.

    Handles:
      - ``ENV <name>=<value>`` (single line)
      - ``ENV <name> <value>`` (legacy space form, deprecated but valid)
      - ``ENV A=1 \\\n     B=2 \\\n     C=3`` (multi-line continuation, normalized
        to a single line before parsing)

    Returns None when the key is unset.
    """
    normalized = _normalize_dockerfile(text)
    # Walk every ENV line and pick out the LAST occurrence of `name`.
    last_value: str | None = None
    for m in re.finditer(r"\bENV\s+([^\n]+)", normalized):
        env_body = m.group(1)
        for token in env_body.split():
            # `NAME=VALUE` form.
            if token.startswith(f"{name}="):
                last_value = token[len(name) + 1:]
                continue
            # `NAME VALUE` form (legacy).
            if token == name:
                idx = env_body.split().index(token)
                parts = env_body.split()
                if idx + 1 < len(parts):
                    last_value = parts[idx + 1]
    return last_value


def _dockerfile_arg_names(text: str) -> list[str]:
    """All ``ARG <name>[=<default>]`` names declared at the top scope."""
    return re.findall(r"^\s*ARG\s+([A-Z_][A-Z0-9_]*)\s*(?:=|\s|$)", text, flags=re.MULTILINE)


def _dockerfile_entrypoint(text: str) -> str:
    m = re.search(r"^\s*ENTRYPOINT\s+(.+)$", text, flags=re.MULTILINE)
    return m.group(1).strip() if m else ""


def _dockerfile_cmd(text: str) -> str:
    m = re.search(r"^\s*CMD\s+(.+)$", text, flags=re.MULTILINE)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Static tests (always run; no docker required)
# ---------------------------------------------------------------------------


def test_dockerfile_exists():
    assert DOCKERFILE.is_file(), f"Dockerfile must exist at {DOCKERFILE}"


def test_smoke_sh_exists_and_is_bash_clean():
    assert SMOKE_SH.is_file(), f"smoke.sh must exist at {SMOKE_SH}"
    # bash -n is the static-check verifier's own gate; mirror it here so the
    # unit test catches a syntax error before the recipe verifier does.
    r = subprocess.run(["bash", "-n", str(SMOKE_SH)], capture_output=True, text=True)
    assert r.returncode == 0, f"smoke.sh failed bash -n: {r.stderr}"


def test_build_sh_exists_and_is_bash_clean():
    assert BUILD_SH.is_file(), f"build.sh must exist at {BUILD_SH}"
    r = subprocess.run(["bash", "-n", str(BUILD_SH)], capture_output=True, text=True)
    assert r.returncode == 0, f"build.sh failed bash -n: {r.stderr}"


def test_dockerfile_uses_non_root_user():
    text = _read_dockerfile()
    user_block = _dockerfile_user_block(text)
    assert user_block, "Dockerfile must end with a USER directive"
    assert user_block != "root", (
        f"Dockerfile must not run as root (claude refuses bypassPermissions); got USER {user_block!r}"
    )
    # The kickoff specifies uid 1000 specifically. We accept any uid != 0 but
    # require the agent user be created in the same Dockerfile (a `groupadd`
    # + `useradd` pair, possibly inside a RUN) so the contract is reproducible.
    normalized = _normalize_dockerfile(text)
    assert re.search(r"\bgroupadd\b", normalized), (
        "Dockerfile must create the agent group (groupadd)"
    )
    assert re.search(r"\buseradd\b", normalized), (
        "Dockerfile must create the agent user (useradd)"
    )


def test_dockerfile_home_is_workspace_home():
    text = _read_dockerfile()
    home = _dockerfile_env_value(text, "HOME")
    assert home == "/workspace/home", (
        f"Dockerfile must set HOME=/workspace/home so the agent CLI caches land on the per-run home mount; got HOME={home!r}"
    )


def test_dockerfile_workdir_is_workspace_target():
    text = _read_dockerfile()
    m = re.search(r"^\s*WORKDIR\s+(\S+)", text, flags=re.MULTILINE)
    assert m, "Dockerfile must set WORKDIR"
    assert m.group(1) == "/workspace/target", (
        f"Dockerfile must set WORKDIR=/workspace/target (D4 row 1); got WORKDIR={m.group(1)!r}"
    )


def test_dockerfile_pythonpath_points_at_mounted_engine():
    text = _read_dockerfile()
    pypath = _dockerfile_env_value(text, "PYTHONPATH")
    assert pypath == "/opt/mini-ork", (
        f"Dockerfile must set PYTHONPATH=/opt/mini-ork (the engine mount path); got PYTHONPATH={pypath!r}"
    )
    # MO_REMOTE_NODE=1 is the security posture flag; load-bearing, must be in
    # the image ENV.
    remote = _dockerfile_env_value(text, "MO_REMOTE_NODE")
    assert remote == "1", (
        f"Dockerfile must set MO_REMOTE_NODE=1 (security posture); got MO_REMOTE_NODE={remote!r}"
    )


def test_dockerfile_declares_required_build_args():
    text = _read_dockerfile()
    args = set(_dockerfile_arg_names(text))
    required = {"CLAUDE_CODE_VERSION", "OPENCODE_VERSION", "WITH_CODEX"}
    missing = required - args
    assert not missing, f"Dockerfile must declare ARGs {sorted(required)}; missing {sorted(missing)}"


def test_dockerfile_entrypoint_and_cmd_match_runtime_contract():
    text = _read_dockerfile()
    entrypoint = _dockerfile_entrypoint(text)
    cmd = _dockerfile_cmd(text)
    # The kickoff calls for `tini` as the init wrapper and `sleep infinity`
    # as the long-lived default. `docker run` with no command then runs
    # `tini -- sleep infinity`, keeping the container alive for the
    # node-agent to exec into.
    assert "tini" in entrypoint, (
        f"Dockerfile ENTRYPOINT must invoke tini (PID-1 reaper); got ENTRYPOINT={entrypoint!r}"
    )
    assert "sleep" in cmd and "infinity" in cmd, (
        f"Dockerfile CMD must be `sleep infinity` (long-lived default); got CMD={cmd!r}"
    )


def test_dockerfile_installs_real_base_packages():
    """Verifier-only-credit trap. A Dockerfile that compiles but installs no
    packages is a runtime no-op. Require concrete package names so a hollow
    Dockerfile fails this gate."""
    text = _read_dockerfile()
    required_packages = {"git", "curl", "jq", "ripgrep", "tini"}
    missing = sorted(pkg for pkg in required_packages if not re.search(
        rf"\b{re.escape(pkg)}\b", text
    ))
    assert not missing, (
        f"Dockerfile must install the base packages {sorted(required_packages)}; missing {missing}"
    )


def test_dockerfile_installs_pyproject_base_dependencies():
    text = _read_dockerfile()
    assert "PyYAML" in text, "Dockerfile must install PyYAML (pyproject.toml base dep)"
    assert "jsonschema" in text, "Dockerfile must install jsonschema (pyproject.toml base dep)"
    # Cross-check: pyproject.toml actually lists these as base deps (not in
    # some optional [full] extra), so the assertion is grounded.
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    assert re.search(r"dependencies\s*=\s*\[[^\]]*PyYAML", pyproject_text), (
        "pyproject.toml must list PyYAML in [project].dependencies (test drift)"
    )
    assert re.search(r"dependencies\s*=\s*\[[^\]]*jsonschema", pyproject_text), (
        "pyproject.toml must list jsonschema in [project].dependencies (test drift)"
    )


def test_dockerfile_pins_node_major():
    text = _read_dockerfile()
    assert re.search(r"nodesource\.com/setup_\$\{?NODE_MAJOR\}?\.x", text), (
        "Dockerfile must install Node LTS via NodeSource using the NODE_MAJOR ARG"
    )


def test_smoke_sh_fail_messages_are_clear():
    """Exercise the early-bail branches of smoke.sh without docker: the
    `engine_dir missing` and `pyproject.toml missing` paths. These are the
    failure modes the kickoff's acceptance criterion calls out explicitly."""
    # Missing engine_dir
    r = subprocess.run(
        [str(SMOKE_SH), "alpine:latest", "/no/such/path/__nope__"],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, "smoke.sh must exit non-zero when engine_dir is missing"
    assert "engine_dir missing" in r.stderr, (
        f"smoke.sh must print 'engine_dir missing' on the missing-dir path; stderr={r.stderr!r}"
    )

    # Engine dir exists but is missing pyproject.toml
    with tempfile.TemporaryDirectory(prefix="smoke-no-pyproject-") as empty:
        r = subprocess.run(
            [str(SMOKE_SH), "alpine:latest", empty],
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, "smoke.sh must exit non-zero when pyproject.toml is missing"
        assert "pyproject.toml" in r.stderr, (
            f"smoke.sh must mention pyproject.toml on the bad-engine path; stderr={r.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Daemon-gated tests (skip cleanly without docker + bind-visible dir)
# ---------------------------------------------------------------------------


@requires_docker
def test_build_and_smoke_single_arch(bind_drive_root):
    """Build the image single-arch on the host platform, then run smoke.sh
    against it with the engine mounted from the test's bind-visible dir."""
    if not _image_ready("alpine:latest"):
        pytest.skip("alpine:latest unavailable for setup probe")
    # The build dir is the repo root — we need `pyproject.toml` and the
    # `docker/agent-node/` tree side-by-side. bind_drive_root is already a
    # bind-visible dir; symlink the relevant tree into it so `docker build`
    # can read both files.
    staging = Path(bind_drive_root) / "engine"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "pyproject.toml").write_text(
        PYPROJECT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # Symlink the docker/agent-node tree into the staging dir.
    agent_node_link = staging / "docker"
    if not agent_node_link.exists():
        os.symlink(REPO_ROOT / "docker", agent_node_link)

    # Build (host platform; do not push; --load into the local daemon).
    r = subprocess.run(
        [
            "bash",
            str(BUILD_SH),
            "--load",
        ],
        capture_output=True,
        text=True,
        cwd=staging,
        timeout=900,
    )
    assert r.returncode == 0, (
        f"build.sh failed: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    )

    # Run smoke.sh against the just-built :latest, with the staging dir as
    # the engine.
    r = subprocess.run(
        [str(SMOKE_SH), "mini-ork/agent-node:latest", str(staging)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0, (
        f"smoke.sh failed: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    )
    assert "all checks passed" in r.stdout, (
        f"smoke.sh did not print success line; stdout={r.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Internal sanity: the parser helpers above should not silently accept empty
# inputs. This is a developer guard — if these fail, the *real* static tests
# above are also broken in a way that needs an immediate fix.
# ---------------------------------------------------------------------------


class _ParserHelperSanity(unittest.TestCase):
    def test_user_block_last_wins(self):
        text = textwrap.dedent(
            """\
            USER root
            RUN something
            USER agent
            """
        )
        assert _dockerfile_user_block(text) == "agent"

    def test_env_value_eq_form(self):
        text = "ENV FOO=bar\nENV FOO=baz\n"
        assert _dockerfile_env_value(text, "FOO") == "baz"

    def test_env_value_space_form(self):
        text = "ENV FOO bar\n"
        assert _dockerfile_env_value(text, "FOO") == "bar"


def test_parser_helper_sanity():
    """Run the unittest.TestCase above so pytest reports its assertions."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(_ParserHelperSanity)
    runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
    result = runner.run(suite)
    assert result.wasSuccessful(), "parser helper sanity failed"