"""B5 — a lane must never push.

Observed live: a lane self-pushed to ``main`` before the reviewer ran, so the
reviewed artifact and the branch had already diverged. The framework owns
commit / merge / push; a lane only edits the working tree.

The guard has two halves and this file pins both:

1. a process-level neutralisation — every lane dispatch injects
   ``remote.origin.pushurl`` through the ``GIT_CONFIG_*`` env channel, so a
   ``git push`` inside the lane fails (``_lane_no_push_env`` via
   ``providers.dispatch_model``); ``MO_LANE_ALLOW_PUSH=1`` opts a lane out, and
   the operator's own ``GIT_CONFIG_COUNT`` is preserved, not clobbered;
2. a prompt-level rule in ``scope_guard_block``, which every agentic lane
   prompt already carries.

Test 1 runs a real ``git push`` under the guard's exact env against a real bare
remote — the keys being present is not the contract; the push failing is.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mini_ork.cli.execute_handlers import scope_guard_block
from mini_ork.dispatch import providers
from mini_ork.dispatch.models import DispatchRequest, DispatchResult

REPO = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                          env={**os.environ, **(env or {})})


# ── 1. the guard actually stops a push ──────────────────────────────────────


def test_guarded_env_makes_a_push_fail(tmp_path):
    """A real push under the guard's env is refused and the remote stays empty."""
    bare = tmp_path / "origin.git"
    subprocess.check_call(["git", "init", "-q", "--bare", str(bare)])
    work = tmp_path / "work"
    subprocess.check_call(["git", "clone", "-q", str(bare), str(work)])
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("hi\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-q", "-m", "init")

    guarded = providers._lane_no_push_env({})
    proc = _git(work, "push", "origin", "HEAD:main", env=guarded)

    assert proc.returncode != 0, proc
    # the remote never received the branch
    branches = _git(bare, "branch", "--list").stdout
    assert "main" not in branches


def test_the_same_push_succeeds_without_the_guard(tmp_path):
    """Control: the bare remote and clone are otherwise fully push-capable."""
    bare = tmp_path / "origin.git"
    subprocess.check_call(["git", "init", "-q", "--bare", str(bare)])
    work = tmp_path / "work"
    subprocess.check_call(["git", "clone", "-q", str(bare), str(work)])
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("hi\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-q", "-m", "init")

    proc = _git(work, "push", "origin", "HEAD:main")

    assert proc.returncode == 0, proc
    assert "main" in _git(bare, "branch", "--list").stdout


# ── 2. the helper's env contract ────────────────────────────────────────────


def test_helper_neutralises_remote_origin_pushurl(monkeypatch):
    monkeypatch.delenv("MO_LANE_ALLOW_PUSH", raising=False)
    env = providers._lane_no_push_env({})

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "remote.origin.pushurl"
    assert env["GIT_CONFIG_VALUE_0"].startswith("no-push://")


def test_helper_opt_out_is_env_gated(monkeypatch):
    monkeypatch.setenv("MO_LANE_ALLOW_PUSH", "1")
    assert providers._lane_no_push_env({"A": "b"}) == {"A": "b"}


def test_helper_preserves_an_existing_git_config_count(monkeypatch):
    """A lane env that already carries GIT_CONFIG_* keeps its entries."""
    monkeypatch.delenv("MO_LANE_ALLOW_PUSH", raising=False)
    env = providers._lane_no_push_env(
        {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "user.name",
         "GIT_CONFIG_VALUE_0": "lane"}
    )

    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "user.name"          # untouched
    assert env["GIT_CONFIG_KEY_2"] == "remote.origin.pushurl"
    assert env["GIT_CONFIG_VALUE_2"].startswith("no-push://")


def test_helper_garbage_count_does_not_crash(monkeypatch):
    monkeypatch.delenv("MO_LANE_ALLOW_PUSH", raising=False)
    env = providers._lane_no_push_env({"GIT_CONFIG_COUNT": "not-a-number"})

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "remote.origin.pushurl"


# ── 3. every lane dispatch carries the guard (production entrypoint) ────────

_REGISTRY = """\
providers:
  t_chat:
    kind: openai-chat
    base_url: http://127.0.0.1:9/v1
    api_key_env: T_CHAT_KEY
    model: m
  t_lane:
    kind: anthropic-compat
    base_url: http://127.0.0.1:9
    api_key_env: T_LANE_KEY
    model: m
"""


def _dispatch_capturing_env(tmp_path, monkeypatch, model: str) -> dict:
    reg = tmp_path / "providers.yaml"
    reg.write_text(_REGISTRY)
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(reg))
    monkeypatch.setenv("T_CHAT_KEY", "k")
    monkeypatch.setenv("T_LANE_KEY", "k")
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))
    seen: dict = {}

    def capture(request, spec):
        seen.update(request.env)
        return DispatchResult(ok=True, rc=0, text="ok", model=request.model)

    monkeypatch.setitem(providers.MODEL_DISPATCH_BACKENDS, model, capture)
    res = providers.dispatch_model(
        DispatchRequest(model=model, prompt="p", workspace="host"), str(REPO)
    )
    assert res.ok, res.error
    return seen


def test_dispatch_model_injects_the_push_guard_into_every_lane_kind(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MO_LANE_ALLOW_PUSH", raising=False)
    for model in ("t_chat", "t_lane"):
        env = _dispatch_capturing_env(tmp_path, monkeypatch, model)
        assert env.get("GIT_CONFIG_KEY_0") == "remote.origin.pushurl", model
        assert env["GIT_CONFIG_VALUE_0"].startswith("no-push://"), model


def test_dispatch_model_honours_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_LANE_ALLOW_PUSH", "1")
    env = _dispatch_capturing_env(tmp_path, monkeypatch, "t_lane")

    assert "GIT_CONFIG_KEY_0" not in env


# ── 4. the prompt rule rides in the guard every lane prompt already has ─────


def test_lane_prompt_forbids_commit_merge_push():
    block = scope_guard_block("/tmp/run-x")

    assert "Repository write guard" in block
    for verb in ("git commit", "git merge", "git push"):
        assert verb in block


# ── 5. the prompt rule is wired into the agentic lane prompt sites ──────────


def test_scope_guard_is_wired_into_the_lane_prompts():
    src = (REPO / "mini_ork" / "cli" / "execute_handlers.py").read_text()
    # the guard block is appended to the implementer / lens / reviewer prompts
    assert src.count("ctx.scope_guard()") >= 4
