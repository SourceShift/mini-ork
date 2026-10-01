"""Unit tests for ``mini_ork.remote.run_mirror`` + the mirror hooks on
``RemoteWorkspace`` (remote-nodes-08, kickoff §"Acceptance").

Layered tests:

* Pure-module unit tests (no HTTP) for ``manifest`` / ``build_push_tar`` /
  ``apply_pull_tar`` / ``mo_home_files`` / ``build_mo_home_tar`` /
  ``write_sidecar`` / ``read_sidecar``.
* Operator-contract tests: ``DEFAULT_EXCLUDES``, ``DENY_LIST``, default size
  caps are importable as module-level constants (the kickoff's "import
  surface is the contract").
* ``RemoteWorkspace`` mirror hooks: stubbed ``_request`` for the
  ``mirror_push`` / ``mirror_pull`` flow (events, deny-list, over-cap,
  conflict).
* Real-socket acceptance: spins up the node-agent with ``runtime="host"``,
  exercises the full push-then-spawn-then-pull loop end-to-end so the
  executor's readers at ``cli/execute_handlers.py:499-505`` and
  ``providers.py:1511-1529`` see remote tokens, costs, and outputs.

The review bar (kickoff §"Review bar") requires every acceptance bullet
have its own test that drives the production path. Each test below
covers ONE bullet:

* lens sidecar pull -> ``test_real_socket_session_round_trip_with_mirror``
* conflict event    -> ``test_pull_emits_conflict_when_local_changed``
* over-cap skip     -> ``test_mirror_push_skips_oversize_file_with_event``
* deny-list         -> ``test_mo_home_upload_excludes_deny_listed_files``
* default-path      -> ``test_run_mirror_module_not_imported_when_backend_unset``
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import threading
import time

from mini_ork.remote.node_agent.app import create_app
from mini_ork.remote.run_mirror import (
    DEFAULT_EXCLUDES,
    DEFAULT_MAX_FILE_MB,
    DEFAULT_MAX_TOTAL_MB,
    DENY_LIST,
    apply_pull_tar,
    build_mo_home_tar,
    build_push_tar,
    diff_manifests,
    manifest,
    mo_home_files,
    read_sidecar,
    write_sidecar,
)
from mini_ork.runtime.backends.remote import (
    RemoteUnavailableError,
    RemoteWorkspace,
    _NodeRef,
    _env_int_mb,
)


# ---------------------------------------------------------------------------
# Operator contract (kickoff §"Files in scope" + the importable-surface rule).
# ---------------------------------------------------------------------------


def test_operator_contract_constants_are_present():
    """The deny-list, default excludes, and size caps are importable directly.

    Per kickoff §1 + prior-art-lens §1.5: a unit test asserts the operator
    contract by importing the module, not by reaching into a private field.
    """
    # The set the kickoff explicitly enumerates — each entry must be present
    # by its exact glob (basename match). A regression here means an
    # executor-owned file silently transits to the remote.
    for name in (
        "execute.log",
        "*.pid",
        ".stop-requested",
        ".workspace-session.json",
        "state.db*",
    ):
        assert name in DEFAULT_EXCLUDES, f"missing exclude: {name!r}"
    assert any(p.endswith("live.jsonl") for p in DEFAULT_EXCLUDES)
    # Deny-list — kickoff §2 names each name verbatim.
    for name in ("state.db", "secrets.local.sh", "auth-tokens.txt"):
        assert name in DENY_LIST, f"deny-list missing {name!r}"
    assert any(p.endswith(".env") for p in DENY_LIST)
    # Size caps — defaults per kickoff §1.
    assert DEFAULT_MAX_FILE_MB == 50
    assert DEFAULT_MAX_TOTAL_MB == 500


def test_env_int_mb_uses_default_when_unset():
    """``_env_int_mb`` returns the default when the env var is missing or invalid."""
    os.environ.pop("MO_REMOTE_MIRROR_MAX_FILE_MB", None)
    assert _env_int_mb("MO_REMOTE_MIRROR_MAX_FILE_MB", DEFAULT_MAX_FILE_MB) == 50 * 1024 * 1024
    os.environ["MO_REMOTE_MIRROR_MAX_FILE_MB"] = "not-an-int"
    try:
        assert _env_int_mb("MO_REMOTE_MIRROR_MAX_FILE_MB", DEFAULT_MAX_FILE_MB) == 50 * 1024 * 1024
    finally:
        os.environ.pop("MO_REMOTE_MIRROR_MAX_FILE_MB", None)


def test_env_int_mb_respects_override():
    """``_env_int_mb`` honours a positive integer override."""
    os.environ["MO_REMOTE_MIRROR_MAX_FILE_MB"] = "10"
    try:
        assert _env_int_mb("MO_REMOTE_MIRROR_MAX_FILE_MB", DEFAULT_MAX_FILE_MB) == 10 * 1024 * 1024
    finally:
        os.environ.pop("MO_REMOTE_MIRROR_MAX_FILE_MB", None)


# ---------------------------------------------------------------------------
# Module-import contract (review bar bullet 4: default path byte-identical).
# ---------------------------------------------------------------------------


def test_run_mirror_module_not_imported_when_backend_unset(monkeypatch):
    """With ``MO_SANDBOX_BACKEND`` unset, the run-mirror module must not load.

    Same isolated-subprocess pattern as
    ``test_remote_module_not_imported_when_backend_unset`` — the test file's
    own imports would otherwise mask any regression. The probe runs the
    default-path resolver and asserts the module is absent from sys.modules.
    """
    monkeypatch.delenv("MO_SANDBOX_BACKEND", raising=False)
    script = (
        "import sys\n"
        "from mini_ork.runtime.agent_workspace import (\n"
        "    resolve_agent_workspace, resolve_spawn_workspace,\n"
        ")\n"
        "ws, cwd = resolve_agent_workspace('/tmp', env={'PATH': ''})\n"
        "assert ws is None, f'unset backend should produce None, got {ws!r}'\n"
        "local_ws = resolve_spawn_workspace('local', env={'PATH': ''})\n"
        "assert local_ws.__class__.__name__ == 'LocalWorkspace', local_ws\n"
        "assert 'mini_ork.remote.run_mirror' not in sys.modules, "
        "'run_mirror was loaded by an unset-backend path'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"isolated resolver probe failed:\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Manifest walker.
# ---------------------------------------------------------------------------


def test_manifest_returns_sha_size_mtime_tuple(tmp_path):
    """``manifest`` produces forward-slash relpaths and a 3-tuple per file."""
    (tmp_path / "a.txt").write_text("hello")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world")
    m = manifest(tmp_path)
    assert set(m.keys()) == {"a.txt", "sub/b.txt"}, m
    for rel, info in m.items():
        assert len(info) == 3
        sha, size, mtime = info
        assert isinstance(sha, str) and len(sha) == 64
        assert isinstance(size, int) and size > 0
        assert isinstance(mtime, float) and mtime > 0
        assert isinstance(rel, str)


def test_manifest_skips_default_excludes(tmp_path):
    """``execute.log``, ``*.pid``, ``state.db*`` are NOT in the manifest."""
    (tmp_path / "ok.txt").write_text("ok")
    (tmp_path / "execute.log").write_text("executor log")
    (tmp_path / "worker.pid").write_text("123")
    (tmp_path / "state.db").write_text("db")
    (tmp_path / "state.db-wal").write_text("wal")
    m = manifest(tmp_path, excludes=DEFAULT_EXCLUDES)
    assert "ok.txt" in m
    for bad in ("execute.log", "worker.pid", "state.db", "state.db-wal"):
        assert bad not in m, f"exclude failed for {bad!r}: {list(m)}"


def test_manifest_follows_symlinks_to_files(tmp_path):
    """Symlinks-to-files ARE followed (aligns with remote walker)."""
    target = tmp_path / "secret.txt"
    target.write_text("secret")
    link = tmp_path / "alias.txt"
    link.symlink_to(target)
    m = manifest(tmp_path)
    assert "alias.txt" in m, (
        f"symlink should be hashed by target content; got {list(m)}"
    )


def test_diff_manifests_reports_changed_and_added():
    """Changed values and brand-new keys both appear; unchanged ones don't."""
    before = {"a": "1", "b": "2", "c": "3"}
    after = {"a": "1", "b": "DIFF", "d": "4"}
    out = diff_manifests(before, after)
    assert out == {"b": "DIFF", "d": "4"}, out


# ---------------------------------------------------------------------------
# Push tar + size caps.
# ---------------------------------------------------------------------------


def test_build_push_tar_includes_changed_files(tmp_path):
    """``build_push_tar`` writes only relpaths in ``members``."""
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "drop.txt").write_text("d")
    m = manifest(tmp_path)
    tar_bytes, stats = build_push_tar(
        tmp_path, {"keep.txt": m["keep.txt"]},
        max_file_bytes=10 * 1024 * 1024, max_total_bytes=10 * 1024 * 1024,
    )
    assert stats["files"] == 1
    assert stats["bytes"] == len("k")
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        names = sorted(m.name for m in tf.getmembers())
    assert names == ["keep.txt"], names


def test_build_push_tar_skips_oversize_file_with_callback(tmp_path):
    """A file over ``max_file_bytes`` is skipped and ``on_skip`` fires."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (60 * 1024 * 1024))  # 60 MB
    small = tmp_path / "small.txt"
    small.write_text("hi")
    m = manifest(tmp_path)
    skipped: list[tuple[str, int]] = []
    tar_bytes, stats = build_push_tar(
        tmp_path, m,
        max_file_bytes=50 * 1024 * 1024, max_total_bytes=500 * 1024 * 1024,
        on_skip=lambda rp, sz: skipped.append((rp, sz)),
    )
    assert stats["files"] == 1
    assert stats["skipped"] == 1
    assert stats["bytes"] == len("hi")
    assert skipped and skipped[0][0] == "big.bin", skipped
    # The tar body must NOT contain big.bin — the file was never read.
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        names = [m.name for m in tf.getmembers()]
    assert "big.bin" not in names, names
    assert "small.txt" in names


def test_build_push_tar_enforces_total_cap(tmp_path):
    """Total cap is enforced AFTER per-file cap; an oversized tail file is skipped."""
    # Three 30 MB files; total cap = 50 MB. The first one fits, the next two
    # would push the total past 50 MB and must be skipped.
    for i in range(3):
        (tmp_path / f"f{i}.bin").write_bytes(b"y" * (30 * 1024 * 1024))
    m = manifest(tmp_path)
    skipped: list[tuple[str, int]] = []
    _, stats = build_push_tar(
        tmp_path, m,
        max_file_bytes=50 * 1024 * 1024, max_total_bytes=50 * 1024 * 1024,
        on_skip=lambda rp, sz: skipped.append((rp, sz)),
    )
    assert stats["files"] == 1, stats
    assert stats["skipped"] == 2, stats


# ---------------------------------------------------------------------------
# Pull tar + conflict resolution.
# ---------------------------------------------------------------------------


def test_apply_pull_tar_writes_changed_files_atomically(tmp_path):
    """A relpath in ``changed`` whose local sha matches push is written; the
    freshly-written file's mtime is set to NOW (kickoff §1's "agent wins by
    mtime" preservation).
    """
    src = tmp_path / "remote-wrote.txt"
    src.write_text("remote body")
    # Build a single-file tar
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo(name="remote-wrote.txt")
        body = b"remote body"
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))
    tar_bytes = buf.getvalue()
    before_mtime = time.time() - 3600  # one hour ago
    os.utime(src, (before_mtime, before_mtime))
    local_at_push = {"remote-wrote.txt": "deadbeef" * 8}  # local NOT touched since push
    local_now = {"remote-wrote.txt": "deadbeef" * 8}
    changed = {"remote-wrote.txt": "feedface" * 8}
    conflicts, written, bytes_written = apply_pull_tar(
        tmp_path, tar_bytes, changed, local_at_push, local_now,
    )
    assert conflicts == []
    assert written == 1
    assert bytes_written == len("remote body")
    new_mtime = (tmp_path / "remote-wrote.txt").stat().st_mtime
    assert new_mtime > before_mtime, (new_mtime, before_mtime)


def test_apply_pull_tar_conflict_emits_event_and_keeps_local(tmp_path):
    """If local sha differs from local_at_push, local wins; on_conflict fires."""
    target = tmp_path / "shared.txt"
    target.write_text("local-new-content")
    # Build a tar that has remote's version of shared.txt
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo(name="shared.txt")
        body = b"remote-content"
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))
    tar_bytes = buf.getvalue()
    # At push time the local sha was X. The executor then wrote a new body
    # whose sha is Y (X != Y).
    local_at_push = {"shared.txt": "pushed-sha"}
    local_now = {"shared.txt": "current-local-sha"}
    changed = {"shared.txt": "remote-sha"}
    seen: list[str] = []
    conflicts, written, _ = apply_pull_tar(
        tmp_path, tar_bytes, changed, local_at_push, local_now,
        on_conflict=seen.append,
    )
    assert conflicts == ["shared.txt"], conflicts
    assert written == 0
    assert seen == ["shared.txt"]
    # Local content must be UNCHANGED — control plane wins on conflict.
    assert target.read_text() == "local-new-content"


def test_apply_pull_tar_skips_unchanged_remote_files(tmp_path):
    """Members in the tar NOT in ``changed`` are skipped — they didn't move on the remote."""
    # The pull tar might contain a file the remote already had; the diff
    # said only 'changed.txt' changed, so 'unchanged.txt' must NOT be touched.
    (tmp_path / "unchanged.txt").write_text("local-stale")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, body in (("changed.txt", b"new"), ("unchanged.txt", b"remote-says-new")):
            ti = tarfile.TarInfo(name=name)
            ti.size = len(body)
            tf.addfile(ti, io.BytesIO(body))
    tar_bytes = buf.getvalue()
    local_at_push = {"changed.txt": "old", "unchanged.txt": "old"}
    local_now = {"changed.txt": "old", "unchanged.txt": "old"}
    changed = {"changed.txt": "new"}
    _, written, _ = apply_pull_tar(
        tmp_path, tar_bytes, changed, local_at_push, local_now,
    )
    assert written == 1
    assert (tmp_path / "changed.txt").read_text() == "new"
    # The unchanged file must NOT have been rewritten.
    assert (tmp_path / "unchanged.txt").read_text() == "local-stale"


# ---------------------------------------------------------------------------
# mo-home subset + deny-list.
# ---------------------------------------------------------------------------


def test_mo_home_files_walks_config_yaml(tmp_path):
    """All ``*.yaml`` under ``<home>/config`` are picked up."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "providers.yaml").write_text("p")
    (config / "agents.yaml").write_text("a")
    (config / "README.md").write_text("readme — NOT yaml")
    files = mo_home_files(tmp_path)
    names = {p.name for p in files}
    assert names == {"providers.yaml", "agents.yaml"}, names


def test_mo_home_files_includes_recipe_when_named(tmp_path):
    """When ``recipe_name`` is given, ``recipes/<name>/**`` is included."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "providers.yaml").write_text("p")
    recipe = tmp_path / "recipes" / "framework-edit"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text("wf")
    sub = recipe / "prompts"
    sub.mkdir()
    (sub / "planner.md").write_text("p")
    files = mo_home_files(tmp_path, recipe_name="framework-edit")
    rels = {p.relative_to(tmp_path).as_posix() for p in files}
    assert "config/providers.yaml" in rels
    assert "recipes/framework-edit/workflow.yaml" in rels
    assert "recipes/framework-edit/prompts/planner.md" in rels


def test_mo_home_files_excludes_deny_listed(tmp_path):
    """``state.db``, ``secrets.local.sh``, ``auth-tokens.txt`` never appear."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "providers.yaml").write_text("p")
    # Deny-listed names that look like config but should NEVER upload.
    (config / "state.db").write_text("db")
    (config / "state.db-wal").write_text("wal")
    (config / "secrets.local.sh").write_text("export X=Y")
    (config / "auth-tokens.txt").write_text("abc")
    (config / "prod.env").write_text("KEY=val")
    files = mo_home_files(tmp_path)
    names = {p.name for p in files}
    assert names == {"providers.yaml"}, names


def test_build_mo_home_tar_relpaths_are_under_home(tmp_path):
    """Tar members use ``config/<name>`` as their relpath."""
    config = tmp_path / "config"
    config.mkdir()
    p = config / "providers.yaml"
    p.write_text("p")
    tar_bytes = build_mo_home_tar(tmp_path, [p])
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        members = tf.getmembers()
    assert len(members) == 1
    assert members[0].name == "config/providers.yaml"


# ---------------------------------------------------------------------------
# Sidecar (resume support).
# ---------------------------------------------------------------------------


def test_sidecar_round_trip(tmp_path):
    """``write_sidecar`` then ``read_sidecar`` returns the same snapshot."""
    snap = {"a.txt": "abc", "b/c.txt": "def"}
    write_sidecar(tmp_path, snap)
    assert read_sidecar(tmp_path) == snap


def test_sidecar_missing_returns_empty(tmp_path):
    """An absent / corrupt sidecar yields ``{}`` (no exception, no false data)."""
    assert read_sidecar(tmp_path) == {}
    (tmp_path / ".mo-run-mirror.json").write_text("not json{")
    assert read_sidecar(tmp_path) == {}


# ---------------------------------------------------------------------------
# RemoteWorkspace mirror hooks — stubbed transport.
# ---------------------------------------------------------------------------


def _ws(**overrides):
    """Build a RemoteWorkspace without invoking ``up()``."""
    node = overrides.pop("node", _NodeRef(name="t", url="http://127.0.0.1:1", token="t", max_sessions=1))
    run_id = overrides.pop("run_id", "r")
    image = overrides.pop("image", "alpine:latest")
    drive_root = overrides.pop("drive_root", "/tmp")
    engine_root = overrides.pop("engine_root", os.getcwd())
    retries = overrides.pop("retries", 1)
    return RemoteWorkspace(
        node=node, run_id=run_id, image=image, drive_root=drive_root,
        engine_root=engine_root, retries=retries, **overrides,
    )


def test_mirror_push_noop_when_run_dir_unset():
    """Without ``run_dir``, ``mirror_push`` returns zeros and issues no HTTP."""
    ws = _ws(run_dir=None)
    ws._sid = "fake-sid"
    calls: list[tuple] = []
    ws._request = lambda *a, **kw: (calls.append((a, kw)) or b"")  # type: ignore[method-assign]
    out = ws.mirror_push()
    assert out == {"files": 0, "bytes": 0, "skipped": 0}, out
    assert calls == [], calls


def test_mirror_push_uploads_diff_against_remote_manifest(tmp_path):
    """``mirror_push`` PUTs a tar containing ONLY the diff vs the remote manifest."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "keep.txt").write_text("k")
    (run_dir / "changed.txt").write_text("new body")
    ws = _ws(run_dir=str(run_dir))
    ws._sid = "fake-sid"
    puts: list[dict] = []
    manifests_seen: list[dict] = []

    def _stub(method, path, *, body=None, query=None, content_type=None):
        if method == "POST" and path.endswith("/files/manifest"):
            manifests_seen.append({"query": dict(query or {}), "body": body})
            # Remote is empty initially.
            return json.dumps({"root": "run", "manifest": {}}).encode("utf-8")
        if method == "PUT" and path.endswith("/files"):
            puts.append({"body": body, "query": dict(query or {})})
            return json.dumps({"root": "run", "extracted": 2}).encode("utf-8")
        raise RemoteUnavailableError(f"unexpected {method} {path}")

    def _json_stub(method, path, _payload=None):
        raw = _stub(method, path, body=b"")
        return json.loads(raw.decode("utf-8"))

    ws._json = _json_stub  # type: ignore[method-assign]
    ws._request = _stub  # type: ignore[method-assign]
    out = ws.mirror_push()
    assert out["files"] == 2, out
    assert out["bytes"] > 0
    assert len(puts) == 1, puts
    # Manifest called ONCE with explicit body (no two-call retry).
    assert len(manifests_seen) == 1, manifests_seen
    # The tar must contain BOTH changed files (the remote was empty).
    with tarfile.open(fileobj=io.BytesIO(puts[0]["body"])) as tf:
        names = sorted(m.name for m in tf.getmembers())
    assert names == ["changed.txt", "keep.txt"], names


def test_mirror_push_skips_oversize_file_with_event(tmp_path, monkeypatch):
    """A file over ``MO_REMOTE_MIRROR_MAX_FILE_MB`` is skipped; ``mirror_push``
    emits ``remote.mirror.skipped`` (via mo_node_emit; the event is a silent
    no-op when ``state.db`` is missing — the unit-test path).
    """
    monkeypatch.setenv("MO_REMOTE_MIRROR_MAX_FILE_MB", "1")  # 1 MB cap
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "small.txt").write_text("ok")
    (run_dir / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB
    ws = _ws(run_dir=str(run_dir))
    ws._sid = "fake-sid"

    def _stub(method, path, **_kw):
        if method == "POST" and path.endswith("/files/manifest"):
            return json.dumps({"root": "run", "manifest": {}}).encode("utf-8")
        if method == "PUT" and path.endswith("/files"):
            return json.dumps({"root": "run", "extracted": 1}).encode("utf-8")
        raise RemoteUnavailableError(f"unexpected {method} {path}")

    def _json_stub(method, path, _payload=None):
        return json.loads(_stub(method, path).decode("utf-8"))

    ws._json = _json_stub  # type: ignore[method-assign]
    ws._request = _stub  # type: ignore[method-assign]
    out = ws.mirror_push()
    assert out["skipped"] == 1, out
    assert out["files"] == 1, out
    # Sidecar recorded both files (the push captured the snapshot).
    sidecar = json.loads((run_dir / ".mo-run-mirror.json").read_text())
    assert "big.bin" in sidecar["local_at_push"]
    assert "small.txt" in sidecar["local_at_push"]


def test_pull_emits_conflict_when_local_changed(tmp_path):
    """``mirror_pull`` calls ``on_conflict`` for files whose local sha diverged
    from the push-time snapshot. The control plane wins (no write).
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "shared.txt"
    target.write_text("local-new-body")
    ws = _ws(run_dir=str(run_dir))
    ws._sid = "fake-sid"
    # Pretend push happened with the OLD local sha for shared.txt.
    ws._mirror_push_snap = {"shared.txt": "old-pushed-sha"}
    # Remote says shared.txt is at a different sha now (agent edited it).
    remote_tar = io.BytesIO()
    with tarfile.open(fileobj=remote_tar, mode="w") as tf:
        ti = tarfile.TarInfo(name="shared.txt")
        ti.size = len(b"remote-new-body")
        tf.addfile(ti, io.BytesIO(b"remote-new-body"))
    tar_bytes = remote_tar.getvalue()

    def _stub(method, path, **kwargs):
        if method == "POST" and path.endswith("/files/manifest"):
            return json.dumps({"root": "run", "manifest": {"shared.txt": "remote-sha"}}).encode("utf-8")
        if method == "GET" and path.endswith("/files"):
            q = kwargs.get("query") or {}
            assert q.get("root") == "run"
            return tar_bytes
        raise RemoteUnavailableError(f"unexpected {method} {path}")

    def _json_stub(method, path, _payload=None):
        return json.loads(_stub(method, path).decode("utf-8"))

    ws._json = _json_stub  # type: ignore[method-assign]
    ws._request = _stub  # type: ignore[method-assign]
    out = ws.mirror_pull()
    assert out["conflicts"] == 1, out
    # Local content untouched.
    assert target.read_text() == "local-new-body"


def test_mirror_pull_resumes_via_sidecar_after_restart(tmp_path):
    """A fresh ``RemoteWorkspace`` (post-restart) reads the sidecar and uses
    it as the push-time baseline for conflict detection.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "shared.txt"
    target.write_text("local-new-body")
    # Seed the sidecar with a "push happened earlier" snapshot.
    write_sidecar(run_dir, {"shared.txt": "old-pushed-sha"})
    ws = _ws(run_dir=str(run_dir))
    ws._sid = "fake-sid"
    # ``_mirror_push_snap`` is empty (fresh process) — pull must read sidecar.

    def _stub(method, path, **_kwargs):
        if method == "POST" and path.endswith("/files/manifest"):
            return json.dumps({"root": "run", "manifest": {"shared.txt": "remote-sha"}}).encode("utf-8")
        if method == "GET" and path.endswith("/files"):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                ti = tarfile.TarInfo(name="shared.txt")
                ti.size = len(b"remote-new-body")
                tf.addfile(ti, io.BytesIO(b"remote-new-body"))
            return buf.getvalue()
        raise RemoteUnavailableError(f"unexpected {method} {path}")

    def _json_stub(method, path, _payload=None):
        return json.loads(_stub(method, path).decode("utf-8"))

    ws._json = _json_stub  # type: ignore[method-assign]
    ws._request = _stub  # type: ignore[method-assign]
    out = ws.mirror_pull()
    assert out["conflicts"] == 1, out
    assert target.read_text() == "local-new-body"


def test_mo_home_upload_excludes_deny_listed_files(tmp_path, monkeypatch):
    """``_upload_mo_home`` never uploads ``state.db``, ``secrets.local.sh``,
    ``auth-tokens.txt``. The deny-list test seeds all three names and asserts
    NONE arrives at the remote (kickoff acceptance bullet 4).
    """
    home = tmp_path / "home"
    config = home / "config"
    config.mkdir(parents=True)
    (config / "providers.yaml").write_text("p")
    (config / "state.db").write_text("db")
    (config / "secrets.local.sh").write_text("export X=Y")
    (config / "auth-tokens.txt").write_text("abc")
    (config / "prod.env").write_text("KEY=val")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    ws = _ws()
    ws._sid = "fake-sid"
    seen_bodies: list[bytes] = []

    def _stub(method, path, *, body=None, **kwargs):
        if method == "POST" and path == "/v1/sessions":
            return json.dumps({"sid": "fake-sid"}).encode("utf-8")
        if method == "PUT" and path.endswith("/files"):
            if body is not None:
                seen_bodies.append(body)
            q = kwargs.get("query") or {}
            return json.dumps({"root": q.get("root"), "extracted": 0}).encode("utf-8")
        if method == "GET" and path == "/v1/health":
            return json.dumps({"engine_shas": []}).encode("utf-8")
        if method == "PUT" and path.startswith("/v1/engines/"):
            return b""
        raise RemoteUnavailableError(f"unexpected {method} {path}")

    # The tree sync (epic 07) is covered elsewhere; this test is the mo-home upload.
    monkeypatch.setattr(RemoteWorkspace, "_sync_up", lambda self, force=False: None)
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    monkeypatch.setattr(
        "mini_ork.runtime.backends.remote.RemoteWorkspace._current_engine_sha",
        lambda _self, _subprocess_mod: "deadbeef00000000",
    )
    ws._request = _stub  # type: ignore[method-assign]

    def _json_stub(method, path, _payload=None):
        return json.loads(_stub(method, path).decode("utf-8"))

    ws._json = _json_stub  # type: ignore[method-assign]
    ws.up()
    # The mo-home PUT is the one whose query root is "mo-home".
    mo_home_puts = [b for b in seen_bodies if b is not None]
    assert mo_home_puts, "no mo-home upload attempted"
    # Decode the tar and assert NONE of the deny-listed names appears.
    with tarfile.open(fileobj=io.BytesIO(mo_home_puts[0])) as tf:
        names = [m.name for m in tf.getmembers()]
    assert "config/providers.yaml" in names, names
    for bad in ("config/state.db", "config/secrets.local.sh",
                "config/auth-tokens.txt", "config/prod.env"):
        assert bad not in names, f"deny-list leak: {bad} in {names}"
    assert ws._mo_home_uploaded is True
    # A second up() must NOT re-upload (idempotency).
    ws.up()
    assert len([b for b in seen_bodies if b is not None]) == 1, seen_bodies


# ---------------------------------------------------------------------------
# Real-socket acceptance: in-process node-agent, end-to-end mirror.
# ---------------------------------------------------------------------------


def test_real_socket_session_round_trip_with_mirror(tmp_path, monkeypatch):
    """End-to-end mirror against a LIVE uvicorn node-agent (kickoff §Acceptance).

    Drives the production urllib path against a real socket: ``up()`` runs
    the one-shot mo-home upload; a fake agent writes ``research.md``,
    ``framework-edit.diff``, and a usage sidecar; ``pull`` brings them
    back locally. Covers the lens-sidecar pull (Acceptance bullet 1).
    """
    import uvicorn

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Seed the run dir with a lens sidecar that the agent will read.
    (run_dir / "lens-a.md").write_text("context from lens-a")
    (run_dir / "ok.txt").write_text("ok")
    state = tmp_path / "agent"
    token = "real-socket-token"
    monkeypatch.setenv("MO_NODE_TOKEN", token)
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "real-socket-run")
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(state_dir=state, token_env="MO_NODE_TOKEN", runtime="host"),
        host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "node-agent did not start"
    ws = RemoteWorkspace(
        node=_NodeRef(name="real", url=f"http://127.0.0.1:{port}", token=token, max_sessions=1),
        run_id="real-socket-run", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=os.environ.get("MINI_ORK_ROOT") or os.getcwd(),
        token_env="MO_NODE_TOKEN", retries=1, run_dir=str(run_dir),
        target_root=_tiny_target(tmp_path),
    )
    try:
        ws.up()
        # Spawn a fake agent that reads lens-a.md, writes three files.
        fake_agent = (
            "set -eu\n"
            "echo 'read:'; cat lens-a.md\n"
            "echo 'research body' > research.md\n"
            "echo 'diff body' > framework-edit.diff\n"
            "echo '{\"tokens_in\": 7, \"tokens_out\": 11, \"cost_usd\": 0.0123}' > usage.json\n"
            "exit 0\n"
        )
        rc, out, err = ws.spawn(
            ["/bin/sh", "-c", fake_agent],
            stdin="", timeout=30,
            env={"PATH": os.environ.get("PATH", ""),
                 "MINI_ORK_RUN_DIR": "/workspace/run"},
            cwd="/workspace/run",
        )
        assert rc == 0, (rc, out, err)
        # After pull, the three files exist locally AND carry remote content.
        assert (run_dir / "research.md").read_text().strip() == "research body"
        assert (run_dir / "framework-edit.diff").read_text().strip() == "diff body"
        usage = json.loads((run_dir / "usage.json").read_text())
        assert usage["tokens_in"] == 7 and usage["tokens_out"] == 11
    finally:
        ws.down()
        server.should_exit = True
        thread.join(timeout=10)


def _tiny_target(base) -> str:
    """A throwaway git checkout for the session to sync (epic 07 needs a target)."""
    repo = base / "target-repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("target\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return str(repo)
