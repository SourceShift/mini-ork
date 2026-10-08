"""Tests for ``mini_ork.verify.test_env`` and the verifier call sites that
consume it.

Four groups, in priority order:

1. ``scrubbed_test_env`` drops every denylisted name and suffix/prefix
   match, keeps ``PATH`` / ``HOME`` / ``MINI_ORK_ROOT`` /
   ``MINI_ORK_TEST_CMD`` / ``MO_DAILY_BUDGET_USD`` / ``PYTHONPATH``, and
   never mutates its input. This is the contract — get it right and the
   call-site tests below are mostly belt-and-suspenders.

2. ``mini_ork.certify.oracle.replay_check`` passes a scrubbed env to
   ``subprocess.run``. We monkeypatch ``subprocess.run`` in the oracle
   module and assert the captured ``env`` kwarg excludes the denylist.

3. ``recipes/framework-edit/verifiers/test.py`` invokes pytest via
   ``_web_smoke`` with the same scrub. We monkeypatch ``subprocess.run``
   and call ``_web_smoke`` directly.

4. ``recipes/recursive-self-improve/verifiers/self-tests-pass.py`` builds
   its ``child_env`` via the same scrub and points ``MINI_ORK_HOME`` at
   a sandbox-owned dir. The module's main path is heavy (mkdtemp +
   symlink + subprocess.run), so the test runs it under a stub and
   asserts the captured env.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
FW_VERIFIER_DIR = REPO_ROOT / "recipes" / "framework-edit" / "verifiers"
RSI_VERIFIER_DIR = REPO_ROOT / "recipes" / "recursive-self-improve" / "verifiers"

from mini_ork.verify.test_env import scrubbed_test_env  # noqa: E402


# ── 1. Unit tests for scrubbed_test_env ──────────────────────────────────────


def test_scrubbed_test_env_drops_mini_ork_state_pointers():
    src = {
        "MINI_ORK_SECRETS": "/tmp/secrets",
        "MINI_ORK_DB": "/tmp/state.db",
        "MINI_ORK_HOME": "/home/operator/.mini-ork",
        "MINI_ORK_PROJECT_HOME": "/tmp/proj",
        "MINI_ORK_RUN_ID": "abc-123",
        "MINI_ORK_RUN_DIR": "/tmp/run",
        "MINI_ORK_PLAN_PATH": "/tmp/plan.json",
        "MINI_ORK_AGENTS": "/tmp/agents.yaml",
        # The operator's target-repo pointer: a test wrapper that re-cds to it
        # would escape the cwd the oracle set and re-run the candidate on the
        # base side (the ask-k2 delta-gate false negative).
        "MO_TARGET_CWD": "/tmp/target-repo",
    }
    out = scrubbed_test_env(src)
    for k in src:
        assert k not in out, f"{k} should have been scrubbed"


def test_scrubbed_test_env_drops_credential_suffixes():
    src = {
        "MINIMAX_API_KEY": "x",
        "GLM_API_KEY": "y",
        "KIMI_API_KEY": "z",
        "GATEWAY_AUTH_TOKEN": "a",
        "GH_ACCESS_TOKEN": "b",
        "DB_SECRET": "c",
        "OAUTH_SECRET_KEY": "d",
    }
    out = scrubbed_test_env(src)
    assert out == {}


def test_scrubbed_test_env_drops_anthropic_prefix():
    src = {
        "ANTHROPIC_API_KEY": "x",
        "ANTHROPIC_BASE_URL": "https://example",
        "ANTHROPIC_CUSTOM_HEADER": "h",
    }
    out = scrubbed_test_env(src)
    assert out == {}


def test_scrubbed_test_env_drops_openai_base_urls():
    src = {
        "OPENAI_API_BASE": "https://mirror",
        "OPENAI_BASE_URL": "https://mirror",
    }
    out = scrubbed_test_env(src)
    assert out == {}


def test_scrubbed_test_env_preserves_mini_ork_root_and_test_cmd():
    src = {
        "MINI_ORK_ROOT": "/repo",
        "MINI_ORK_TEST_CMD": "python3.11 -m pytest -q",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/op",
        "PYTHONPATH": ".",
        "MO_DAILY_BUDGET_USD": "2000",
        "MO_CHEAP_LANE": "minimax",
    }
    out = scrubbed_test_env(src)
    assert out == src


def test_scrubbed_test_env_does_not_mutate_input():
    src = {
        "MINI_ORK_SECRETS": "/tmp/secrets",
        "PATH": "/usr/bin",
        "MINIMAX_API_KEY": "x",
    }
    snapshot = dict(src)
    scrubbed_test_env(src)
    assert src == snapshot


def test_scrubbed_test_env_default_is_os_environ(monkeypatch):
    monkeypatch.setenv("MINI_ORK_SECRETS", "/tmp/secrets")
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("PATH", "/usr/bin")
    out = scrubbed_test_env()
    assert "MINI_ORK_SECRETS" not in out
    assert "MINIMAX_API_KEY" not in out
    assert out.get("PATH") == "/usr/bin"


def test_scrubbed_test_env_returns_a_new_dict():
    src = {"PATH": "/usr/bin"}
    out = scrubbed_test_env(src)
    assert out is not src
    assert isinstance(out, dict)


def test_scrubbed_test_env_handles_empty_input():
    assert scrubbed_test_env({}) == {}


# ── 2. replay_check passes a scrubbed env to subprocess.run ──────────────────


def _make_pytest_output(passed_ids=(), failed_ids=()) -> bytes:
    """Synthesize the pytest -v output replay_check's parser expects."""
    lines = []
    for tid in passed_ids:
        lines.append(f"tests/test_mod.py::{tid} PASSED                              [ 50%]")
    for tid in failed_ids:
        lines.append(f"tests/test_mod.py::{tid} FAILED                              [ 50%]")
    return ("\n".join(lines) + "\n").encode()


def test_replay_check_passes_scrubbed_env(monkeypatch, tmp_path):
    """replay_check._run must pass ``env=scrubbed_test_env()`` so the
    child pytest does not see operator secrets or live mini-ork state."""
    from mini_ork.certify import oracle

    base_cwd = tmp_path / "base"
    base_cwd.mkdir()
    cand_cwd = tmp_path / "cand"
    cand_cwd.mkdir()

    monkeypatch.setenv("MINI_ORK_SECRETS", "/tmp/fake-secrets")
    monkeypatch.setenv("MINI_ORK_DB", "/tmp/fake-db.sqlite")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "fake-run-id")
    monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-12345")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")
    monkeypatch.setenv("OPENAI_API_BASE", "https://mirror")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MINI_ORK_ROOT", "/repo")

    captured_envs: list[dict | None] = []

    def fake_run(*args, **kwargs):
        captured_envs.append(kwargs.get("env"))
        # Emit pytest output so replay_check parses and reaches an outcome.
        log_path = kwargs.get("stdout")
        if log_path is not None and hasattr(log_path, "write"):
            log_path.write(_make_pytest_output(passed_ids=("t1",), failed_ids=("t2",)))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(oracle.subprocess, "run", fake_run)

    # Avoid actually opening the log files; replay_check defaults them to
    # base_cwd/.replay_*.log but our fake_run ignores them.
    oracle.replay_check(
        "pytest -v tests/test_mod.py",
        base_cwd=str(base_cwd),
        candidate_cwd=str(cand_cwd),
    )
    assert captured_envs, "subprocess.run was not called"
    for env in captured_envs:
        assert env is not None, "env kwarg was not passed"
        assert "MINI_ORK_SECRETS" not in env
        assert "MINI_ORK_DB" not in env
        assert "MINI_ORK_RUN_ID" not in env
        assert "MINIMAX_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_BASE" not in env
        # Non-secret vars preserved
        assert env.get("PATH") == "/usr/bin"
        assert env.get("MINI_ORK_ROOT") == "/repo"


# ── 3. framework-edit/verifiers/test.py:_web_smoke scrubs the env ───────────


def _load_fwedit_verifier(monkeypatch, tmp_path):
    """Import the framework-edit test verifier with a stub subprocess.run.

    Returns the loaded module so the caller can invoke ``_web_smoke`` and
    inspect the captured calls. Sets ``MINI_ORK_RUN_DIR`` to ``tmp_path``,
    pre-creates the worktree layout, and patches ``subprocess.run`` so
    the module-level checks and the ``_web_smoke`` invocation all run
    under our fake.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    work_parent = run_dir / "verifier-test-work"
    worktree = work_parent / "repo"
    worktree.mkdir(parents=True)
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_web_smoke.py").write_text("# stub\n")

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_SECRETS", "/tmp/fake-secrets")
    monkeypatch.setenv("MINI_ORK_DB", "/tmp/fake-db.sqlite")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "fake-run-id")
    monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-12345")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")
    monkeypatch.setenv("PATH", "/usr/bin")

    # Add the verifier dir to sys.path so `from _verdict_merge import write_verdict`
    # resolves, and `import test` picks up this file (not pytest's test plugin).
    sys.path.insert(0, str(FW_VERIFIER_DIR))

    captured: list[dict] = []

    def fake_run(argv, **_kwargs):
        captured.append({"argv": argv, "env": _kwargs.get("env"), "kwargs": _kwargs})
        if isinstance(argv, list):
            if "rev-parse" in argv:
                return SimpleNamespace(
                    returncode=0,
                    stdout=str(worktree.resolve()).encode() + b"\n",
                )
            if "archive" in argv:
                # Empty tar — extractall on empty is a no-op.
                return SimpleNamespace(returncode=0, stdout=b"")
        # All other invocations succeed silently (git init, git apply, etc.).
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    spec = importlib.util.spec_from_file_location(
        "_fwedit_test_verifier", FW_VERIFIER_DIR / "test.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, captured, worktree


# ── 4. recursive-self-improve/verifiers/self-tests-pass.py scrubs the env ────


# ── 5. Source-level checks: the recipe verifiers wire the new scrub ──────────


def test_fwedit_verifier_imports_scrubbed_test_env():
    """Defensive lazy-import + local fallback must be present so a verifier
    copied into a fixture without the rest of mini_ork still loads."""
    src = (FW_VERIFIER_DIR / "test.py").read_text()
    assert "from mini_ork.verify.test_env import scrubbed_test_env" in src
    assert "def scrubbed_test_env(environ=None):" in src  # fallback body


def test_oracle_imports_scrubbed_test_env():
    src = (REPO_ROOT / "mini_ork" / "certify" / "oracle.py").read_text()
    assert "from mini_ork.verify.test_env import scrubbed_test_env" in src
    assert "env=scrubbed_test_env()" in src


def test_fwedit_web_smoke_passes_scrubbed_env_source():
    """The web-smoke run builds its env from scrubbed_test_env() (importing the
    verifier script would execute it, so this checks the source)."""
    src = (REPO_ROOT / "recipes" / "framework-edit" / "verifiers" / "test.py").read_text()
    assert "env = scrubbed_test_env()" in src


def test_rsi_verifier_keeps_its_allowlist_env():
    """self-tests-pass.py builds a stricter allowlist env of its own; it must
    keep doing so (an allowlist never passes MINI_ORK_SECRETS or a key)."""
    src = (REPO_ROOT / "recipes" / "recursive-self-improve" / "verifiers" / "self-tests-pass.py").read_text()
    assert "ENV_KEEP" in src
    for name in ("MINI_ORK_SECRETS", "MINI_ORK_DB", "MINIMAX_API_KEY"):
        assert f'"{name}"' not in src.split("ENV_KEEP")[1].split("}")[0]
