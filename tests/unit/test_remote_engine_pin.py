"""RBF-48: the remote engine sha is pinned once per run; a self-edit run is
not defaulted to a remote node.

Two mechanisms from the kickoff (``rbf-remote-selfedit``):

* ``RemoteWorkspace.up()`` resolves and dirty-checks the engine sha exactly
  ONCE per run — on the first ``up()``, before any provisioning — and pins it
  in memory and in ``<run_dir>/.remote-engine-sha``. Later ``up()``/dispatches
  in the same run (a fresh process included) reuse the pin and never re-run
  the dirty check, so a self-edit that dirties or moves the engine tree cannot
  refuse the run mid-flight. A dirty engine on the FIRST ``up()`` still
  refuses, and it refuses BEFORE a sandbox is minted (RB-44).
* ``_apply_default_placement`` does not default a self-edit run to remote
  (``MO_ALLOW_FRAMEWORK_CWD=1``, or the target's git toplevel equals the
  engine root's), while an ordinary target still adopts the profile and an
  explicit ``--placement remote`` still wins.

Hermetic: the engine tree is a tmp git repo, the node provisioner and HTTP are
stubbed at the instance level, and the run-event emitter is neutralised — no
network, no credentials, no live DB.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import mini_ork
from mini_ork.cli.main import _apply_default_placement
from mini_ork.runtime.backends.remote import (
    _NodeRef,
    RemoteUnavailableError,
    RemoteWorkspace,
)

_REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]

_SCRUB_ENV_KEYS = (
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_DB", "MINI_ORK_HOME",
    "MINI_ORK_ROOT", "MINI_ORK_ENGINE_ROOT", "MO_REMOTE_ALLOW_DIRTY_ENGINE",
    "MO_NODE_TOKEN", "MO_DEFAULT_NODE_ENV", "MO_PLACEMENT", "MO_NODE_ENV",
    "MO_REMOTE_NODE", "MO_ALLOW_FRAMEWORK_CWD", "MO_TARGET_CWD",
)

_PROFILE = "stubby"
_SELF_EDIT_LINE = (
    f"[placement] self-edit run: staying local (default profile {_PROFILE} not applied)"
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for key in _SCRUB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # The run-event emitter must never reach a live DB from a unit test.
    import mini_ork.observability.node_events as node_events

    monkeypatch.setattr(node_events, "mo_node_emit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {args} failed in {repo}: {proc.stderr}"
    return proc.stdout.strip()


def _engine_repo(path: Path) -> Path:
    """A clean tmp git repo standing in for the engine checkout."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "engine.txt").write_text("v1\n", encoding="utf-8")
    _git(path, "add", "engine.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


class _Stubs:
    """Instance-level stubs for one RemoteWorkspace: provisioner + HTTP."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.session_posts: list[dict] = []

    def attach(self, ws: RemoteWorkspace) -> None:
        def fake_ensure_node(*, create: bool):
            self.events.append(("provision", create))
            return None

        ws.ensure_node = fake_ensure_node

        def fake_request(method, path, *, body=None, content_type=None,
                         query=None, timeout=30.0, retries=None):
            self.events.append(("http", method, path))
            if method == "POST" and path == "/v1/sessions":
                if body is not None:
                    self.session_posts.append(json.loads(body.decode("utf-8")))
                return json.dumps({"sid": "test-sid"}).encode("utf-8")
            if method == "GET" and path == "/v1/health":
                return json.dumps({"sessions": 0, "engine_shas": []}).encode("utf-8")
            return b"{}"

        ws._request = fake_request
        ws._sync_up = lambda **kwargs: None
        ws._upload_mo_home = lambda: None


def _workspace(engine_root: Path, run_dir: Path) -> RemoteWorkspace:
    return RemoteWorkspace(
        node=_NodeRef(name="test-node", url="http://127.0.0.1:9", token="test",
                      max_sessions=1, headers={}),
        run_id="test-run-pin",
        image="spec/image:1",
        drive_root=str(run_dir / "drive"),
        engine_root=str(engine_root),
        run_dir=str(run_dir),
        node_provider="test-provider",
        provider_env={},
    )


def _counting_sha(monkeypatch) -> list[str]:
    """Wrap ``_current_engine_sha`` so the test can prove it ran exactly once."""
    calls: list[str] = []
    real = RemoteWorkspace._current_engine_sha

    def counting(self, subprocess_mod):
        calls.append(self._engine_root)
        return real(self, subprocess_mod)

    monkeypatch.setattr(RemoteWorkspace, "_current_engine_sha", counting)
    return calls


# ---------------------------------------------------------------------------
# mechanism 1: the engine sha is pinned once, before provisioning
# ---------------------------------------------------------------------------


def test_first_up_pins_sha_and_a_second_dispatch_reuses_it(tmp_path, monkeypatch):
    engine = _engine_repo(tmp_path / "engine")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    head = _git(engine, "rev-parse", "HEAD")
    calls = _counting_sha(monkeypatch)

    stubs = _Stubs()
    ws = _workspace(engine, run_dir)
    stubs.attach(ws)
    ws.up()

    pin = run_dir / ".remote-engine-sha"
    assert pin.is_file(), "the first up() must pin the sha in the run dir"
    assert pin.read_text(encoding="utf-8").strip() == head
    assert ws._engine_sha == head
    assert len(calls) == 1
    # a clean engine still provisions (the check precedes it, it does not
    # replace it)
    assert ("provision", True) in stubs.events

    # the implementer edits the engine tree: a self-edit run is now dirty
    (engine / "engine.txt").write_text("edited by the implementer\n", encoding="utf-8")
    assert subprocess.run(["git", "-C", str(engine), "diff", "--quiet"],
                          check=False).returncode != 0

    # same process, later dispatch
    ws.up()

    # a later dispatch in a new process: a fresh workspace sharing only the
    # run dir must read the durable pin, not re-resolve a dirty tree
    stubs2 = _Stubs()
    ws2 = _workspace(engine, run_dir)
    stubs2.attach(ws2)
    ws2.up()

    assert len(calls) == 1, "the dirty check must run exactly once per run"
    assert pin.read_text(encoding="utf-8").strip() == head
    assert stubs2.session_posts, "the second dispatch must reach session create"
    assert all(post.get("engine_sha") == head for post in stubs2.session_posts)


def test_pinned_sha_wins_when_head_moves(tmp_path, monkeypatch):
    engine = _engine_repo(tmp_path / "engine")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    head = _git(engine, "rev-parse", "HEAD")
    calls = _counting_sha(monkeypatch)

    stubs = _Stubs()
    ws = _workspace(engine, run_dir)
    stubs.attach(ws)
    ws.up()

    # the framework lands the self-edit between nodes: HEAD moves past the pin
    (engine / "engine.txt").write_text("committed edit\n", encoding="utf-8")
    _git(engine, "add", "engine.txt")
    _git(engine, "commit", "-q", "-m", "self-edit landed")
    moved = _git(engine, "rev-parse", "HEAD")
    assert moved != head

    stubs2 = _Stubs()
    ws2 = _workspace(engine, run_dir)
    stubs2.attach(ws2)
    ws2.up()

    assert len(calls) == 1
    assert all(post.get("engine_sha") == head for post in stubs2.session_posts), (
        f"the run must keep the sha pinned on its first up() ({head}), not {moved}")


def test_dirty_first_up_refuses_before_provisioning(tmp_path):
    engine = _engine_repo(tmp_path / "engine")
    (engine / "engine.txt").write_text("dirty from the start\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    stubs = _Stubs()
    ws = _workspace(engine, run_dir)
    stubs.attach(ws)

    with pytest.raises(RemoteUnavailableError) as excinfo:
        ws.up()

    message = str(excinfo.value)
    assert "is dirty" in message, message
    assert "must name an exact tree" in message, message
    # RB-44: refuse before a sandbox is minted
    assert not any(kind == "provision" for kind, *_ in stubs.events), (
        "the dirty-engine check must run before the provisioner")
    assert not any(e[0] == "http" and e[1] == "POST" and e[2] == "/v1/sessions"
                   for e in stubs.events), "a refused up() must not mint a session"
    assert not (run_dir / ".remote-engine-sha").exists(), (
        "a refused first up() must pin nothing")


# ---------------------------------------------------------------------------
# mechanism 2: a self-edit run is not defaulted to remote
# ---------------------------------------------------------------------------


def _repo(path: Path) -> Path:
    """A tmp git repo with a toplevel distinct from the engine checkout."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _engine_toplevel() -> Path:
    return Path(_git(_REPO_ROOT, "rev-parse", "--show-toplevel"))


def _profile_home(path: Path) -> Path:
    env_dir = path / "config" / "environments"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / f"{_PROFILE}.yaml").write_text(
        "provider: stub\nimage: stub:1\n", encoding="utf-8")
    return path


def _placement_env(tmp_path, monkeypatch, *, allow_flag: str, target_cwd: Path,
                   engine_root: Path) -> dict[str, str]:
    env = {
        "MO_ALLOW_FRAMEWORK_CWD": allow_flag,
        "MO_TARGET_CWD": str(target_cwd),
        "MINI_ORK_ROOT": str(engine_root),
        "MINI_ORK_HOME": str(_profile_home(tmp_path / "home")),
        "MO_DEFAULT_NODE_ENV": _PROFILE,
        "MO_REMOTE_NODE": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def _output(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_framework_cwd_flag_keeps_the_run_local(tmp_path, monkeypatch, capsys):
    engine = _repo(tmp_path / "engine")   # distinct toplevels: the flag alone
    target = _repo(tmp_path / "target")   # makes this a self-edit
    env = _placement_env(tmp_path, monkeypatch, allow_flag="1", target_cwd=target,
                         engine_root=engine)

    assert _apply_default_placement("", "", env) == ("", "", False)
    assert _SELF_EDIT_LINE in _output(capsys)


def test_target_toplevel_equal_to_engine_toplevel_keeps_the_run_local(
        tmp_path, monkeypatch, capsys):
    engine_top = _engine_toplevel()   # flag deliberately not "1"
    env = _placement_env(tmp_path, monkeypatch, allow_flag="0",
                         target_cwd=engine_top, engine_root=engine_top)

    assert _apply_default_placement("", "", env) == ("", "", False)
    assert _SELF_EDIT_LINE in _output(capsys)


def test_ordinary_target_still_defaults_to_the_profile(tmp_path, monkeypatch, capsys):
    target = _repo(tmp_path / "target")
    env = _placement_env(tmp_path, monkeypatch, allow_flag="0", target_cwd=target,
                         engine_root=_engine_toplevel())

    assert _apply_default_placement("", "", env) == ("remote", _PROFILE, True)
    assert _SELF_EDIT_LINE not in _output(capsys)


def test_explicit_remote_placement_is_still_honoured(tmp_path, monkeypatch, capsys):
    engine_top = _engine_toplevel()
    env = _placement_env(tmp_path, monkeypatch, allow_flag="1",
                         target_cwd=engine_top, engine_root=engine_top)

    assert _apply_default_placement("remote", _PROFILE, env) == ("remote", _PROFILE, False)
    assert _SELF_EDIT_LINE not in _output(capsys)
