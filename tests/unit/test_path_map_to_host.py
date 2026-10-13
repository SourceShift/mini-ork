"""``PathMap.to_host`` is the exact inverse of ``PathMap.path`` over the run's
roots: what a remote agent reports in sandbox terms reads back against the host."""
from __future__ import annotations

from mini_ork.runtime.path_map import PathMap
from mini_ork.runtime.run_roots import RunRoots


def _map(tmp_path):
    roots = {n: tmp_path / n for n in ("target", "run", "home", "engine")}
    for d in roots.values():
        d.mkdir()
    return roots, PathMap.from_roots(RunRoots(target=str(roots["target"]),
                                              run_dir=str(roots["run"]),
                                              home=str(roots["home"]),
                                              engine=str(roots["engine"])))


def test_every_root_round_trips(tmp_path):
    roots, pm = _map(tmp_path)
    for name, sandbox in (("target", "/workspace/target"), ("run", "/workspace/run"),
                          ("home", "/workspace/mo-home"), ("engine", "/opt/mini-ork")):
        host_file = str(roots[name] / "pkg" / "x.py")
        assert pm.path(host_file) == f"{sandbox}/pkg/x.py"
        assert pm.path(pm.to_host(f"{sandbox}/pkg/x.py")) == f"{sandbox}/pkg/x.py"
    target_host = dict((s, h) for h, s in pm.pairs)["/workspace/target"]
    assert pm.to_host("/workspace/target") == target_host


def test_paths_outside_the_map_are_unchanged(tmp_path):
    roots, pm = _map(tmp_path)
    for value in (str(roots["target"] / "a.py"),      # a host path (local run)
                  "/workspace/targetx/a.py",           # a sibling, not the root
                  "/etc/hosts", "relative/a.py", "", "-c"):
        assert pm.to_host(value) == value
