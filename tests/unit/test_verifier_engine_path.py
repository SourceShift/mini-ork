"""Regression: recipe verifiers import the engine that dispatched them.

Live smoke of G03-T04 (2026-10-05): `_run_verifier_ref` spawned the code-fix
verifier in the target repo, where `mini_ork` resolved through a venv editable
install to a STALE checkout; the new `mini_ork.gates.suite_adequacy` import
failed and every audit abstained as `module-unavailable`. The executor must put
its own engine root first on the verifier's PYTHONPATH.
"""
from __future__ import annotations

import json
from pathlib import Path

from mini_ork.cli import execute as ex

ENGINE = Path(ex.__file__).resolve().parents[2]


def test_py_verifier_imports_the_executing_engine(tmp_path, monkeypatch):
    target = tmp_path / "target-repo"          # no mini_ork here
    target.mkdir()
    script = tmp_path / "probe_verifier.py"
    script.write_text(
        "import json, mini_ork\n"
        "print(json.dumps({'pass': True, 'engine': mini_ork.__file__}))\n"
    )
    monkeypatch.delenv("PYTHONPATH", raising=False)
    evidence = tmp_path / "verifier_probe.json"

    rc = ex._run_verifier_ref(str(script), str(evidence), cwd=str(target), run_dir="")

    payload = json.loads(evidence.read_text())
    assert rc == 0, evidence.read_text()
    assert Path(payload["engine"]).resolve().is_relative_to(ENGINE)


def test_inherited_pythonpath_is_kept_after_the_engine(tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "vt_marker_mod.py").write_text("X = 1\n")
    script = tmp_path / "probe_verifier.py"
    script.write_text(
        "import json, sys, vt_marker_mod\n"
        "print(json.dumps({'pass': True, 'path0': sys.path[1]}))\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(extra))
    evidence = tmp_path / "verifier_probe.json"

    rc = ex._run_verifier_ref(str(script), str(evidence), cwd=str(tmp_path), run_dir="")

    payload = json.loads(evidence.read_text())
    assert rc == 0, evidence.read_text()
    assert Path(payload["path0"]).resolve() == ENGINE   # engine first, inherited still importable
