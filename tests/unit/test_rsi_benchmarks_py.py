"""Hermetic tests for ``mini_ork.rsi.benchmarks`` and its built-in benchmark.

No network, no real replay: everything that would touch git or the replay
harness runs inside ``tmp_path`` or against a fake replay module.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork.rsi.benchmarks as bm

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import verifier_replay as vr  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=check)


def make_repo(tmp: Path, subdir: str = "repo") -> Path:
    repo = tmp / subdir
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "unit@example.invalid")
    _git(repo, "config", "user.name", "Unit Test")
    (repo / "f.txt").write_text("one\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def register_counter(name: str):
    state = {"calls": 0, "items": []}

    def runner(root, items, ctx):
        state["calls"] += 1
        state["items"].append(tuple(items) if items is not None else None)
        ids = tuple(items) if items is not None else ("a", "b")
        return bm.BenchResult(
            benchmark=name, tree=None,
            scores={i: (0.0 if i == "b" else 1.0) for i in ids},
            strata={i: "s" for i in ids},
            guards={"g": float(state["calls"])}, meta={"calls": state["calls"]})

    def items_fn(ctx):
        return {"a": "s", "b": "s", "c": "s"}

    bm.register_benchmark(bm.Benchmark(name, runner, items_fn, ("surf",)),
                          replace=True)
    return state


def make_root(tmp: Path, subdir: str = "root") -> Path:
    root = tmp / subdir
    (root / "recipes" / "code-fix" / "verifiers").mkdir(parents=True)
    (root / "recipes" / "code-fix" / "verifiers" / "test.py").write_text("print('v')\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "unit@example.invalid")
    _git(root, "config", "user.name", "Unit Test")
    _git(root, "add", "recipes")
    _git(root, "commit", "-q", "-m", "base")
    return root


def patch_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FakeReplay:
    def __init__(self, replay_impl=None, grade_impl=None):
        self.replay_calls = []
        self.grade_calls = []
        self._replay_impl = replay_impl or self._ok_replay
        self._grade_impl = grade_impl or self._ok_grade

    @staticmethod
    def _ok_replay(patch, task, kw):
        return {"run": patch.name, "task": task["id"], "verifier_pass": True,
                "verifier_reason": "why"}

    @staticmethod
    def _ok_grade(patch, task, kw):
        return {"run": patch.name, "task": task["id"], "hidden_pass": True,
                "failed_ids": []}

    def stored_patches(self, patches_dir, tasks):
        pdir = Path(patches_dir)
        return [(f, tasks[f.stem]) for f in sorted(pdir.glob("*.patch"))
                if f.stem in tasks and f.stat().st_size > 0]

    def replay_one(self, patch, task, **kw):
        self.replay_calls.append((Path(patch), task, kw))
        return self._replay_impl(Path(patch), task, kw)

    def grade_one(self, patch, task, **kw):
        self.grade_calls.append((Path(patch), task, kw))
        return self._grade_impl(Path(patch), task, kw)

    def namespace(self):
        return SimpleNamespace(stored_patches=self.stored_patches,
                               replay_one=self.replay_one,
                               grade_one=self.grade_one)


def vr_setup(tmp: Path, patch_ids, *, seed_rows=None, workers=4,
             grade_impl=None, replay_impl=None, subdir="vr"):
    base = tmp / subdir
    repo = base / "repo"
    repo.mkdir(parents=True)
    pdir = base / "patches"
    pdir.mkdir()
    for i in patch_ids:
        (pdir / f"{i}.patch").write_text(f"PATCH {i}\n+{i}\n")
    manifest = base / "manifest.json"
    manifest.write_text(json.dumps([{"id": i} for i in patch_ids]))
    seed = base / "seed.jsonl"
    if seed_rows is not None:
        seed.write_text("".join(json.dumps(r) + "\n" for r in seed_rows))
    opts = {"patches_dir": str(pdir), "manifest": str(manifest),
            "seed_labels": str(seed if seed_rows is not None else base / "absent.jsonl")}
    ctx = bm.BenchContext(home=base / "home", repo=repo, workers=workers,
                          options=opts)
    fake = FakeReplay(replay_impl=replay_impl, grade_impl=grade_impl)
    return ctx, fake, pdir


def replay_count(fake: FakeReplay, stem: str) -> int:
    return sum(1 for p, _t, _kw in fake.replay_calls if p.stem == stem)


def replay_kwargs(fake: FakeReplay, stem: str) -> list[dict]:
    return [kw for p, _t, kw in fake.replay_calls if p.stem == stem]


def effective_cache_key(name: str, repo: Path, ctx) -> str:
    """The key ``run_benchmark`` uses: surface-keyed when the benchmark has a
    surface, else ``tree_key``.

    RRL-3b changed this: a benchmark with a surface no longer pins
    ``tree_key`` as its cache key, so the two cache-path assertions below
    that used ``bm.tree_key(repo)`` are updated to this helper.
    """
    bench = bm.get_benchmark(name)
    salt = json.dumps({"name": name,
                       "options": dict(sorted(ctx.options.items()))},
                      sort_keys=True)
    return (bm.surface_key(repo, bench.surface, salt=salt)
            if bench.surface else bm.tree_key(repo))


# ── registry ────────────────────────────────────────────────────────────────


def test_builtin_registered_with_surface():
    assert bm.VERIFIER_REPLAY in bm.benchmark_names()
    bench = bm.get_benchmark(bm.VERIFIER_REPLAY)
    assert bench.surface == bm.VR_SURFACE
    assert bench.runner is bm._vr_run
    assert bench.items is bm._vr_items


def test_registry_duplicate_raises_and_replace_restores():
    stub = bm.Benchmark(bm.VERIFIER_REPLAY, lambda r, i, c: None, lambda c: {})
    with pytest.raises(ValueError):
        bm.register_benchmark(stub)
    original = bm.get_benchmark(bm.VERIFIER_REPLAY)
    try:
        bm.register_benchmark(stub, replace=True)
        assert bm.get_benchmark(bm.VERIFIER_REPLAY) is stub
    finally:
        bm.register_benchmark(original, replace=True)


def test_get_benchmark_unknown_raises_keyerror():
    with pytest.raises(KeyError) as excinfo:
        bm.get_benchmark("nope")
    assert "unknown benchmark 'nope'" in str(excinfo.value)


# ── tree_key ────────────────────────────────────────────────────────────────


def test_tree_key_stable_changes_and_non_git(tmp_path):
    repo = make_repo(tmp_path)
    k1 = bm.tree_key(repo)
    assert isinstance(k1, str) and k1
    assert bm.tree_key(repo) == k1
    (repo / "f.txt").write_text("one\nedited\n")
    k2 = bm.tree_key(repo)
    assert k2 != k1
    (repo / "untracked.txt").write_text("new\n")
    k3 = bm.tree_key(repo)
    assert k3 != k2
    plain = tmp_path / "plain"
    plain.mkdir()
    assert bm.tree_key(plain) is None


# ── run_benchmark caching ───────────────────────────────────────────────────


def test_cache_partial_hit_and_use_cache_false(tmp_path):
    repo = make_repo(tmp_path)
    ctx = bm.BenchContext(home=tmp_path / "home", repo=repo)
    name = "unit-cache-1"
    state = register_counter(name)
    try:
        r1 = bm.run_benchmark(name, repo, ctx=ctx)
        assert state["calls"] == 1
        assert r1.scores == {"a": 1.0, "b": 0.0}
        cache_file = bm._cache_path(ctx, name, effective_cache_key(name, repo, ctx))
        assert cache_file.is_file()

        r2 = bm.run_benchmark(name, repo, ctx=ctx, items=["a"])
        assert state["calls"] == 1
        assert r2.scores == {"a": 1.0}

        bm.run_benchmark(name, repo, ctx=ctx, items=["a"], use_cache=False)
        assert state["calls"] == 2
    finally:
        bm._REGISTRY.pop(name, None)


def test_non_git_root_never_writes_cache(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    ctx = bm.BenchContext(home=tmp_path / "home", repo=plain)
    name = "unit-cache-2"
    state = register_counter(name)
    try:
        bm.run_benchmark(name, plain, ctx=ctx)
        assert state["calls"] == 1
        bench_dir = tmp_path / "home" / "rsi" / "bench" / name
        assert not bench_dir.exists() or not any(bench_dir.glob("*.json"))
    finally:
        bm._REGISTRY.pop(name, None)


def test_missing_id_runs_and_cache_holds_union(tmp_path):
    repo = make_repo(tmp_path)
    ctx = bm.BenchContext(home=tmp_path / "home", repo=repo)
    name = "unit-cache-3"
    state = register_counter(name)
    try:
        bm.run_benchmark(name, repo, ctx=ctx)
        bm.run_benchmark(name, repo, ctx=ctx, items=["a", "c"])
        assert state["calls"] == 2
        cached = json.loads(bm._cache_path(
            ctx, name, effective_cache_key(name, repo, ctx)).read_text())
        assert cached["scores"] == {"a": 1.0, "b": 0.0, "c": 1.0}
        bm.run_benchmark(name, repo, ctx=ctx, items=["c"])
        assert state["calls"] == 2
    finally:
        bm._REGISTRY.pop(name, None)


# ── BenchResult ─────────────────────────────────────────────────────────────


def test_benchresult_round_trip_and_subset():
    r = bm.BenchResult(benchmark="b", tree="tk", scores={"a": 1.0, "b": 0.0},
                       strata={"a": "correct", "b": "hard_negative"},
                       guards={"false_accept": 1.0}, skipped=("x",), meta={"m": 1})
    d = r.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["skipped"] == ["x"]
    assert bm.BenchResult.from_dict(d) == r
    s = r.subset(["a"])
    assert s.scores == {"a": 1.0}
    assert s.strata == {"a": "correct"}
    assert s.guards == r.guards
    assert s.meta == {"m": 1}


# ── BOOTSTRAP / import_root ─────────────────────────────────────────────────


def test_bootstrap_constant():
    assert vr.BOOTSTRAP == ("import runpy, sys; root, script = sys.argv[1], sys.argv[2]; "
                            "sys.path[0:0] = [root]; sys.argv = [script]; "
                            "runpy.run_path(script, run_name='__main__')")


def test_run_verifier_import_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "mini_ork_probe_mod.py").write_text('WHERE = "root"\n')
    probe = tmp_path / "v.py"
    probe.write_text(
        "import mini_ork_probe_mod, json\n"
        'print(json.dumps({"pass": mini_ork_probe_mod.WHERE == "root", "reason": "ok"}))\n')
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert vr.run_verifier(probe, scratch=scratch, python=sys.executable,
                           timeout=60, import_root=root) == (True, "ok")
    assert vr.run_verifier(probe, scratch=scratch, python=sys.executable,
                           timeout=60)[0] is None


# ── ensure_labels / _vr_run ─────────────────────────────────────────────────


def test_ensure_labels_seed_grade_and_persistence(tmp_path, monkeypatch):
    ctx, fake, pdir = vr_setup(tmp_path, ["t1", "t2", "t3"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True},
        {"run": "t2.patch", "hidden_pass": False},
        {"run": "t3.patch", "hidden_pass": "maybe"},
    ])
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    res = bm.ensure_labels(ctx, ["t1", "t2", "t3"])
    assert res == {"t1": True, "t2": False, "t3": True}
    assert len(fake.grade_calls) == 1
    assert fake.grade_calls[0][0] == pdir / "t3.patch"
    labels = json.loads(bm.labels_path(ctx).read_text())
    assert labels["t1"] == {"patch_sha256": patch_sha(pdir / "t1.patch"),
                            "hidden_pass": True}
    bm.ensure_labels(ctx, ["t1", "t2", "t3"])
    assert len(fake.grade_calls) == 1


def test_ensure_labels_edited_patch_is_regraded(tmp_path, monkeypatch):
    ctx, fake, pdir = vr_setup(tmp_path, ["t1"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True}])
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    assert bm.ensure_labels(ctx, ["t1"]) == {"t1": True}
    assert fake.grade_calls == []
    (pdir / "t1.patch").write_text("PATCH t1\n+edited\n")
    fake2 = FakeReplay(grade_impl=lambda patch, task, kw: {
        "run": patch.name, "task": task["id"], "hidden_pass": False,
        "failed_ids": []})
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake2.namespace())
    assert bm.ensure_labels(ctx, ["t1"]) == {"t1": False}
    assert fake2.grade_calls and fake2.grade_calls[0][0] == pdir / "t1.patch"


def test_vr_run_scores_guards_and_strata(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    ctx, fake, _ = vr_setup(tmp_path, ["t1", "t2", "t3"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True},
        {"run": "t2.patch", "hidden_pass": False},
        {"run": "t3.patch", "hidden_pass": True},
    ], replay_impl=lambda patch, task, kw: {
        "run": patch.name, "task": task["id"], "verifier_pass": True,
        "verifier_reason": "why"})
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    res = bm._vr_run(root, None, ctx)
    assert res.scores == {"t1": 1.0, "t2": 0.0, "t3": 1.0}
    assert res.guards == {"false_accept": 1.0, "false_reject": 0.0, "no_verdict": 0.0}
    assert res.strata["t2"] == "hard_negative"
    assert res.tree == bm.tree_key(root)
    for stem in ("t1", "t2", "t3"):
        kws = replay_kwargs(fake, stem)
        assert len(kws) == 1
        assert kws[0]["import_root"] == root
        assert kws[0]["hidden"] is False
        assert kws[0]["verifier"] == root / bm.DEFAULT_VERIFIER


def test_vr_run_no_verdict_scores_zero(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    ctx, fake, _ = vr_setup(tmp_path, ["t1"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True}],
        replay_impl=lambda patch, task, kw: {
            "run": patch.name, "task": task["id"], "verifier_pass": None,
            "verifier_reason": "no verdict"})
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    res = bm._vr_run(root, None, ctx)
    assert res.scores == {"t1": 0.0}
    assert res.guards["no_verdict"] == 1.0


def test_vr_run_retry_once_then_error_not_skip(tmp_path, monkeypatch):
    """A twice-raising item is an error of this run, never a cached skip."""
    root = make_root(tmp_path)
    ctx, fake, _ = vr_setup(tmp_path, ["t1", "t3"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True},
        {"run": "t3.patch", "hidden_pass": True}])

    def impl(patch, task, kw):
        if patch.stem == "t3":
            raise RuntimeError("boom")
        return {"run": patch.name, "task": task["id"], "verifier_pass": True,
                "verifier_reason": "why"}

    fake._replay_impl = impl
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    res = bm._vr_run(root, None, ctx)
    assert "t3" not in res.skipped
    assert res.meta["errors"]["t3"] == "RuntimeError: boom"
    assert res.scores == {"t1": 1.0}
    assert replay_count(fake, "t3") == 2


def test_errored_item_is_rerun_not_served_from_cache(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    ctx, fake, _ = vr_setup(tmp_path, ["t1", "t3"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True},
        {"run": "t3.patch", "hidden_pass": True}])
    state = {"boom": True}

    def impl(patch, task, kw):
        if patch.stem == "t3" and state["boom"]:
            raise RuntimeError("transient")
        return {"run": patch.name, "task": task["id"], "verifier_pass": True,
                "verifier_reason": "why"}

    fake._replay_impl = impl
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    first = bm.run_benchmark(bm.VERIFIER_REPLAY, root, ctx=ctx, items=["t1", "t3"])
    assert first.scores == {"t1": 1.0}
    state["boom"] = False
    second = bm.run_benchmark(bm.VERIFIER_REPLAY, root, ctx=ctx, items=["t1", "t3"])
    assert second.scores == {"t1": 1.0, "t3": 1.0}


def test_vr_run_retry_once_then_success(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    ctx, fake, _ = vr_setup(tmp_path, ["t3"], seed_rows=[
        {"run": "t3.patch", "hidden_pass": True}])
    seen = {"n": 0}

    def impl(patch, task, kw):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("transient")
        return {"run": patch.name, "task": task["id"], "verifier_pass": True,
                "verifier_reason": "why"}

    fake._replay_impl = impl
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    res = bm._vr_run(root, None, ctx)
    assert res.skipped == ()
    assert res.scores == {"t3": 1.0}
    assert replay_count(fake, "t3") == 2


def test_vr_items_returns_strata(tmp_path, monkeypatch):
    ctx, fake, _ = vr_setup(tmp_path, ["t1", "t2", "t3"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True},
        {"run": "t2.patch", "hidden_pass": False}],
        grade_impl=lambda patch, task, kw: {
            "run": patch.name, "task": task["id"],
            "skipped": "patch does not apply to base: x"})
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    assert bm._vr_items(ctx) == {"t1": "correct", "t2": "hard_negative"}


def test_vr_run_missing_verifier_raises(tmp_path, monkeypatch):
    ctx, fake, _ = vr_setup(tmp_path, ["t1"])
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    plain = tmp_path / "plain-root"
    plain.mkdir()
    with pytest.raises(FileNotFoundError):
        bm._vr_run(plain, None, ctx)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_list(capsys):
    assert bm.main(["list"]) == 0
    out = capsys.readouterr().out
    assert any(bm.VERIFIER_REPLAY in line for line in out.splitlines())


def test_cli_run_unknown_returns_2(tmp_path, capsys):
    assert bm.main(["run", "nope", "--root", str(tmp_path)]) == 2
    assert capsys.readouterr().err.strip()


# ── RRL-3b: surface key + resolved/scoped verifier-replay ───────────────────


def test_cache_version_and_surface_key(tmp_path):
    assert bm.CACHE_VERSION == "2"
    (tmp_path / "s.py").write_text("a\n")
    k1 = bm.surface_key(tmp_path, ["s.py"])
    assert isinstance(k1, str) and len(k1) == 64
    assert bm.surface_key(tmp_path, ["s.py"]) == k1
    (tmp_path / "other.txt").write_text("1\n")  # outside the surface
    assert bm.surface_key(tmp_path, ["s.py"]) == k1
    assert bm.surface_key(tmp_path, ["s.py"], salt="x") != k1
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.py").write_text("a\n")
    kd = bm.surface_key(tmp_path, ["d"])
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "x.pyc").write_text("junk\n")
    (d / "x.pyc").write_text("junk\n")
    assert bm.surface_key(tmp_path, ["d"]) == kd
    (d / "a.py").write_text("b\n")
    assert bm.surface_key(tmp_path, ["d"]) != kd
    # a rename inside a surfaced directory changes the key: the relative path
    # is part of the digest, not just each file's content.
    before = bm.surface_key(tmp_path, ["d"])
    (d / "a.py").rename(d / "b.py")
    assert bm.surface_key(tmp_path, ["d"]) != before
    (d / "b.py").rename(d / "sub")
    assert bm.surface_key(tmp_path, ["d"]) != before
    # a missing entry is hashed as missing, distinct from present content
    assert bm.surface_key(tmp_path, ["gone.py"]) != bm.surface_key(tmp_path, ["s.py"])


def test_vr_surface_includes_the_measurement_harness():
    # Replay/grading logic decides the scores, so it must be in the surface —
    # otherwise an edit to the harness reuses stale cached measurements.
    assert "scripts/verifier_replay.py" in bm.VR_SURFACE
    assert "mini_ork/rsi/benchmarks.py" in bm.VR_SURFACE


def test_run_benchmark_surface_keyed_cache(tmp_path):
    repo = make_repo(tmp_path)  # commits f.txt
    name = "unit-surface-cache"
    calls = {"n": 0}

    def runner(root, items, ctx):
        calls["n"] += 1
        return bm.BenchResult(benchmark=name, tree="runner-tree", scores={},
                              strata={}, guards={})

    bm.register_benchmark(bm.Benchmark(name, runner, lambda c: {}, ("f.txt",)),
                          replace=True)
    ctx = bm.BenchContext(home=tmp_path / "home", repo=repo)
    salt = json.dumps({"name": name, "options": {}}, sort_keys=True)
    try:
        r1 = bm.run_benchmark(name, repo, ctx=ctx)
        assert calls["n"] == 1
        assert r1.tree == bm.surface_key(repo, ("f.txt",), salt=salt)
        assert r1.tree != "runner-tree"
        (repo / "outside.txt").write_text("x\n")  # outside the surface
        r2 = bm.run_benchmark(name, repo, ctx=ctx)
        assert calls["n"] == 1 and r2.tree == r1.tree
        (repo / "f.txt").write_text("changed\n")  # inside the surface
        r3 = bm.run_benchmark(name, repo, ctx=ctx)
        assert calls["n"] == 2 and r3.tree != r1.tree
    finally:
        bm._REGISTRY.pop(name, None)


def test_vr_run_resolves_paths_and_defaults_scoped(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    ctx, fake, _ = vr_setup(tmp_path, ["t1"], seed_rows=[
        {"run": "t1.patch", "hidden_pass": True}])
    monkeypatch.setattr(bm, "_replay_module", lambda repo: fake.namespace())
    bm._vr_run(root, None, ctx)
    kw = fake.replay_calls[0][2]
    assert kw.get("scoped_tests") is True
    assert Path(kw["repo"]).is_absolute()
    assert Path(kw["verifier"]).is_absolute()
    assert Path(kw["import_root"]).is_absolute()

    ctx_off = bm.BenchContext(home=ctx.home, repo=ctx.repo,
                              options={**ctx.options, "scoped_tests": "0"})
    bm._vr_run(root, None, ctx_off)
    assert fake.replay_calls[-1][2].get("scoped_tests") is False


def test_option_paths_use_resolved_repo(tmp_path):
    ctx = bm.BenchContext(home=tmp_path / "home", repo=Path("."))
    patches, manifest, seed = bm._option_paths(ctx)
    resolved = Path(".").resolve()
    assert patches == resolved / bm.DEFAULT_PATCHES
    assert manifest == resolved / bm.DEFAULT_MANIFEST
    assert seed == resolved / bm.DEFAULT_SEED
