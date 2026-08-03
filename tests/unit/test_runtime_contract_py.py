"""Tests for mini_ork.runtime.contract — the native mo_runtime_exec port (WS7).

Three layers:

1. Unit semantics of the native local exec (cwd pinning, rc propagation,
   merged stderr, pgid-kill timeout, env_kv, backend resolution).
2. Integration semantics: native ``mo_runtime_exec`` exercises its normal
   process-management paths, including timeouts and merged stderr.
3. Pin: the minimal agent routes through the native port and no
   ``lib/runtime/*.sh`` is invoked anywhere on that path.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.runtime.contract import exec_local, mo_runtime_exec  # noqa: E402

# ── 1. native semantics ──────────────────────────────────────────────────────

def test_echo_stdout_rc0():
    out, rc = exec_local("echo hello")
    assert out == "hello\n"
    assert rc == 0


def test_rc_propagates_and_stderr_merges():
    out, rc = exec_local("echo oops >&2; exit 3")
    assert rc == 3
    assert "oops" in out  # bash contract redirects 2>&1 into the output


def test_cwd_pinned_inside_child(tmp_path):
    out, rc = exec_local("pwd", cwd=str(tmp_path))
    assert rc == 0
    assert out.strip() == str(tmp_path)


def test_cwd_missing_fails_126(tmp_path):
    missing = str(tmp_path / "nope")
    out, rc = exec_local("true", cwd=missing)
    assert rc == 126
    assert "cd failed" in out


def test_empty_cwd_inherits():
    out, rc = exec_local("pwd")
    assert rc == 0
    assert out.strip() == str(Path.cwd())


def test_timeout_kills_group_rc124():
    # Spawn a detached grandchild sleep: only a group kill reaps it.
    out, rc = exec_local("sleep 30 & sleep 30", timeout=1)
    assert rc == 124


def test_timeout_zero_waits_forever():
    out, rc = exec_local("echo done", timeout=0)
    assert (out, rc) == ("done\n", 0)


def test_env_kv_reaches_child():
    out, rc = exec_local("echo $MO_TEST_KV", env_kv=("MO_TEST_KV=abc123",))
    assert (out, rc) == ("abc123\n", 0)


def test_backend_default_and_local(monkeypatch):
    monkeypatch.delenv("MO_RUNTIME_BACKEND", raising=False)
    assert mo_runtime_exec("echo x")[0] == "x\n"
    monkeypatch.setenv("MO_RUNTIME_BACKEND", "local")
    assert mo_runtime_exec("echo x")[0] == "x\n"


def test_opt_in_backends_degrade_to_local_with_warn(monkeypatch, capsys):
    # bubblewrap/docker are bash-only; the native port mirrors their own
    # "prerequisites missing → WARN + local" fallback instead of failing.
    for name in ("bubblewrap", "docker"):
        monkeypatch.setenv("MO_RUNTIME_BACKEND", name)
        out, rc = mo_runtime_exec("echo fallback")
        assert (out, rc) == ("fallback\n", 0)
        assert name in capsys.readouterr().err


def test_unknown_backend_fails_loudly(monkeypatch):
    monkeypatch.setenv("MO_RUNTIME_BACKEND", "bogus")
    out, rc = mo_runtime_exec("true")
    assert rc == 2
    assert "unknown backend" in out


# ── 2. pin: minimal agent uses the native path, no lib/runtime/*.sh ──────────

def test_minimal_agent_routes_through_native_contract(monkeypatch, tmp_path):
    calls: list[tuple] = []

    def _spy(cmd, cwd="", timeout=0, env_kv=(), backend=None):
        calls.append((cmd, cwd, timeout))
        return ("spy-ok\n", 0)

    # Default workspace is LocalWorkspace; its .exec delegates to
    # mini_ork.runtime.sandbox.mo_runtime_exec (the new import location).
    monkeypatch.setattr("mini_ork.runtime.sandbox.mo_runtime_exec", _spy)
    from mini_ork.agent.minimal import MinimalAgent

    agent = MinimalAgent(cwd=str(tmp_path), timeout=17)
    out, rc = agent._run_bash("echo hi")
    assert (out, rc) == ("spy-ok\n", 0)
    assert calls == [("echo hi", str(tmp_path), 17)]


def test_minimal_agent_invokes_no_bash_runtime_lib(monkeypatch, tmp_path):
    """A full agent turn must not spawn anything touching lib/runtime/*.sh."""
    real_popen = subprocess.Popen
    spawned: list[list] = []

    def _watching_popen(argv, *a, **kw):
        spawned.append(list(argv))
        return real_popen(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _watching_popen)
    monkeypatch.setattr(
        "mini_ork.agent.minimal.dispatch_model",
        lambda req: "```bash\necho pinned > pin.txt\n```",
    )
    from mini_ork.agent.minimal import MinimalAgent

    result = MinimalAgent(cwd=str(tmp_path), max_turns=1).run("pin the path")
    assert (tmp_path / "pin.txt").read_text().strip() == "pinned"
    assert result.turns == 1
    flat = " ".join(" ".join(map(str, argv)) for argv in spawned)
    assert "lib/runtime" not in flat
    assert "contract.sh" not in flat
    assert "mo_runtime_exec" not in flat  # no bash function dispatch


def test_minimal_py_source_has_no_bash_contract_reference():
    src = (REPO / "mini_ork" / "agent" / "minimal.py").read_text()
    assert "lib/runtime" not in src
    assert "contract.sh" not in src
    assert "MINI_ORK_ROOT" not in src
