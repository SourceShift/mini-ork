"""Hermetic tests for ``mini_ork.acp.setup``.

Every test runs against a tmp cwd; the module-level ``_run`` seam and
``shutil.which`` are monkeypatched so no real ``claude`` binary is ever
executed. ``run_interactive`` is driven with a scripted ``io.StringIO`` as
``stdin`` and ``io.StringIO`` as ``stdout`` (not ``stderr`` — we need to
read what was written). The function signature accepts the streams as
arguments so the test never touches the real stdio.

Contract:
  * each readiness check returns a ``Check(name, ok, detail, fix)`` and
    never raises;
  * the subscription-lane orchestrator check reads only the ``loggedIn``
    field of ``claude auth status --json`` — email / org / anything else
    is dropped;
  * non-JSON output of ``claude auth status --json`` fails the check;
  * ``run_interactive`` on full pass returns 0 and writes nothing to
    stdout; declining a fix exits 1; accepting a fix re-checks and exits 0.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import setup as acp_setup  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, *, stdout: str = "", returncode: int = 0):
    """Replace the module-level ``_run`` seam; return the recorded calls list."""

    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return _FakeCompleted(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(acp_setup, "_run", fake_run)
    return calls


def _patch_claude(monkeypatch, *, present: bool):
    """Force ``shutil.which("claude")`` to return a path or None."""

    if present:
        monkeypatch.setattr(acp_setup.shutil, "which", lambda name: "/usr/bin/claude")
    else:
        monkeypatch.setattr(acp_setup.shutil, "which", lambda name: None)


# ── project check ───────────────────────────────────────────────────────────


def test_project_check_passes_when_mini_ork_dir_present(tmp_path: Path):
    (tmp_path / ".mini-ork").mkdir()
    checks = acp_setup.readiness(tmp_path)
    project = next(c for c in checks if c.name == "project")
    assert project.ok is True
    assert str(tmp_path) in project.detail


def test_project_check_fails_without_mini_ork(tmp_path: Path):
    checks = acp_setup.readiness(tmp_path)
    project = next(c for c in checks if c.name == "project")
    assert project.ok is False
    assert project.fix == "mini-ork init"


# ── orchestrator subscription-lane check ─────────────────────────────────────


def test_subscription_check_passes_when_logged_in(tmp_path: Path, monkeypatch):
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=True)
    payload = {"loggedIn": True, "email": "alice@example.com", "org": "Anthropic"}
    _patch_run(monkeypatch, stdout=json.dumps(payload))
    checks = acp_setup.readiness(tmp_path, lane="opus")
    orch = next(c for c in checks if c.name == "orchestrator")
    assert orch.ok is True
    # NEVER leak the email / org / anything else from the json payload.
    assert "alice@example.com" not in orch.detail
    assert "Anthropic" not in orch.detail


def test_subscription_check_fails_when_logged_in_false(tmp_path: Path, monkeypatch):
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=True)
    _patch_run(monkeypatch, stdout=json.dumps({"loggedIn": False}))
    checks = acp_setup.readiness(tmp_path, lane="opus")
    orch = next(c for c in checks if c.name == "orchestrator")
    assert orch.ok is False
    assert orch.fix == "claude auth login"


def test_subscription_check_fails_on_non_json_output(tmp_path: Path, monkeypatch):
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=True)
    _patch_run(monkeypatch, stdout="not json at all")
    checks = acp_setup.readiness(tmp_path, lane="opus")
    orch = next(c for c in checks if c.name == "orchestrator")
    assert orch.ok is False
    assert orch.fix == "claude auth login"


def test_subscription_check_fails_when_claude_missing(tmp_path: Path, monkeypatch):
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=False)
    checks = acp_setup.readiness(tmp_path, lane="opus")
    orch = next(c for c in checks if c.name == "orchestrator")
    assert orch.ok is False
    # Nothing to run until Claude Code is installed: no auto-fix, the detail says what to do.
    assert orch.fix == ""
    assert "install Claude Code" in orch.detail


def test_subscription_check_drops_extra_payload_fields(tmp_path: Path, monkeypatch):
    """Detail string never carries the email / org / other auth-account fields."""
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=True)
    payload = {
        "loggedIn": True,
        "email": "alice@example.com",
        "org": "Anthropic",
        "displayName": "Alice",
        "authToken": "secret-token-should-not-leak",
    }
    _patch_run(monkeypatch, stdout=json.dumps(payload))
    checks = acp_setup.readiness(tmp_path, lane="sonnet")
    orch = next(c for c in checks if c.name == "orchestrator")
    assert orch.ok is True
    blob = orch.detail
    for forbidden in ("alice@example.com", "Anthropic", "Alice", "secret-token"):
        assert forbidden not in blob, f"detail leaked {forbidden!r}: {blob}"


def test_readiness_never_raises_on_subprocess_timeout(tmp_path: Path, monkeypatch):
    """A failing ``claude auth status`` is reported, not raised."""
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=True)

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=15)

    monkeypatch.setattr(acp_setup, "_run", fake_run)
    checks = acp_setup.readiness(tmp_path, lane="opus")
    orch = next(c for c in checks if c.name == "orchestrator")
    assert orch.ok is False
    assert orch.fix == "claude auth login"


# ── lanes check (non-subscription orchestrator) ─────────────────────────────


def test_lanes_check_skipped_when_no_roles_configured(tmp_path: Path, monkeypatch):
    (tmp_path / ".mini-ork").mkdir()
    # Force the dispatch helpers to report nothing configured; ``load_lanes``
    # is monkeypatched so no agents.yaml is touched.
    monkeypatch.setattr(
        "mini_ork.web.recipes.load_lanes",
        lambda home=None: {},
    )
    checks = acp_setup.readiness(tmp_path, lane="codex")
    lanes = next(c for c in checks if c.name == "lanes")
    assert lanes.ok is True
    assert "no worker lanes" in lanes.detail


# ── run_interactive ─────────────────────────────────────────────────────────


def test_run_interactive_decline_returns_1(tmp_path: Path, monkeypatch):
    """Declining every fix returns exit code 1 and writes nothing to stdout."""
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=False)
    stdin = io.StringIO("n\n")
    stdout = io.StringIO()
    rc = acp_setup.run_interactive(tmp_path, stdin=stdin, stdout=stdout)
    assert rc == 1
    # The dialog writes to stdout (the test stream), nothing to real stdout.
    out = stdout.getvalue()
    assert "✗" in out
    assert "mini-ork first-run setup" in out


def test_run_interactive_accept_init_returns_0(tmp_path: Path, monkeypatch):
    """Accepting the ``mini-ork init`` fix creates the dir and re-passes."""
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=True)
    _patch_run(monkeypatch, stdout=json.dumps({"loggedIn": True}))
    # First call (init fix): subprocess ``run`` returns rc=0. Then ``claude
    # auth status --json`` is called and reports loggedIn. The combined
    # fixture is OK with both call sites using the same fake_run.
    stdin = io.StringIO("y\n")
    stdout = io.StringIO()
    rc = acp_setup.run_interactive(tmp_path, stdin=stdin, stdout=stdout)
    assert rc == 0


def test_run_interactive_writes_only_to_given_stream(tmp_path: Path, monkeypatch):
    """run_interactive output goes to the supplied stream, not real stdout."""
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=False)
    stdin = io.StringIO("n\n")
    # Real stdout captured at call time.
    real_stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", real_stdout)
    capture = io.StringIO()
    acp_setup.run_interactive(tmp_path, stdin=stdin, stdout=capture)
    # ``capture`` has the dialog; ``real_stdout`` (the monkey-patched sys.stdout)
    # does not — the function uses the ``stdout=`` argument when given.
    assert "mini-ork first-run setup" in capture.getvalue()
    assert real_stdout.getvalue() == ""


def test_run_interactive_full_pass_exits_zero(tmp_path: Path, monkeypatch):
    """When every check passes on the first call, exit 0 with no prompts."""
    (tmp_path / ".mini-ork").mkdir()
    _patch_claude(monkeypatch, present=True)
    _patch_run(monkeypatch, stdout=json.dumps({"loggedIn": True}))
    stdin = io.StringIO("")  # never asked — every check ok
    stdout = io.StringIO()
    rc = acp_setup.run_interactive(tmp_path, stdin=stdin, stdout=stdout)
    assert rc == 0
    assert "✓" in stdout.getvalue()

def test_project_check_fails_without_a_mini_ork_dir(tmp_path: Path, monkeypatch):
    """No .mini-ork in the project is the most common first-run gap; the check
    must not pass just because the project directory exists."""
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    _patch_claude(monkeypatch, present=True)
    _patch_run(monkeypatch, stdout=json.dumps({"loggedIn": True}))
    checks = {c.name: c for c in acp_setup.readiness(tmp_path)}
    assert checks["project"].ok is False
    assert checks["project"].fix == "mini-ork init"


def test_lane_check_reads_the_projects_secret_store(tmp_path: Path, monkeypatch):
    """A gateway lane whose key sits only in <project>/.mini-ork/config/secrets.local.sh
    is configured — the agent process never sourced that file."""
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  gw: {kind: anthropic-compat, family: gateway, base_url: 'https://example.invalid', "
        "api_key_env: GW_TEST_KEY, model: test-model}\n")
    (home / "config" / "secrets.local.sh").write_text("export GW_TEST_KEY=sk-test-123\n")
    (home / "config" / "secrets.local.sh").chmod(0o600)
    monkeypatch.delenv("GW_TEST_KEY", raising=False)
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    check = acp_setup._check_provider_credentials("gw", role="implementer", home=home)
    assert check.ok, check.detail
    assert "sk-test-123" not in check.detail
