"""Pluggable artifact store: local backend, leak-proof resolution, OCP registry.

The store is the *physical* substrate under ``ArtifactLedger``'s semantic
contract. These tests pin three things:

1. the local backend's read/write/guard behavior,
2. ``resolve_run_root``'s precedence — and specifically that an authoritative
   ``base_dir`` beats a leaked ambient ``MINI_ORK_RUN_DIR`` (the run-dir-split
   regression), and
3. the backend registry seam (``register_artifact_backend`` / selection).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.workflow.store import (
    ArtifactStore,
    ArtifactStoreError,
    LocalArtifactStore,
    make_artifact_store,
    register_artifact_backend,
    resolve_run_root,
)
from mini_ork.workflow.store import _ARTIFACT_BACKENDS  # registry, for snapshot/restore


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    # Clear every run-identity var so resolution never reads a real ambient run.
    for name in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_HOME", "MINI_ORK_RECIPE",
        "MINI_ORK_PLAN_PATH", "MINI_ORK_RUN_ID", "MO_ARTIFACT_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def _backend_registry_snapshot():
    # The registry is module-global; restore it so a test's custom backend
    # can't leak into another test.
    saved = dict(_ARTIFACT_BACKENDS)
    try:
        yield
    finally:
        _ARTIFACT_BACKENDS.clear()
        _ARTIFACT_BACKENDS.update(saved)


# ── LocalArtifactStore: physical read/write round-trip ───────────────────────

def test_local_store_round_trips_bytes(tmp_path):
    store = LocalArtifactStore(tmp_path, "run-1")
    store.write_bytes("chapters/ch1.md", b"# Chapter 1\n")
    assert store.exists("chapters/ch1.md")
    assert store.read_bytes("chapters/ch1.md") == b"# Chapter 1\n"
    assert store.size("chapters/ch1.md") == len(b"# Chapter 1\n")
    # local_path is what a harness writes to — under the run root, materialized.
    assert store.local_path("chapters/ch1.md") == (tmp_path / "chapters" / "ch1.md").resolve()


def test_local_store_run_id_defaults_to_root_name(tmp_path):
    root = tmp_path / "runs" / "run-xyz"
    root.mkdir(parents=True)
    store = LocalArtifactStore(root)
    assert store.run_id == "run-xyz"
    assert store.run_root == root.resolve()


def test_uri_is_backend_agnostic_and_run_scoped(tmp_path):
    store = LocalArtifactStore(tmp_path, "run-1")
    assert store.uri("chapters/ch1.md") == "artifact://run-1/chapters/ch1.md"
    # a leading slash on the rel_path must not double up in the uri
    assert store.uri("/chapters/ch1.md") == "artifact://run-1/chapters/ch1.md"


@pytest.mark.parametrize("escape", ["../evil.txt", "../../etc/passwd", "/etc/passwd"])
def test_guarded_rejects_paths_escaping_the_run_root(tmp_path, escape):
    store = LocalArtifactStore(tmp_path, "run-1")
    with pytest.raises(ArtifactStoreError):
        store.local_path(escape)


def test_local_publish_and_fetch_are_noops(tmp_path):
    # The local dir IS the durable store, so the sync mirror does nothing and
    # must not raise (remote backends override these).
    store = LocalArtifactStore(tmp_path, "run-1")
    store.write_bytes("a.md", b"x")
    assert store.publish("a.md") is None
    assert store.fetch("a.md") is None


# ── resolve_run_root: precedence (real-run scaffold → base_dir → env → fresh) ─

def test_explicit_base_dir_beats_scaffolded_home(tmp_path):
    # Step 1: an explicit base_dir is the caller's authoritative plan-derived
    # run_dir and wins even when a <home>/runs/<run_id> happens to exist (a
    # run_id collision). The explicit argument must beat the guessed home dir.
    home = tmp_path / "home"
    scaffolded = home / "runs" / "run-real"
    scaffolded.mkdir(parents=True)
    base = tmp_path / "plan-derived"
    base.mkdir()
    root = resolve_run_root("run-real", base_dir=base, home=home)
    assert root == base.resolve()


def test_run_id_home_used_when_no_base_dir(tmp_path):
    # Step 2: with no explicit base_dir, the scaffolded <home>/runs/<run_id>
    # (addressed by the stable run_id) is used — the peer-agent / bare-execute
    # path, immune to a leaked ambient dir.
    home = tmp_path / "home"
    scaffolded = home / "runs" / "run-real"
    scaffolded.mkdir(parents=True)
    root = resolve_run_root("run-real", base_dir=None, home=home)
    assert root == scaffolded.resolve()


def test_base_dir_beats_ambient_env(tmp_path, monkeypatch):
    # Step 2: no scaffolded dir → the caller's authoritative base_dir wins over
    # a stray ambient MINI_ORK_RUN_DIR. THIS is the precedence fix.
    home = tmp_path / "home"  # no runs/<run_id> under it
    base = tmp_path / "plan-derived"
    base.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "leaked"))
    root = resolve_run_root("run-bench", base_dir=base, home=home)
    assert root == base.resolve()


def test_ambient_env_is_last_resort(tmp_path, monkeypatch):
    # Step 3: no scaffold, no base_dir → ambient env is the last resort for a
    # bare ``execute`` with no plan.
    home = tmp_path / "home"
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(ambient))
    root = resolve_run_root("run-bare", base_dir=None, home=home)
    assert root == ambient.resolve()


def test_fresh_run_falls_back_to_home_runs(tmp_path):
    # Step 4: nothing exists yet → <home>/runs/<run_id> even if not created (a
    # fresh run about to scaffold it).
    home = tmp_path / "home"
    root = resolve_run_root("run-fresh", base_dir=None, home=home)
    assert root == (home / "runs" / "run-fresh").resolve()


def test_resolve_requires_run_id_or_base_dir(tmp_path):
    home = tmp_path / "home"
    with pytest.raises(ArtifactStoreError):
        resolve_run_root("", base_dir=None, home=home)


# ── THE REGRESSION: a leaked ambient run-dir must not split one run ───────────

def test_leaked_ambient_run_dir_does_not_split_the_run(tmp_path, monkeypatch):
    """Producer and verifier, in the same leaked-env process, converge on one root.

    Reproduces the run-dir-split leak: a long-lived worker leaks
    ``MINI_ORK_RUN_DIR`` pointing at a decoy. Before the fix, the consumer
    resolved to the leaked dir and the producer's artifact was 'missing'. Keying
    on the stable ``run_id`` + authoritative ``base_dir`` makes the split
    impossible: both stores resolve to the same canonical root.
    """
    home = tmp_path / "home"  # nothing scaffolded here
    canonical = tmp_path / "runs" / "run-canonical"
    canonical.mkdir(parents=True)
    decoy = tmp_path / "leaked" / "run-decoy"
    decoy.mkdir(parents=True)
    # a long-lived worker leaked a stale run dir into the ambient env
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(decoy))

    # producer resolves from the STABLE run_id + the plan-derived base_dir
    producer = make_artifact_store("run-canonical", base_dir=canonical, home=home)
    producer.write_bytes("chapters/ch1.md", b"# Chapter 1\n")

    # verifier, later in the SAME leaked-env process, resolves the same way
    verifier = make_artifact_store("run-canonical", base_dir=canonical, home=home)

    assert producer.run_root == verifier.run_root == canonical.resolve()
    assert verifier.read_bytes("chapters/ch1.md") == b"# Chapter 1\n"
    # and nothing was written under the leaked decoy
    assert not (decoy / "chapters" / "ch1.md").exists()


# ── Backend registry (OCP seam) ──────────────────────────────────────────────

def test_make_artifact_store_defaults_to_local(tmp_path):
    store = make_artifact_store("run-1", base_dir=tmp_path)
    assert isinstance(store, LocalArtifactStore)
    assert store.run_root == tmp_path.resolve()


def test_unknown_backend_raises(tmp_path):
    with pytest.raises(ArtifactStoreError):
        make_artifact_store("run-1", base_dir=tmp_path, backend="does-not-exist")


def test_env_selects_backend(tmp_path, monkeypatch, _backend_registry_snapshot):
    seen: dict[str, object] = {}

    def _factory(run_id, *, base_dir=None, home=None):
        seen["run_id"] = run_id
        return LocalArtifactStore(base_dir or tmp_path, run_id)

    register_artifact_backend("mem-test", _factory)
    monkeypatch.setenv("MO_ARTIFACT_BACKEND", "mem-test")
    store = make_artifact_store("run-9", base_dir=tmp_path)
    assert seen["run_id"] == "run-9"
    assert isinstance(store, LocalArtifactStore)


def test_explicit_backend_arg_beats_env(tmp_path, monkeypatch, _backend_registry_snapshot):
    calls: list[str] = []

    def _factory(run_id, *, base_dir=None, home=None):
        calls.append(run_id)
        return LocalArtifactStore(base_dir or tmp_path, run_id)

    register_artifact_backend("explicit-test", _factory)
    monkeypatch.setenv("MO_ARTIFACT_BACKEND", "does-not-exist")
    # explicit arg wins over the (broken) env selection
    make_artifact_store("run-x", base_dir=tmp_path, backend="explicit-test")
    assert calls == ["run-x"]


def test_custom_backend_can_override_publish_fetch(tmp_path, _backend_registry_snapshot):
    """A remote backend's contract: local_path is what agents touch; publish/fetch
    are the durability mirror. This in-memory fake documents that seam."""
    synced: list[str] = []

    class _MirrorStore(LocalArtifactStore):
        def publish(self, rel_path: str) -> None:
            synced.append(f"publish:{rel_path}")

        def fetch(self, rel_path: str) -> None:
            synced.append(f"fetch:{rel_path}")

    def _factory(run_id, *, base_dir=None, home=None):
        return _MirrorStore(base_dir or tmp_path, run_id)

    register_artifact_backend("mirror-test", _factory)
    store = make_artifact_store("run-m", base_dir=tmp_path, backend="mirror-test")
    assert isinstance(store, ArtifactStore)
    store.publish("a.md")
    store.fetch("a.md")
    assert synced == ["publish:a.md", "fetch:a.md"]
